"""Zero-effort ground-truth oracles from version control.

When refactoring, the pre-refactor implementation *is* the specification.
``extract_git_oracle`` reads the file at any git ref, locates the symbol
(Python via ``ast``; C/C++ via the brace-aware outliner), and returns a
self-contained snippet: the symbol plus the imports, helpers, constants and
classes it transitively depends on, ready to pass as ``oracle_code``.
"""

from __future__ import annotations

import ast
import difflib
import os
import subprocess
from pathlib import Path
from typing import Any

from counterexample.adapters.cpp_outline import CppDef, identifiers, include_lines, outline, toplevel_statements

PY_SUFFIXES = {".py", ".pyw"}
CPP_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".tpp"}


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: str | Path) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=60)
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out") from exc
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode("utf-8", "replace").strip() or f"git {' '.join(args)} failed")
    return proc.stdout.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #


def _node_names(node: ast.stmt) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    if isinstance(node, (ast.If, ast.Try, ast.With)):
        names: set[str] = set()
        for child in ast.walk(node):
            if child is not node and isinstance(child, ast.stmt):
                names |= _node_names(child)
        return names
    return set()


def _used_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _segment(lines: list[str], node: ast.stmt) -> tuple[int, int, str]:
    start = node.lineno
    for dec in getattr(node, "decorator_list", []) or []:
        start = min(start, dec.lineno)
    end = node.end_lineno or node.lineno
    return start, end, "\n".join(lines[start - 1 : end])


