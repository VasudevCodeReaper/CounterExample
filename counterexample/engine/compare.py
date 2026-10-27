"""Output comparators for the differential arena.

Modes
-----
``auto``        structural equality; floats compared with a relative/absolute
                tolerance (summation order differs between brute force and
                optimized code, so exact float equality produces false alarms).
``exact``       structural equality, no float tolerance.
``strict``      Python ``==`` *and* identical types at every level.
``approx``      like ``auto`` with the caller's ``float_tolerance``.
``unordered``   the top-level sequence is compared as a multiset.
``unordered_deep``  every sequence at every depth is compared as a multiset.
``set``         top-level sequences compared as sets (duplicates ignored).

"Structural" means tuples and lists are interchangeable (so a C++ ``pair`` can
match a Python ``tuple`` or ``list``), ``NaN == NaN``, ``1 == 1.0`` and
``-0.0 == 0.0``.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable

from counterexample.codec import Opaque, encode

MODES = ("auto", "exact", "strict", "approx", "unordered", "unordered_deep", "set")

Comparator = Callable[[Any, Any], bool]


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _canon(v: Any) -> Any:
    """A JSON-able canonical form used for hashing/sorting nested values."""
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, int):
        return float(v) if abs(v) < 2**53 else v
    if isinstance(v, float):
        if math.isnan(v):
            return "__nan__"
        if math.isinf(v):
            return "__inf__" if v > 0 else "__-inf__"
        return 0.0 if v == 0 else v
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return {"__set__": sorted((_canon(x) for x in v), key=_key)}
    if isinstance(v, dict):
        return {"__dict__": sorted(([_canon(k), _canon(x)] for k, x in v.items()), key=_key)}
    if isinstance(v, Opaque):
        return {"__opaque__": [v.kind, v.payload]}
    return encode(v)


def _key(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def canonical_key(v: Any) -> str:
    return _key(_canon(v))


def make_comparator(mode: str = "auto", float_tolerance: float = 1e-9) -> Comparator:
    if mode not in MODES:
        raise ValueError(f"unknown comparator {mode!r}; choose from {', '.join(MODES)}")
    tol = 0.0 if mode in ("exact", "strict") else float(float_tolerance)
    strict = mode == "strict"
    deep_unordered = mode == "unordered_deep"

    def floats_equal(a: float, b: float) -> bool:
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        if a == b:
            return True
        if tol <= 0 or math.isinf(a) or math.isinf(b):
            return False
        return math.isclose(a, b, rel_tol=tol, abs_tol=tol)

    def multiset_equal(a: list[Any], b: list[Any]) -> bool:
        if len(a) != len(b):
            return False
        sa = sorted(a, key=canonical_key)
        sb = sorted(b, key=canonical_key)
        if all(eq(x, y) for x, y in zip(sa, sb)):
            return True
        # Tolerant fallback for floats whose canonical keys sort differently.
        remaining = list(b)
        for x in a:
            for i, y in enumerate(remaining):
                if eq(x, y):
                    del remaining[i]
                    break
            else:
                return False
        return True

    def eq(a: Any, b: Any) -> bool:
        if strict and type(a) is not type(b):
            return False
        if _num(a) and _num(b):
            if isinstance(a, float) or isinstance(b, float):
                return floats_equal(float(a), float(b))
            return a == b
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            if deep_unordered:
                return multiset_equal(list(a), list(b))
            return len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
        if isinstance(a, (set, frozenset)) and isinstance(b, (set, frozenset)):
            if len(a) != len(b):
                return False
            if a == b:
                return True
            return multiset_equal(list(a), list(b))
        if isinstance(a, dict) and isinstance(b, dict):
            if len(a) != len(b):
                return False
            for k, v in a.items():
                if k in b:
                    if not eq(v, b[k]):
                        return False
                    continue
                # Keys may differ only by representation (1 vs 1.0, tuple vs list).
                ck = canonical_key(k)
                match = next((bk for bk in b if canonical_key(bk) == ck), None)
                if match is None or not eq(v, b[match]):
                    return False
            return True
        try:
            return bool(a == b)
        except Exception:  # noqa: BLE001 - exotic __eq__
            return canonical_key(a) == canonical_key(b)

    if mode in ("unordered", "set"):

        def top(a: Any, b: Any) -> bool:
            if isinstance(a, (list, tuple, set, frozenset)) and isinstance(b, (list, tuple, set, frozenset)):
                la, lb = list(a), list(b)
                if mode == "set":
                    da = {canonical_key(x): x for x in la}
                    db = {canonical_key(x): x for x in lb}
                    return multiset_equal(list(da.values()), list(db.values()))
                return multiset_equal(la, lb)
            return eq(a, b)

        return top
    return eq
