"""Lock captured counterexamples into permanent pytest / jest / gtest tests.

The arena returns a ``counterexample_payload`` (schema ``counterexample/v1``)
carrying the minimal input, the oracle's expected output (or expected
exception / expected post-call argument state) and the comparator used.  This
module renders it as an idiomatic unit test and appends it to a test file -
idempotently: the test name embeds a hash of the input, so locking the same
counterexample twice is a no-op.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import math
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from counterexample.codec import cpp_literal, decode, infer_cpp_type, js_literal, py_literal

FRAMEWORKS = ("pytest", "jest", "vitest", "gtest")
_ALIASES = {"py": "pytest", "python": "pytest", "js": "jest", "javascript": "jest", "ts": "jest", "typescript": "jest", "googletest": "gtest", "cpp": "gtest", "c++": "gtest"}
_SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "build", "dist", "site-packages", ".tox", ".mypy_cache", ".pytest_cache", "third_party", "vendor"}
_STD_NAMES = ("vector", "string", "map", "set", "pair", "tuple", "deque", "list", "unordered_map", "unordered_set", "optional", "array", "multiset")


# --------------------------------------------------------------------------- #
# Payload handling
# --------------------------------------------------------------------------- #


def _load_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError("counterexample_payload must be an object (or its JSON string)")
    if "counterexample_payload" in payload and isinstance(payload["counterexample_payload"], dict):
        payload = payload["counterexample_payload"]
    if "args" not in payload:
        raise ValueError("counterexample_payload is missing 'args' (pass the arena's counterexample_payload unchanged)")
    return payload


def _contains_float(v: Any) -> bool:
    if isinstance(v, float):
        return True
    if isinstance(v, dict):
        return any(_contains_float(k) or _contains_float(x) for k, x in v.items())
    if isinstance(v, (list, tuple, set, frozenset)):
        return any(_contains_float(x) for x in v)
    return False


def _contains_tuple(v: Any) -> bool:
    if isinstance(v, tuple):
        return True
    if isinstance(v, dict):
        return any(_contains_tuple(x) for x in v.values())
    if isinstance(v, (list, set, frozenset)):
        return any(_contains_tuple(x) for x in v)
    return False


def _find_root(start: Path) -> Path:
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if any((p / marker).exists() for marker in (".git", "pyproject.toml", "setup.py", "setup.cfg", "package.json", "CMakeLists.txt")):
            return p
    return start.resolve()


def _iter_files(root: Path, suffixes: tuple[str, ...], limit: int = 5000):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(suffixes):
                count += 1
                if count > limit:
                    return
                yield Path(dirpath) / name


def _split_fn(fn: str) -> tuple[str | None, str]:
    for sep in ("::", "."):
        if sep in fn:
            cls, meth = fn.rsplit(sep, 1)
            return cls.split(sep)[-1], meth
    return None, fn


def _discover_python_module(test_path: Path, fn: str) -> str | None:
    cls, name = _split_fn(fn)
    root = _find_root(test_path.parent)
    rx = re.compile(rf"^class\s+{re.escape(cls)}\b" if cls else rf"^(?:async\s+)?def\s+{re.escape(name)}\s*\(", re.M)
    for path in _iter_files(root, (".py",)):
        if path.name.startswith("test_") or path.name.endswith("_test.py") or path.resolve() == test_path.resolve():
            continue
        try:
            if rx.search(path.read_text(encoding="utf-8", errors="ignore")):
                rel = path.relative_to(root).with_suffix("")
                parts = list(rel.parts)
                if parts and parts[0] == "src":
                    parts = parts[1:]
                if parts and parts[-1] == "__init__":
                    parts = parts[:-1]
                return ".".join(parts) if parts else None
        except (OSError, ValueError):
            continue
    return None


def _discover_js_module(test_path: Path, fn: str) -> str | None:
    cls, name = _split_fn(fn)
    target = cls or name
    root = _find_root(test_path.parent)
    rx = re.compile(
        rf"(?:function\s+{re.escape(target)}\b|(?:const|let|var)\s+{re.escape(target)}\s*=|class\s+{re.escape(target)}\b|exports\.{re.escape(target)}\s*=)"
    )
    for path in _iter_files(root, (".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx")):
        if re.search(r"\.(test|spec)\.", path.name) or path.resolve() == test_path.resolve():
            continue
        try:
            if rx.search(path.read_text(encoding="utf-8", errors="ignore")):
                rel = os.path.relpath(path.with_suffix(""), test_path.parent).replace(os.sep, "/")
                return rel if rel.startswith(".") else "./" + rel
        except (OSError, ValueError):
            continue
    return None


def _discover_cpp_header(test_path: Path, fn: str) -> str | None:
    cls, name = _split_fn(fn)
    root = _find_root(test_path.parent)
    rx = re.compile(rf"\bclass\s+{re.escape(cls)}\b" if cls else rf"\b{re.escape(name)}\s*\(")
    for path in _iter_files(root, (".h", ".hpp", ".hh", ".hxx")):
        try:
            if rx.search(path.read_text(encoding="utf-8", errors="ignore")):
                return os.path.relpath(path, test_path.parent).replace(os.sep, "/")
        except (OSError, ValueError):
            continue
    return None


# --------------------------------------------------------------------------- #
# pytest
# --------------------------------------------------------------------------- #

PY_HELPER = '''

# --- counterexample: comparison helper (generated, shared by locked tests) ---
def _counterexample_equal(actual, expected, mode="auto", tol=1e-9):
    import math

    def eq(a, b):
        if isinstance(a, bool) or isinstance(b, bool):
            return a == b
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if isinstance(a, float) or isinstance(b, float):
                a, b = float(a), float(b)
                if math.isnan(a) or math.isnan(b):
                    return math.isnan(a) and math.isnan(b)
                return a == b or (tol > 0 and math.isclose(a, b, rel_tol=tol, abs_tol=tol))
            return a == b
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            if mode == "unordered_deep":
                return multiset(list(a), list(b))
            return len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
        if isinstance(a, dict) and isinstance(b, dict):
            return a.keys() == b.keys() and all(eq(a[k], b[k]) for k in a)
        return a == b

    def multiset(xs, ys):
        if len(xs) != len(ys):
            return False
        rest = list(ys)
        for x in xs:
            for i, y in enumerate(rest):
                if eq(x, y):
                    del rest[i]
                    break
            else:
                return False
        return True

    seqs = (list, tuple, set, frozenset)
    if mode in ("unordered", "set") and isinstance(actual, seqs) and isinstance(expected, seqs):
        a, b = list(actual), list(expected)
        if mode == "set":
            a = [x for i, x in enumerate(a) if not any(eq(x, y) for y in a[:i])]
            b = [x for i, x in enumerate(b) if not any(eq(x, y) for y in b[:i])]
        return multiset(a, b)
    return eq(actual, expected)
'''


def _py_call(fn: str, args: list[Any], kwargs: dict[str, Any], args_var: str | None = None) -> str:
    cls, meth = _split_fn(fn)
    target = f"{cls}().{meth}" if cls else meth
    if args_var:
        kw = f", **{py_literal(kwargs)}" if kwargs else ""
        return f"{target}(*{args_var}{kw})"
    parts = [py_literal(a) for a in args] + [f"{k}={py_literal(v)}" for k, v in kwargs.items()]
    return f"{target}({', '.join(parts)})"


def _render_pytest(p: dict[str, Any], name: str, fn: str) -> tuple[str, bool, bool]:
    """Return (test source, needs_pytest_import, needs_helper)."""
    args, kwargs = decode(p["args"]), decode(p.get("kwargs") or {})
    kind = p.get("expected_kind", "return")
    comparator = p.get("comparator", "auto")
    tol = float(p.get("float_tolerance", 1e-9))
    doc = f'    """Locked by counterexample on {date.today().isoformat()} (failure: {p.get("failure_kind", "mismatch")})."""'
    lines = [f"def {name}():", doc]
    if kind == "exception":
        exc = p.get("expected_exception") or "Exception"
        exc = exc if isinstance(getattr(builtins, exc, None), type) and issubclass(getattr(builtins, exc), BaseException) else "Exception"
        lines += [f"    with pytest.raises({exc}):", f"        {_py_call(fn, args, kwargs)}"]
        return "\n".join(lines) + "\n", True, False
    expected = decode(p.get("expected"))
    needs_helper = comparator in ("unordered", "set", "unordered_deep") or _contains_float(expected) or _contains_tuple(expected)
    if kind == "args_after":
        lines += [f"    args = {py_literal(args)}", f"    {_py_call(fn, args, kwargs, 'args')}"]
        actual = "args"
    else:
        lines.append(f"    result = {_py_call(fn, args, kwargs)}")
        actual = "result"
    if needs_helper:
        mode = comparator if comparator in ("unordered", "set", "unordered_deep") else "auto"
        tol_arg = 0.0 if comparator in ("exact", "strict") else tol
        lines.append(f"    assert _counterexample_equal({actual}, {py_literal(expected)}, {mode!r}, {tol_arg!r}), {actual}")
    else:
        lines.append(f"    assert {actual} == {py_literal(expected)}")
    return "\n".join(lines) + "\n", False, needs_helper


# --------------------------------------------------------------------------- #
# jest / vitest
# --------------------------------------------------------------------------- #


def _js_call(fn: str, args: list[Any], spread_var: str | None = None) -> str:
    cls, meth = _split_fn(fn)
    target = f"new {cls}().{meth}" if cls else meth
    if spread_var:
        return f"{target}(...{spread_var})"
    return f"{target}({', '.join(js_literal(a) for a in args)})"


def _render_jest(p: dict[str, Any], name: str, fn: str) -> str:
    args, kwargs = decode(p["args"]), decode(p.get("kwargs") or {})
    if kwargs:
        args = args + [kwargs]  # JS has no kwargs; pass them as a trailing options object
    kind = p.get("expected_kind", "return")
    comparator = p.get("comparator", "auto")
    lines = [f"test({json.dumps(name)}, () => {{", f"  // Locked by counterexample on {date.today().isoformat()} (failure: {p.get('failure_kind', 'mismatch')})."]
    if kind == "exception":
        lines += [f"  expect(() => {_js_call(fn, args)}).toThrow();", "});"]
        return "\n".join(lines) + "\n"
    expected = decode(p.get("expected"))
    if kind == "args_after":
        lines += [f"  const args = {js_literal(args)};", f"  {_js_call(fn, args, 'args')};"]
        actual = "args"
    else:
        lines.append(f"  const result = {_js_call(fn, args)};")
        actual = "result"
    if isinstance(expected, float) and math.isnan(expected):
        lines.append(f"  expect({actual}).toBeNaN();")
    elif isinstance(expected, float) and not math.isinf(expected) and comparator not in ("exact", "strict"):
        lines.append(f"  expect({actual}).toBeCloseTo({js_literal(expected)}, 6);")
    elif comparator in ("unordered", "set", "unordered_deep") and isinstance(expected, (list, tuple)):
        lines.append(f"  expect({actual}).toHaveLength({len(expected)});")
        lines.append(f"  expect({actual}).toEqual(expect.arrayContaining({js_literal(expected)}));")
    else:
        lines.append(f"  expect({actual}).toEqual({js_literal(expected)});")
    lines.append("});")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# gtest
# --------------------------------------------------------------------------- #


def _std_qualify(t: str) -> str:
    t = re.sub(r"\bconst\b|&|\bvolatile\b", " ", t)
    t = re.sub(r"(?<![\w:])(" + "|".join(_STD_NAMES) + r")\b", r"std::\1", t)
    return re.sub(r"\s+", " ", t).strip()


def _render_gtest(p: dict[str, Any], name: str, fn: str) -> str:
    args = decode(p["args"])
    kind = p.get("expected_kind", "return")
    sig = p.get("cpp_signature") or {}
    types = [_std_qualify(t) for t in sig.get("params", [])]
    if len(types) != len(args):
        types = [infer_cpp_type(a) for a in args]
    cls, meth = _split_fn(fn)
    lines = [f"TEST(Counterexample, {name}) {{", f"  // Locked by counterexample on {date.today().isoformat()} (failure: {p.get('failure_kind', 'mismatch')})."]
    for i, (t, a) in enumerate(zip(types, args)):
        lines.append(f"  {t} arg{i} = {cpp_literal(a)};")
    call_args = ", ".join(f"arg{i}" for i in range(len(args)))
    if cls:
        lines.append(f"  {cls} solver;")
        call = f"solver.{meth}({call_args})"
    else:
        call = f"{fn.replace('.', '::')}({call_args})"
    if kind == "exception":
        lines += [f"  EXPECT_ANY_THROW({call});", "}"]
        return "\n".join(lines) + "\n"
    expected = decode(p.get("expected"))
    tol = float(p.get("float_tolerance", 1e-9))
    if kind == "args_after":
        lines.append(f"  {call};")
        for i, (t, e) in enumerate(zip(types, expected if isinstance(expected, (list, tuple)) else [])):
            lines.append(f"  {t} expected{i} = {cpp_literal(e)};")
            lines.append(f"  EXPECT_EQ(arg{i}, expected{i});")
    else:
        lines.append(f"  auto result = {call};")
        if isinstance(expected, float) and math.isnan(expected):
            lines.append("  EXPECT_TRUE(std::isnan(result));")
        elif isinstance(expected, float) and not math.isinf(expected):
            lines.append(f"  EXPECT_NEAR(result, {cpp_literal(expected)}, {tol!r});")
        else:
            lines.append(f"  decltype(result) expected = {cpp_literal(expected)};")
            lines.append("  EXPECT_EQ(result, expected);")
    lines.append("}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def lock_regression_test(
    counterexample_payload: Any,
    test_file_path: str,
    framework: str = "pytest",
    *,
    module: str | None = None,
    import_line: str | None = None,
    function_name: str | None = None,
    test_name: str | None = None,
) -> dict[str, Any]:
    """Append a regression test for the counterexample to ``test_file_path``."""
    try:
        p = _load_payload(counterexample_payload)
    except (ValueError, json.JSONDecodeError) as exc:
        return {"status": "ERROR", "error": str(exc)}
    fw = _ALIASES.get(framework.lower(), framework.lower())
    if fw not in FRAMEWORKS:
        return {"status": "ERROR", "error": f"unsupported framework {framework!r}; choose pytest, jest, vitest or gtest"}
    fn = function_name or p.get("function")
    if not fn:
        return {"status": "ERROR", "error": "payload has no 'function'; pass function_name"}
    digest = hashlib.sha1(json.dumps([fn, p["args"], p.get("kwargs")], sort_keys=True, default=str).encode()).hexdigest()[:8]
    base = re.sub(r"\W+", "_", fn.replace("::", "_").replace(".", "_")).strip("_")
    path = Path(test_file_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    warnings: list[str] = []
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    cls, meth = _split_fn(fn)
    import_name = cls or meth

    if fw == "pytest":
        name = test_name or f"test_counterexample_{base}_{digest}"
        if not import_line:
            mod = module or _discover_python_module(path, fn)
            if mod:
                import_line = f"from {mod} import {import_name}"
            else:
                import_line = f"from your_module import {import_name}  # TODO: point at the module under test"
                warnings.append("could not locate the module defining the function; fix the generated import or pass module=")
        snippet, needs_pytest, needs_helper = _render_pytest(p, name, fn)
        header_lines = ([import_line] if import_line not in existing else []) + (["import pytest"] if needs_pytest and "import pytest" not in existing else [])
        helper = PY_HELPER if needs_helper and "_counterexample_equal" not in existing else ""
        run = f"pytest {path.name}::{name} -q"
    elif fw in ("jest", "vitest"):
        name = test_name or f"counterexample: {fn} minimal input ({digest})"
        esm = path.suffix in (".mjs", ".ts", ".tsx", ".mts") or re.search(r"^\s*import\s", existing, re.M) is not None or fw == "vitest"
        if not import_line:
            mod = module or _discover_js_module(path, fn)
            if not mod:
                mod = f"./{import_name}"
                warnings.append("could not locate the module defining the function; fix the generated import or pass module=")
            import_line = f"import {{ {import_name} }} from {json.dumps(mod)};" if esm else f"const {{ {import_name} }} = require({json.dumps(mod)});"
        snippet = _render_jest(p, name, fn)
        header_lines = []
        if fw == "vitest" and "from 'vitest'" not in existing and 'from "vitest"' not in existing:
            header_lines.append('import { test, expect } from "vitest";')
        if import_line not in existing:
            header_lines.append(import_line)
        helper = ""
        run = f"npx {'vitest run' if fw == 'vitest' else 'jest'} {path.name} -t {json.dumps(name)}"
    else:
        name = test_name or f"{base}_{digest}"
        if not import_line:
            header = module or _discover_cpp_header(path, fn)
            if header:
                import_line = f'#include "{header}"'
            else:
                import_line = f'#include "{import_name}.h"  // TODO: include the header declaring {fn}'
                warnings.append("could not locate a header declaring the function; fix the generated #include or pass module=")
        snippet = _render_gtest(p, name, fn)
        header_lines = [ln for ln in ["#include <gtest/gtest.h>", "#include <cmath>", "#include <limits>", "#include <map>", "#include <set>", "#include <string>", "#include <tuple>", "#include <vector>", import_line] if ln not in existing]
        helper = ""
        run = f"ctest -R Counterexample.{name}  (or ./your_test_binary --gtest_filter=Counterexample.{name})"

    marker = f"TEST(Counterexample, {name})" if fw == "gtest" else (json.dumps(name) if fw in ("jest", "vitest") else f"def {name}(")
    if marker in existing:
        return {
            "status": "ALREADY_LOCKED",
            "test_file_path": str(path),
            "test_name": name,
            "framework": fw,
            "run_command": run,
            "message": "this counterexample is already locked in the test file",
        }

    if existing:
        new_text = existing
        if header_lines:
            new_text = _insert_headers(existing, header_lines, fw)
        new_text = new_text.rstrip("\n") + "\n" + helper + "\n\n" + snippet
    else:
        banner = "# Regression tests locked by counterexample.\n" if fw == "pytest" else "// Regression tests locked by counterexample.\n"
        new_text = banner + "\n".join(header_lines) + "\n" + helper + "\n\n" + snippet
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return {
        "status": "LOCKED",
        "test_file_path": str(path),
        "test_name": name,
        "framework": fw,
        "import_line": import_line,
        "test_code": snippet,
        "run_command": run,
        "warnings": warnings,
    }


def _insert_headers(text: str, headers: list[str], fw: str) -> str:
    """Insert import/include lines after the existing block of imports."""
    lines = text.splitlines()
    pattern = {
        "pytest": r"^(import |from \S+ import )",
        "jest": r"^(import |const .*require\()",
        "vitest": r"^(import |const .*require\()",
        "gtest": r"^#include ",
    }[fw]
    last = -1
    for i, ln in enumerate(lines):
        if re.match(pattern, ln):
            last = i
    insert_at = last + 1 if last >= 0 else (1 if lines and lines[0].startswith(("#", "//")) and fw != "gtest" else 0)
    lines[insert_at:insert_at] = headers
    return "\n".join(lines) + "\n"