def _python_symbols(tree: ast.Module) -> list[str]:
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node.name)
        elif isinstance(node, ast.ClassDef):
            out.append(node.name)
            out += [f"{node.name}.{m.name}" for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return out


def extract_python_symbol(source: str, symbol: str, include_dependencies: bool = True) -> dict[str, Any] | None:
    tree = ast.parse(source)
    lines = source.splitlines()
    parts = symbol.replace("::", ".").split(".")
    scope: list[ast.stmt] = tree.body
    chain: list[ast.stmt] = []
    for part in parts:
        found = next(
            (n for n in scope if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part),
            None,
        )
        if found is None:
            return None
        chain.append(found)
        scope = found.body if isinstance(found, ast.ClassDef) else []
    top = chain[0]
    target = chain[-1]
    if isinstance(target, ast.ClassDef) and len(chain) == 1:
        entry = symbol
    else:
        entry = ".".join(parts)

    included: list[ast.stmt] = [top]
    deps: list[str] = []
    if include_dependencies:
        defs: dict[str, ast.stmt] = {}
        for node in tree.body:
            for name in _node_names(node):
                defs.setdefault(name, node)
        pending = list(_used_names(top) - {top.name})
        seen = {id(top)}
        while pending:
            name = pending.pop()
            node = defs.get(name)
            if node is None or id(node) in seen:
                continue
            seen.add(id(node))
            included.append(node)
            deps.append(name)
            pending.extend(_used_names(node) - _node_names(node))
        for node in tree.body:  # `from __future__` must survive and come first
            if isinstance(node, ast.ImportFrom) and node.module == "__future__" and id(node) not in seen:
                included.append(node)
    included.sort(key=lambda n: n.lineno)
    segments = [_segment(lines, n) for n in included]
    code = "\n\n".join(seg for _, _, seg in segments) + "\n"
    s, e, _ = _segment(lines, top)
    t_start, t_end, _ = _segment(lines, target)
    return {
        "code": code,
        "entrypoint": entry,
        "line_range": [t_start, t_end],
        "dependencies": sorted(set(deps)),
        "symbol_source": "\n".join(lines[t_start - 1 : t_end]),
        "container_range": [s, e],
    }


# --------------------------------------------------------------------------- #
# C / C++
# --------------------------------------------------------------------------- #


def _cpp_symbols(defs: list[CppDef]) -> list[str]:
    return [d.qualified for d in defs if d.kind in ("function", "method", "class")]


def extract_cpp_symbol(source: str, symbol: str, include_dependencies: bool = True) -> dict[str, Any] | None:
    defs = outline(source)
    want = symbol.replace(".", "::")
    target = next((d for d in defs if d.qualified == want), None) or next(
        (d for d in defs if d.kind != "class" and (d.name == want or (d.cls and f"{d.cls}::{d.name}" == want))), None
    ) or next((d for d in defs if d.name == want), None)
    if target is None:
        return None
    pieces: dict[int, tuple[int, int]] = {}

    def add_span(start: int, end: int) -> None:
        pieces[start] = (start, end)

    container = target
    if target.kind == "method":
        cls_def = next((d for d in defs if d.kind == "class" and d.name == target.cls and d.start <= target.start < d.end), None)
        if cls_def is None:
            cls_def = next((d for d in defs if d.kind == "class" and d.name == target.cls), None)
            add_span(target.start, target.end)  # out-of-class definition
        if cls_def is not None:
            container = cls_def
    add_span(container.start, container.end)
    deps: list[str] = []
    if include_dependencies:
        named = {d.name: d for d in defs if d.kind in ("function", "class") and d is not container}
        stmts = toplevel_statements(source)
        pending = list(identifiers(source[container.start : container.end]))
        if target is not container:
            pending += list(identifiers(source[target.start : target.end]))
        seen: set[str] = {container.name}
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            d = named.get(name)
            if d is not None and not any(s <= d.start < e for s, e in pieces.values()):
                add_span(d.start, d.end)
                deps.append(name)
                pending.extend(identifiers(source[d.start : d.end]))
            for s, e, names in stmts:
                if name in names and not any(ps <= s < pe for ps, pe in pieces.values()):
                    add_span(s, e)
                    deps.append(name)
                    pending.extend(identifiers(source[s:e]))
    header = include_lines(source)
    body = "\n\n".join(source[s:e].strip("\n") for s, e in sorted(pieces.values()))
    code = ("\n".join(header) + "\n\n" if header else "") + body + "\n"
    line_of = lambda pos: source.count("\n", 0, pos) + 1  # noqa: E731
    entry = target.qualified if target.kind != "class" else target.name
    return {
        "code": code,
        "entrypoint": entry,
        "line_range": [line_of(target.start), line_of(target.end)],
        "dependencies": sorted(set(deps)),
        "symbol_source": source[target.start : target.end],
    }


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def extract_git_oracle(
    file_path: str,
    symbol_name: str,
    git_ref: str = "HEAD",
    *,
    repo_path: str | None = None,
    include_dependencies: bool = True,
) -> dict[str, Any]:
    """Extract ``symbol_name`` from ``file_path`` as it existed at ``git_ref``."""
    base = Path(repo_path).resolve() if repo_path else Path.cwd()
    path = Path(file_path)
    if not path.is_absolute():
        path = (base / path).resolve()
    probe = path.parent if path.parent.exists() else base
    try:
        root = Path(_git(["rev-parse", "--show-toplevel"], probe).strip())
        rel = Path(os.path.relpath(path, root)).as_posix()
        if rel.startswith("../"):
            return {"status": "ERROR", "error": f"{file_path} is outside the git repository at {root}"}
        commit = _git(["rev-parse", "--verify", f"{git_ref}^{{commit}}"], root).strip()
        try:
            source = _git(["show", f"{commit}:{rel}"], root)
        except GitError as exc:
            return {"status": "ERROR", "error": f"{rel} does not exist at {git_ref} ({commit[:10]}): {exc}"}
        log = _git(["log", "-1", "--format=%h%x09%s%x09%an%x09%ad", "--date=short", commit], root).strip().split("\t")
    except GitError as exc:
        return {"status": "ERROR", "error": str(exc)}

    suffix = path.suffix.lower()
    if suffix in PY_SUFFIXES:
        language = "python"
        try:
            found = extract_python_symbol(source, symbol_name, include_dependencies)
        except SyntaxError as exc:
            return {"status": "ERROR", "error": f"{rel} at {git_ref} does not parse: {exc}"}
        available = _python_symbols(ast.parse(source)) if found is None else []
    elif suffix in CPP_SUFFIXES:
        language = "cpp"
        found = extract_cpp_symbol(source, symbol_name, include_dependencies)
        available = _cpp_symbols(outline(source)) if found is None else []
    else:
        return {
            "status": "ERROR",
            "error": f"unsupported file type {suffix!r}: symbol extraction supports Python and C/C++ files",
        }
    if found is None:
        return {
            "status": "ERROR",
            "error": f"symbol {symbol_name!r} not found in {rel} at {git_ref}",
            "available_symbols": available[:100],
        }

    result: dict[str, Any] = {
        "status": "OK",
        "language": language,
        "oracle_code": found["code"],
        "entrypoint": found["entrypoint"],
        "symbol": symbol_name,
        "file_path": rel,
        "git_ref": git_ref,
        "commit": commit,
        "commit_summary": {"short": log[0], "subject": log[1] if len(log) > 1 else "", "author": log[2] if len(log) > 2 else "", "date": log[3] if len(log) > 3 else ""},
        "line_range": found["line_range"],
        "dependencies": found["dependencies"],
    }
    if path.exists():
        try:
            current_source = path.read_text(encoding="utf-8")
            current = (
                extract_python_symbol(current_source, symbol_name, False)
                if language == "python"
                else extract_cpp_symbol(current_source, symbol_name, False)
            )
        except (SyntaxError, UnicodeDecodeError):
            current = None
        if current is None:
            result["changed_since_ref"] = True
            result["working_tree_note"] = "symbol is missing (or unparsable) in the working tree"
        else:
            old = found["symbol_source"].strip().splitlines()
            new = current["symbol_source"].strip().splitlines()
            changed = [ln.rstrip() for ln in old] != [ln.rstrip() for ln in new]
            result["changed_since_ref"] = changed
            if changed:
                diff = "\n".join(difflib.unified_diff(old, new, f"{rel}@{git_ref}", f"{rel} (working tree)", lineterm=""))
                result["diff_vs_working_tree"] = diff[:4000] + ("\n... (truncated)" if len(diff) > 4000 else "")
    result["next_step"] = (
        f"Pass oracle_code to run_differential_arena as oracle_code with oracle_entrypoint={found['entrypoint']!r} "
        "(and oracle_errors='fail' if raised exceptions are part of the contract), using the refactored function as candidate_code."
    )
    return result
