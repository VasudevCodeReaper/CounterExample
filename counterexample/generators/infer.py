"""Infer a generator spec from a function signature when the agent omits one.

Python annotations (``def f(nums: list[int], k: int) -> int``) and C++ parameter
types (``int f(vector<int> nums, int k)``) both map onto the same JSON specs.
"""

from __future__ import annotations

import ast
import re
from typing import Any

from counterexample.generators.base import SpecError


def _py_annotation(node: ast.expr | None, where: str) -> dict[str, Any]:
    if node is None:
        raise SpecError(f"cannot infer generator for {where}: parameter has no type annotation")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _py_annotation(ast.parse(node.value, mode="eval").body, where)
        except SyntaxError as exc:
            raise SpecError(f"bad string annotation for {where}: {node.value!r}") from exc
    if isinstance(node, ast.Constant) and node.value is None:
        return {"type": "const", "value": None}
    if isinstance(node, ast.Name) or isinstance(node, ast.Attribute):
        name = node.id if isinstance(node, ast.Name) else node.attr
        simple = {
            "int": {"type": "int"},
            "float": {"type": "float"},
            "bool": {"type": "bool"},
            "str": {"type": "str"},
            "bytes": {"type": "str"},
            "list": {"type": "list"},
            "List": {"type": "list"},
            "dict": {"type": "dict"},
            "Dict": {"type": "dict"},
            "set": {"type": "set"},
            "Set": {"type": "set"},
        }
        if name in simple:
            return dict(simple[name])
        raise SpecError(f"cannot infer generator for {where}: unsupported annotation {name!r}")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _py_annotation(node.left, where)
        right = _py_annotation(node.right, where)
        if right == {"type": "const", "value": None}:
            return {"type": "optional", "of": left}
        if left == {"type": "const", "value": None}:
            return {"type": "optional", "of": right}
        return {"type": "one_of", "options": [left, right]}
    if isinstance(node, ast.Subscript):
        base = node.value.id if isinstance(node.value, ast.Name) else getattr(node.value, "attr", "")
        sl = node.slice
        params = list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]
        if base in ("list", "List", "Sequence", "Iterable", "deque"):
            return {"type": "list", "elements": _py_annotation(params[0], where)}
        if base in ("set", "Set", "frozenset", "FrozenSet"):
            return {"type": "set", "elements": _py_annotation(params[0], where)}
        if base in ("dict", "Dict", "Mapping"):
            return {"type": "dict", "keys": _py_annotation(params[0], where), "values": _py_annotation(params[1], where)}
        if base in ("tuple", "Tuple"):
            if len(params) == 2 and isinstance(params[1], ast.Constant) and params[1].value is Ellipsis:
                return {"type": "list", "elements": _py_annotation(params[0], where)}
            return {"type": "tuple", "items": [_py_annotation(p, where) for p in params]}
        if base == "Optional":
            return {"type": "optional", "of": _py_annotation(params[0], where)}
        if base == "Union":
            return {"type": "one_of", "options": [_py_annotation(p, where) for p in params]}
        raise SpecError(f"cannot infer generator for {where}: unsupported generic {base!r}")
    raise SpecError(f"cannot infer generator for {where}: unsupported annotation {ast.unparse(node)!r}")


def spec_from_python(code: str, entrypoint: str) -> dict[str, Any]:
    """Build ``{"args": [...]}`` from the annotations of ``entrypoint`` in ``code``."""
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == entrypoint:
            params = [a for a in node.args.posonlyargs + node.args.args if a.arg not in ("self", "cls")]
            n_defaults = len(node.args.defaults)
            required = params[: len(params) - n_defaults] if n_defaults else params
            return {"args": [_py_annotation(a.annotation, f"{entrypoint}({a.arg})") for a in required]}
    raise SpecError(f"function {entrypoint!r} not found while inferring a generator spec")


# --------------------------------------------------------------------------- #
# C++
# --------------------------------------------------------------------------- #

_CPP_INTS = {
    "int": 32, "signed": 32, "long": 64, "long int": 64, "long long": 64, "long long int": 64,
    "int32_t": 32, "int64_t": 64, "short": 16, "int16_t": 16, "int8_t": 8,
}
_CPP_UINTS = {"unsigned": 32, "unsigned int": 32, "uint32_t": 32, "size_t": 64, "uint64_t": 64,
              "unsigned long long": 64, "unsigned long": 64, "uint8_t": 8, "uint16_t": 16}


def split_template_args(text: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def normalize_cpp_type(t: str) -> str:
    t = re.sub(r"\bconst\b|\bvolatile\b|&|\*", " ", t)
    t = t.replace("std::", "")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*<\s*", "<", t)
    t = re.sub(r"\s*>\s*", ">", t)
    t = re.sub(r"\s*,\s*", ", ", t)
    return t


def spec_from_cpp_type(cpp_type: str) -> dict[str, Any]:
    t = normalize_cpp_type(cpp_type)
    if t in _CPP_INTS:
        # 64-bit parameters still default to the 32-bit range so that naive
        # sums inside the oracle do not overflow; widen explicitly if needed.
        return {"type": "int", "bits": 32}
    if t in _CPP_UINTS:
        return {"type": "int", "min": 0, "max": 2**32 - 1}
    if t in ("double", "float", "long double"):
        return {"type": "float"}
    if t == "bool":
        return {"type": "bool"}
    if t == "char":
        return {"type": "choice", "values": list("abcxyz019 ")}
    if t == "string":
        return {"type": "str", "alphabet": "ascii"}
    m = re.fullmatch(r"(\w+)<(.*)>", t)
    if m:
        name, inner = m.group(1), split_template_args(m.group(2))
        if name in ("vector", "deque", "list"):
            return {"type": "list", "elements": spec_from_cpp_type(inner[0])}
        if name in ("set", "unordered_set", "multiset"):
            return {"type": "set", "elements": spec_from_cpp_type(inner[0])}
        if name in ("map", "unordered_map"):
            return {"type": "dict", "keys": spec_from_cpp_type(inner[0]), "values": spec_from_cpp_type(inner[1])}
        if name in ("pair", "tuple"):
            return {"type": "tuple", "items": [spec_from_cpp_type(x) for x in inner]}
        if name == "optional":
            return {"type": "optional", "of": spec_from_cpp_type(inner[0])}
        if name == "array":
            return {"type": "list", "elements": spec_from_cpp_type(inner[0]), "min_len": int(inner[1]), "max_len": int(inner[1])}
    raise SpecError(f"cannot infer a generator for C++ type {cpp_type!r}; pass generator_spec explicitly")
