"""Lossless value transport between the engine, sandbox workers and agents.

JSON cannot carry tuples, sets, NaN/Inf, bytes or dicts with non-string keys,
yet those are exactly the values adversarial generators love to produce.  The
codec wraps them in single-key ``{"$tag": ...}`` objects so every value makes
the round trip ``decode(encode(v)) == v``.

The same module renders values as source-code literals (Python, JavaScript,
C++) for :mod:`counterexample.engine.regression`.

NOTE: ``counterexample/adapters/_python_worker.py`` carries a private copy of
``encode``/``decode`` because the sandbox worker must run with ``python -I``
and import nothing from this package.  Keep the two in sync.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import math
import reprlib
from fractions import Fraction
from typing import Any

MAX_DEPTH = 200


class Opaque:
    """Stand-in for a value that has no faithful JSON form (e.g. a custom object).

    Two opaque values compare equal when their kind and payload match, which
    keeps oracle/candidate comparisons meaningful for objects with ``__dict__``.
    """

    __slots__ = ("kind", "payload", "text")

    def __init__(self, kind: str, payload: Any, text: str):
        self.kind = kind
        self.payload = payload
        self.text = text

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Opaque):
            return NotImplemented
        return self.kind == other.kind and self.payload == other.payload

    def __hash__(self) -> int:
        return hash((self.kind, json.dumps(self.payload, sort_keys=True, default=str)))

    def __repr__(self) -> str:
        return self.text


def _sort_key(v: Any) -> str:
    return repr(v)


def encode(value: Any, _depth: int = 0) -> Any:
    """Convert a Python value into a JSON-safe, tag-annotated structure."""
    if _depth > MAX_DEPTH:
        return {"$repr": reprlib.repr(value)}
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return {"$float": "nan"}
        if math.isinf(value):
            return {"$float": "inf" if value > 0 else "-inf"}
        return value
    d = _depth + 1
    if isinstance(value, list):
        return [encode(v, d) for v in value]
    if isinstance(value, tuple):
        return {"$tuple": [encode(v, d) for v in value]}
    if isinstance(value, dict):
        if all(isinstance(k, str) and not k.startswith("$") for k in value):
            return {k: encode(v, d) for k, v in value.items()}
        return {"$dict": [[encode(k, d), encode(v, d)] for k, v in value.items()]}
    if isinstance(value, frozenset):
        return {"$frozenset": [encode(v, d) for v in sorted(value, key=_sort_key)]}
    if isinstance(value, set):
        return {"$set": [encode(v, d) for v in sorted(value, key=_sort_key)]}
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, complex):
        return {"$complex": [encode(value.real, d), encode(value.imag, d)]}
    if isinstance(value, Fraction):
        return {"$fraction": [value.numerator, value.denominator]}
    if isinstance(value, Opaque):
        return {"$" + value.kind: value.payload, "$text": value.text}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: encode(getattr(value, f.name), d) for f in dataclasses.fields(value)}
        return {"$obj": type(value).__name__, "fields": fields}
    tolist = getattr(value, "tolist", None)
    if callable(tolist):  # numpy arrays / scalars
        try:
            return encode(tolist(), d)
        except Exception:
            pass
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict) and not callable(value):
        fields = {str(k): encode(v, d) for k, v in attrs.items() if not str(k).startswith("_")}
        return {"$obj": type(value).__name__, "fields": fields}
    return {"$repr": reprlib.repr(value)}


def decode(obj: Any) -> Any:
    """Inverse of :func:`encode`."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, list):
        return [decode(v) for v in obj]
    if isinstance(obj, dict):
        if len(obj) >= 1:
            tag = next(iter(obj))
            if isinstance(tag, str) and tag.startswith("$"):
                body = obj[tag]
                if tag == "$float":
                    return float(body)
                if tag == "$tuple":
                    return tuple(decode(v) for v in body)
                if tag == "$dict":
                    return {_hashable(decode(k)): decode(v) for k, v in body}
                if tag == "$set":
                    return {_hashable(decode(v)) for v in body}
                if tag == "$frozenset":
                    return frozenset(_hashable(decode(v)) for v in body)
                if tag == "$bytes":
                    return base64.b64decode(body)
                if tag == "$complex":
                    return complex(decode(body[0]), decode(body[1]))
                if tag == "$fraction":
                    return Fraction(body[0], body[1])
                if tag == "$obj":
                    fields = {k: decode(v) for k, v in obj.get("fields", {}).items()}
                    text = f"{body}({', '.join(f'{k}={v!r}' for k, v in fields.items())})"
                    return Opaque("obj", [body, obj.get("fields", {})], text)
                if tag == "$repr":
                    return Opaque("repr", body, body)
        return {k: decode(v) for k, v in obj.items()}
    return obj


def _hashable(v: Any) -> Any:
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted(((_hashable(k), _hashable(x)) for k, x in v.items()), key=repr))
    if isinstance(v, set):
        return frozenset(v)
    return v


def dumps(value: Any) -> str:
    return json.dumps(encode(value), separators=(",", ":"), allow_nan=False)


def loads(text: str) -> Any:
    return decode(json.loads(text))


# --------------------------------------------------------------------------- #
# Size metrics & human-readable rendering
# --------------------------------------------------------------------------- #


def value_size(value: Any, _depth: int = 0) -> int:
    """Rough "N" of a value: atoms are 1, collections are the sum of their parts."""
    if _depth > MAX_DEPTH:
        return 1
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, dict):
        return sum(value_size(k, _depth + 1) + value_size(v, _depth + 1) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return sum(value_size(v, _depth + 1) for v in value)
    return 1


def short_repr(value: Any, limit: int = 400) -> str:
    text = py_literal(value)
    if len(text) <= limit:
        return text
    return text[: limit - 15] + f" ...<{len(text) - limit + 15} more chars>"


# --------------------------------------------------------------------------- #
# Source-code literals
# --------------------------------------------------------------------------- #


def py_literal(value: Any) -> str:
    """Render ``value`` as a Python expression that evaluates back to it."""
    if isinstance(value, float):
        if math.isnan(value):
            return "float('nan')"
        if math.isinf(value):
            return "float('inf')" if value > 0 else "float('-inf')"
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(py_literal(v) for v in value) + "]"
    if isinstance(value, tuple):
        if len(value) == 1:
            return "(" + py_literal(value[0]) + ",)"
        return "(" + ", ".join(py_literal(v) for v in value) + ")"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{py_literal(k)}: {py_literal(v)}" for k, v in value.items()) + "}"
    if isinstance(value, frozenset):
        return "frozenset(" + (py_literal(set(value)) if value else "") + ")"
    if isinstance(value, set):
        if not value:
            return "set()"
        return "{" + ", ".join(py_literal(v) for v in sorted(value, key=_sort_key)) + "}"
    if isinstance(value, complex):
        return f"complex({py_literal(value.real)}, {py_literal(value.imag)})"
    if isinstance(value, Fraction):
        return f"Fraction({value.numerator}, {value.denominator})"
    return repr(value)


def js_literal(value: Any) -> str:
    """Render ``value`` as a JavaScript expression (for jest tests)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            return f"{value}n"
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(js_literal(v) for v in value) + "]"
    if isinstance(value, (set, frozenset)):
        return "new Set([" + ", ".join(js_literal(v) for v in sorted(value, key=_sort_key)) + "])"
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return "{" + ", ".join(f"{json.dumps(k)}: {js_literal(v)}" for k, v in value.items()) + "}"
        return "new Map([" + ", ".join(f"[{js_literal(k)}, {js_literal(v)}]" for k, v in value.items()) + "])"
    if isinstance(value, (bytes, bytearray)):
        return "Buffer.from(" + json.dumps(base64.b64encode(bytes(value)).decode()) + ", 'base64')"
    return json.dumps(repr(value))


def infer_cpp_type(value: Any) -> str:
    """Best-effort C++ type for a Python value (used when no signature is known)."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "long long"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "std::string"
    if isinstance(value, tuple) and len(value) == 2:
        return f"std::pair<{infer_cpp_type(value[0])}, {infer_cpp_type(value[1])}>"
    if isinstance(value, tuple):
        return "std::tuple<" + ", ".join(infer_cpp_type(v) for v in value) + ">"
    if isinstance(value, list):
        inner = _unify_cpp([infer_cpp_type(v) for v in value]) if value else "long long"
        return f"std::vector<{inner}>"
    if isinstance(value, (set, frozenset)):
        inner = _unify_cpp([infer_cpp_type(v) for v in value]) if value else "long long"
        return f"std::set<{inner}>"
    if isinstance(value, dict):
        if not value:
            return "std::map<long long, long long>"
        k = _unify_cpp([infer_cpp_type(x) for x in value])
        v = _unify_cpp([infer_cpp_type(x) for x in value.values()])
        return f"std::map<{k}, {v}>"
    return "auto"


def _unify_cpp(types: list[str]) -> str:
    uniq = set(types)
    if len(uniq) == 1:
        return types[0]
    if uniq <= {"long long", "double", "bool"}:
        return "double"
    return types[0]


def cpp_literal(value: Any) -> str:
    """Render ``value`` as a C++ brace initializer / expression."""
    if value is None:
        return "{}"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if -(2**31) <= value < 2**31:
            return str(value)
        if -(2**63) <= value < 2**63:
            return f"{value}LL" if value != -(2**63) else "(-9223372036854775807LL - 1)"
        return f"{value}ULL"
    if isinstance(value, float):
        if math.isnan(value):
            return "std::numeric_limits<double>::quiet_NaN()"
        if math.isinf(value):
            return ("" if value > 0 else "-") + "std::numeric_limits<double>::infinity()"
        text = repr(value)
        return text if ("." in text or "e" in text) else text + ".0"
    if isinstance(value, str):
        return "std::string(" + json.dumps(value) + ")"
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(cpp_literal(v) for v in value) + "}"
    if isinstance(value, (set, frozenset)):
        return "{" + ", ".join(cpp_literal(v) for v in sorted(value, key=_sort_key)) + "}"
    if isinstance(value, dict):
        return "{" + ", ".join("{" + cpp_literal(k) + ", " + cpp_literal(v) + "}" for k, v in value.items()) + "}"
    return "/* unsupported: " + repr(value).replace("*/", "* /") + " */ {}"
