"""Primitive and combinator generators: ints, floats (NaN/Inf), Unicode strings,
lists, tuples, dicts, sets, permutations, choices and unions.

Boundary conditions are on by default: ``0``, ``-1``, empty and single-element
collections, duplicate values, ``INT_MIN``/``INT_MAX`` and skewed (small,
sorted, reverse-sorted, low-cardinality) distributions.
"""

from __future__ import annotations

import math
import random
import string
from typing import Any, Iterator

from counterexample.generators.base import (
    Generator,
    SpecError,
    build,
    parse_range,
    register,
    remove_chunks,
    shrink_int_toward,
    sized_count,
)

# --------------------------------------------------------------------------- #
# Scalars
# --------------------------------------------------------------------------- #


@register("int", "integer")
class IntGen(Generator):
    """Integers. Defaults to the full 32-bit range so INT_MIN/INT_MAX are hit.

    Options: ``min``, ``max``, ``bits`` (8/16/32/64), ``unsigned``.
    """

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        bits = int(spec.get("bits", 32))
        if spec.get("unsigned"):
            d_lo, d_hi = 0, 2**bits - 1
        else:
            d_lo, d_hi = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
        self.lo = int(spec.get("min", d_lo))
        self.hi = int(spec.get("max", d_hi))
        if self.lo > self.hi:
            raise SpecError(f"int min {self.lo} exceeds max {self.hi}")
        self.target = min(max(0, self.lo), self.hi)
        bounds = [self.target, -1, 1, self.lo, self.hi, self.lo + 1, self.hi - 1, 2, -2]
        self.bounds: list[int] = []
        for b in bounds:
            if self.lo <= b <= self.hi and b not in self.bounds:
                self.bounds.append(b)

    def generate(self, rng: random.Random, size: float) -> int:
        lo, hi = self.lo, self.hi
        r = rng.random()
        if r < 0.18:
            return rng.choice(self.bounds)
        if r < 0.6:
            w = 3 + int(max(0.0, size) * 60)
            a, b = max(lo, -w), min(hi, w)
            if a <= b:
                return rng.randint(a, b)
            return rng.randint(lo, min(hi, lo + 2 * w)) if lo > 0 else rng.randint(max(lo, hi - 2 * w), hi)
        if r < 0.85:
            span_bits = max(1, (hi - lo).bit_length())
            mag = rng.getrandbits(rng.randint(1, span_bits))
            v = mag if rng.random() < 0.5 else -mag
            if lo <= v <= hi:
                return v
            return lo + (mag % (hi - lo + 1))
        return rng.randint(lo, hi)

    def edge_cases(self) -> list[int]:
        return list(self.bounds)

    def scaled(self, rng: random.Random, n: int) -> int:
        # Only used when the argument is marked "scale": true (e.g. fib(n)).
        return min(max(n, self.lo), self.hi)

    def shrink(self, value: Any) -> Iterator[int]:
        if not isinstance(value, int) or isinstance(value, bool):
            return
        if value < 0 and self.lo <= -value <= self.hi and -value != value:
            yield -value
        for c in shrink_int_toward(value, self.target):
            if self.lo <= c <= self.hi:
                yield c

    def validate(self, value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and self.lo <= value <= self.hi


@register("float", "double")
class FloatGen(Generator):
    """Floats. Options: ``min``, ``max``, ``allow_nan``, ``allow_inf``, ``special``
    (shortcut enabling both NaN and +/-Inf)."""

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.lo = float(spec.get("min", -1e6))
        self.hi = float(spec.get("max", 1e6))
        if self.lo > self.hi:
            raise SpecError(f"float min {self.lo} exceeds max {self.hi}")
        special = bool(spec.get("special", False))
        self.allow_nan = bool(spec.get("allow_nan", special))
        self.allow_inf = bool(spec.get("allow_inf", special))
        self.target = min(max(0.0, self.lo), self.hi)
        cands = [self.target, 1.0, -1.0, 0.5, -0.0, 0.1, 1e-9, -1e-9, self.lo, self.hi, 2.5]
        self.bounds: list[float] = []
        for c in cands:
            if self.lo <= c <= self.hi and not any(c == b and math.copysign(1, c) == math.copysign(1, b) for b in self.bounds):
                self.bounds.append(c)
        if self.allow_nan:
            self.bounds.append(float("nan"))
        if self.allow_inf:
            self.bounds.extend([float("inf"), float("-inf")])

    def generate(self, rng: random.Random, size: float) -> float:
        r = rng.random()
        if r < 0.15:
            return rng.choice(self.bounds)
        if r < 0.5:
            a, b = max(self.lo, -10.0), min(self.hi, 10.0)
            if a > b:
                a, b = self.lo, self.hi
            v = rng.uniform(a, b)
            return round(v, rng.choice([0, 1, 2, 6])) if rng.random() < 0.6 else v
        if r < 0.8:
            return rng.uniform(self.lo, self.hi)
        top = math.log10(max(abs(self.lo), abs(self.hi), 1e-300))
        v = 10 ** rng.uniform(-12, top)
        v = v if rng.random() < 0.5 else -v
        return min(max(v, self.lo), self.hi)

    def edge_cases(self) -> list[float]:
        return list(self.bounds)

    def shrink(self, value: Any) -> Iterator[float]:
        if not isinstance(value, float):
            return
        if math.isnan(value) or math.isinf(value):
            yield self.target
            if self.lo <= 1.0 <= self.hi:
                yield 1.0
            return
        if value == self.target:
            return
        yield self.target
        if value < 0 and self.lo <= -value <= self.hi:
            yield -value
        if abs(value) < 2**53:
            t = float(math.trunc(value))
            if t != value and self.lo <= t <= self.hi:
                yield t
            for digits in (1, 2, 3):
                r = round(value, digits)
                if r != value and self.lo <= r <= self.hi:
                    yield r
        half = value / 2
        if abs(half) > 1e-6 and self.lo <= half <= self.hi:
            yield half

    def validate(self, value: Any) -> bool:
        if not isinstance(value, float):
            return False
        if math.isnan(value):
            return self.allow_nan
        if math.isinf(value):
            return self.allow_inf
        return self.lo <= value <= self.hi


@register("bool", "boolean")
class BoolGen(Generator):
    def generate(self, rng: random.Random, size: float) -> bool:
        return rng.random() < 0.5

    def edge_cases(self) -> list[bool]:
        return [False, True]

    def shrink(self, value: Any) -> Iterator[bool]:
        if value is True:
            yield False

    def validate(self, value: Any) -> bool:
        return isinstance(value, bool)


# Tricky Unicode: combining marks, astral emoji, zero-width & bidi controls,
# case-folding traps (ß, İ, final sigma), compatibility forms and NUL.
UNICODE_POOL = [
    "a", "b", "Z", "0", " ", "é", "é", "ß", "İ", "Σ", "ς",
    "\U0001F600", "\U0001F468‍\U0001F469", "​", "‮", "﻿", "\x00",
    "\t", "\n", "\r", "ﬁ", "Å", "Å", "١", "Ⅻ", "　",
    "中", "नि", "\U00010348", "\\", "\"", "'", "%s", "<", "&",
]

ALPHABETS = {
    "ascii": string.ascii_letters + string.digits + string.punctuation + " ",
    "printable": string.printable,
    "lower": string.ascii_lowercase,
    "upper": string.ascii_uppercase,
    "letters": string.ascii_letters,
    "digits": string.digits,
    "alnum": string.ascii_letters + string.digits,
    "hex": "0123456789abcdef",
    "binary": "01",
    "ab": "ab",
    "abc": "abc",
    "whitespace": " \t\n\r",
    "brackets": "()[]{}",
}


@register("str", "string", "unicode", "text")
class StrGen(Generator):
    """Strings. Options: ``min_len``, ``max_len``, ``alphabet`` (a named alphabet
    such as ``lower``/``ascii``/``binary``/``unicode`` or a literal set of chars)."""

    scalable = True

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.min_len = int(spec.get("min_len", 0))
        self.max_len = int(spec.get("max_len", 20))
        if self.min_len > self.max_len:
            raise SpecError("str min_len exceeds max_len")
        alpha = spec.get("alphabet", "unicode" if spec.get("type") == "unicode" else "ascii")
        if isinstance(alpha, list):
            self.pool = [str(a) for a in alpha]
        elif alpha == "unicode":
            self.pool = list(UNICODE_POOL)
        elif alpha in ALPHABETS:
            self.pool = list(ALPHABETS[alpha])
        elif isinstance(alpha, str) and alpha:
            self.pool = list(alpha)
        else:
            raise SpecError(f"invalid alphabet {alpha!r}")
        self.simplest = self.pool[0]

    def _make(self, rng: random.Random, n: int) -> str:
        pool = self.pool
        if len(pool) > 2 and rng.random() < 0.5:
            # Low-cardinality strings expose repetition/palindrome/collision bugs.
            pool = rng.sample(pool, rng.randint(1, min(3, len(pool))))
        out = ""
        while len(out) < n:
            out += rng.choice(pool)
        # Pool items may be multi-codepoint (combining marks, ZWJ emoji); cutting
        # through one is itself a useful adversarial case.
        return out[:n]

    def generate(self, rng: random.Random, size: float) -> str:
        return self._make(rng, sized_count(rng, self.min_len, self.max_len, size))

    def scaled(self, rng: random.Random, n: int) -> str:
        return self._make(rng, max(self.min_len, n))

    def edge_cases(self) -> list[str]:
        p = self.pool
        cands = ["", p[0], " ", p[0] * 2]
        if len(p) > 1:
            cands += [p[0] + p[1], p[0] + p[1] + p[0], p[1] + p[0]]
        if self.spec.get("alphabet") == "unicode" or self.spec.get("type") == "unicode":
            cands += ["é", "\U0001F600", "​", "ß"]
        out = []
        for c in cands:
            if self.min_len <= len(c) <= self.max_len and c not in out:
                out.append(c)
        if not out:
            out.append(p[0] * self.min_len)
        return out

    def shrink(self, value: Any) -> Iterator[str]:
        if not isinstance(value, str):
            return
        chars = list(value)
        for cand in remove_chunks(chars, self.min_len):
            yield "".join(cand)
        for i, ch in enumerate(chars):
            if ch != self.simplest:
                yield value[:i] + self.simplest + value[i + 1 :]

    def validate(self, value: Any) -> bool:
        return isinstance(value, str) and self.min_len <= len(value) <= self.max_len


@register("const", "just")
class ConstGen(Generator):
    def generate(self, rng: random.Random, size: float) -> Any:
        return self.spec.get("value")

    def edge_cases(self) -> list[Any]:
        return [self.spec.get("value")]

    def validate(self, value: Any) -> bool:
        return value == self.spec.get("value")


@register("choice", "sampled_from", "enum")
class ChoiceGen(Generator):
    """Pick from ``values``; shrinks toward earlier entries."""

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.values = list(spec.get("values", []))
        if not self.values:
            raise SpecError("choice needs a non-empty 'values' list")

    def generate(self, rng: random.Random, size: float) -> Any:
        return rng.choice(self.values)

    def edge_cases(self) -> list[Any]:
        return list(self.values)

    def shrink(self, value: Any) -> Iterator[Any]:
        for v in self.values:
            if v == value:
                return
            yield v

    def validate(self, value: Any) -> bool:
        return value in self.values


@register("one_of", "union")
class OneOfGen(Generator):
    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        options = spec.get("options") or spec.get("of")
        if not options:
            raise SpecError("one_of needs a non-empty 'options' list")
        self.options = [build(o) for o in options]
        self.scalable = any(o.scalable for o in self.options)

    def generate(self, rng: random.Random, size: float) -> Any:
        return rng.choice(self.options).generate(rng, size)

    def scaled(self, rng: random.Random, n: int) -> Any:
        scalable = [o for o in self.options if o.scalable] or self.options
        return rng.choice(scalable).scaled(rng, n)

    def edge_cases(self) -> list[Any]:
        out: list[Any] = []
        for o in self.options:
            out.extend(o.edge_cases()[:4])
        return out

    def shrink(self, value: Any) -> Iterator[Any]:
        for idx, o in enumerate(self.options):
            if o.validate(value):
                for earlier in self.options[:idx]:
                    edges = earlier.edge_cases()
                    if edges:
                        yield edges[0]
                yield from o.shrink(value)
                return

    def validate(self, value: Any) -> bool:
        return any(o.validate(value) for o in self.options)


@register("optional", "nullable")
class OptionalGen(Generator):
    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.inner = build(spec.get("of", {"type": "int"}))
        self.null_prob = float(spec.get("null_prob", 0.2))
        self.scalable = self.inner.scalable

    def generate(self, rng: random.Random, size: float) -> Any:
        return None if rng.random() < self.null_prob else self.inner.generate(rng, size)

    def scaled(self, rng: random.Random, n: int) -> Any:
        return self.inner.scaled(rng, n)

    def edge_cases(self) -> list[Any]:
        return [None] + self.inner.edge_cases()

    def shrink(self, value: Any) -> Iterator[Any]:
        if value is None:
            return
        yield None
        yield from self.inner.shrink(value)

    def validate(self, value: Any) -> bool:
        return value is None or self.inner.validate(value)


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #


def _sort_maybe(values: list[Any], order: Any) -> list[Any]:
    if not order:
        return values
    try:
        return sorted(values, reverse=(order == "desc"))
    except TypeError:
        return values


@register("list", "array", "vector")
class ListGen(Generator):
    """Lists. Options: ``elements`` (spec, default int), ``min_len``, ``max_len``
    (default 50), ``unique``, ``sorted`` (true/"asc"/"desc"), ``dup_bias``."""

    scalable = True

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.elem = build(spec.get("elements", spec.get("of", {"type": "int"})))
        self.min_len = int(spec.get("min_len", 0))
        self.max_len = int(spec.get("max_len", 50))
        if self.min_len > self.max_len:
            raise SpecError("list min_len exceeds max_len")
        self.unique = bool(spec.get("unique", False))
        self.order = spec.get("sorted", False)
        self.dup_bias = float(spec.get("dup_bias", 0.25))

    def _fill(self, rng: random.Random, n: int, size: float) -> list[Any]:
        if self.unique:
            out: list[Any] = []
            seen: set[str] = set()
            attempts = 0
            while len(out) < n and attempts < n * 20 + 50:
                attempts += 1
                v = self.elem.generate(rng, size)
                key = repr(v)
                if key not in seen:
                    seen.add(key)
                    out.append(v)
            return out
        if n >= 2 and rng.random() < self.dup_bias:
            pool = [self.elem.generate(rng, size) for _ in range(rng.randint(1, min(3, n)))]
            return [rng.choice(pool) for _ in range(n)]
        return [self.elem.generate(rng, size) for _ in range(n)]

    def _arrange(self, rng: random.Random, values: list[Any]) -> list[Any]:
        if self.order:
            return _sort_maybe(values, self.order)
        r = rng.random()
        if r < 0.1:
            return _sort_maybe(values, "asc")
        if r < 0.15:
            return _sort_maybe(values, "desc")
        return values

    def generate(self, rng: random.Random, size: float) -> list[Any]:
        n = sized_count(rng, self.min_len, self.max_len, size)
        return self._arrange(rng, self._fill(rng, n, size))

    def scaled(self, rng: random.Random, n: int) -> list[Any]:
        values = [self.elem.generate(rng, 1.0) for _ in range(max(self.min_len, n))]
        if self.unique:
            values = self._fill(rng, max(self.min_len, n), 1.0)
        return _sort_maybe(values, self.order) if self.order else values

    def edge_cases(self) -> list[list[Any]]:
        elems = self.elem.edge_cases()[:6] or [self.elem.generate(random.Random(0), 0.0)]
        e0 = elems[0]
        cands: list[list[Any]] = [[]]
        cands += [[e] for e in elems]
        cands.append([e0, e0])
        if len(elems) > 1:
            cands.append([elems[1], e0])
            cands.append([e0, elems[1], e0])
        if self.min_len > 1:
            cands.append([e0] * self.min_len)
            cands.append([elems[i % len(elems)] for i in range(self.min_len)])
        out: list[list[Any]] = []
        for c in cands:
            if not (self.min_len <= len(c) <= self.max_len):
                continue
            if self.unique and len({repr(x) for x in c}) != len(c):
                continue
            c = _sort_maybe(c, self.order) if self.order else c
            if c not in out:
                out.append(c)
        return out

    def shrink(self, value: Any) -> Iterator[list[Any]]:
        if not isinstance(value, list):
            return
        for cand in remove_chunks(value, self.min_len):
            yield cand
        if not self.unique:
            # Shrink equal elements together so duplicate-triggered bugs survive.
            groups: dict[str, list[int]] = {}
            for i, item in enumerate(value):
                groups.setdefault(repr(item), []).append(i)
            for idxs in groups.values():
                if len(idxs) < 2:
                    continue
                for c in self.elem.shrink(value[idxs[0]]):
                    new = list(value)
                    for i in idxs:
                        new[i] = c
                    yield _sort_maybe(new, self.order) if self.order else new
        for i, item in enumerate(value):
            for c in self.elem.shrink(item):
                new = value[:i] + [c] + value[i + 1 :]
                if self.unique and any(repr(c) == repr(x) for j, x in enumerate(value) if j != i):
                    continue
                yield _sort_maybe(new, self.order) if self.order else new

    def validate(self, value: Any) -> bool:
        return isinstance(value, list) and self.min_len <= len(value) <= self.max_len


@register("tuple")
class TupleGen(Generator):
    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.items = [build(s) for s in spec.get("items", [])]
        self.scalable = any(g.scalable for g in self.items)

    def generate(self, rng: random.Random, size: float) -> tuple:
        return tuple(g.generate(rng, size) for g in self.items)

    def scaled(self, rng: random.Random, n: int) -> tuple:
        return tuple(g.scaled(rng, n) if g.scalable else g.generate(rng, 0.3) for g in self.items)

    def edge_cases(self) -> list[tuple]:
        firsts = [(g.edge_cases() or [g.generate(random.Random(0), 0.0)]) for g in self.items]
        base = [f[0] for f in firsts]
        out = [tuple(base)]
        for i, f in enumerate(firsts):
            for e in f[1:4]:
                t = list(base)
                t[i] = e
                out.append(tuple(t))
        return out

    def shrink(self, value: Any) -> Iterator[tuple]:
        if not isinstance(value, tuple) or len(value) != len(self.items):
            return
        for i, g in enumerate(self.items):
            for c in g.shrink(value[i]):
                yield value[:i] + (c,) + value[i + 1 :]

    def validate(self, value: Any) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == len(self.items)
            and all(g.validate(v) for g, v in zip(self.items, value))
        )


@register("dict", "map", "object")
class DictGen(Generator):
    scalable = True

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.keys = build(spec.get("keys", {"type": "str", "alphabet": "lower", "min_len": 1, "max_len": 3}))
        self.values = build(spec.get("values", {"type": "int"}))
        self.min_size = int(spec.get("min_size", 0))
        self.max_size = int(spec.get("max_size", 10))

    def _make(self, rng: random.Random, n: int, size: float) -> dict[Any, Any]:
        out: dict[Any, Any] = {}
        attempts = 0
        while len(out) < n and attempts < n * 20 + 50:
            attempts += 1
            k = self.keys.generate(rng, size)
            try:
                hash(k)
            except TypeError:
                continue
            out[k] = self.values.generate(rng, size)
        return out

    def generate(self, rng: random.Random, size: float) -> dict[Any, Any]:
        return self._make(rng, sized_count(rng, self.min_size, self.max_size, size), size)

    def scaled(self, rng: random.Random, n: int) -> dict[Any, Any]:
        return self._make(rng, max(self.min_size, n), 1.0)

    def edge_cases(self) -> list[dict[Any, Any]]:
        ks = self.keys.edge_cases()[:3] or ["a"]
        vs = self.values.edge_cases()[:3] or [0]
        cands = [{}] + [{ks[0]: v} for v in vs]
        if len(ks) > 1:
            cands.append({ks[0]: vs[0], ks[1]: vs[0]})
        return [c for c in cands if self.min_size <= len(c) <= self.max_size] or [self._make(random.Random(0), self.min_size, 0.0)]

    def shrink(self, value: Any) -> Iterator[dict[Any, Any]]:
        if not isinstance(value, dict):
            return
        items = list(value.items())
        for cand in remove_chunks(items, self.min_size):
            yield dict(cand)
        for i, (k, v) in enumerate(items):
            for c in self.values.shrink(v):
                new = dict(items)
                new[k] = c
                yield new
        for i, (k, v) in enumerate(items):
            for c in self.keys.shrink(k):
                if c in value:
                    continue
                try:
                    hash(c)
                except TypeError:
                    continue
                yield dict(items[:i] + [(c, v)] + items[i + 1 :])

    def validate(self, value: Any) -> bool:
        return isinstance(value, dict) and self.min_size <= len(value) <= self.max_size


@register("record", "struct", "object_of")
class RecordGen(Generator):
    """A dict with fixed keys, e.g. a JSON request body or a config object.
    Options: ``fields`` ({name: spec}), ``optional`` (names that may be omitted)."""

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        fields = spec.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise SpecError("record needs a non-empty 'fields' object")
        self.fields = {k: build(v) for k, v in fields.items()}
        self.optional = set(spec.get("optional", []))
        self.scalable = any(g.scalable for g in self.fields.values())

    def generate(self, rng: random.Random, size: float) -> dict[str, Any]:
        return {k: g.generate(rng, size) for k, g in self.fields.items() if k not in self.optional or rng.random() < 0.7}

    def scaled(self, rng: random.Random, n: int) -> dict[str, Any]:
        return {k: g.scaled(rng, n) if g.scalable else g.generate(rng, 0.3) for k, g in self.fields.items()}

    def edge_cases(self) -> list[dict[str, Any]]:
        firsts = {k: (g.edge_cases() or [g.generate(random.Random(0), 0.0)]) for k, g in self.fields.items()}
        base = {k: e[0] for k, e in firsts.items()}
        out = [dict(base)]
        for k, edges in firsts.items():
            for e in edges[1:4]:
                out.append({**base, k: e})
        return out

    def shrink(self, value: Any) -> Iterator[dict[str, Any]]:
        if not isinstance(value, dict):
            return
        for k in self.optional:
            if k in value:
                yield {kk: vv for kk, vv in value.items() if kk != k}
        for k, g in self.fields.items():
            if k in value:
                for c in g.shrink(value[k]):
                    yield {**value, k: c}

    def validate(self, value: Any) -> bool:
        return (
            isinstance(value, dict)
            and set(value) <= set(self.fields)
            and set(self.fields) - set(value) <= self.optional
            and all(self.fields[k].validate(v) for k, v in value.items())
        )


@register("set", "frozenset")
class SetGen(Generator):
    scalable = True

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.inner = ListGen(
            {
                "elements": spec.get("elements", spec.get("of", {"type": "int"})),
                "min_len": spec.get("min_size", 0),
                "max_len": spec.get("max_size", 30),
                "unique": True,
            }
        )
        self.frozen = spec.get("type") == "frozenset" or bool(spec.get("frozen", False))

    def _wrap(self, values: list[Any]) -> Any:
        return frozenset(values) if self.frozen else set(values)

    def _unwrap(self, value: Any) -> list[Any]:
        try:
            return sorted(value)
        except TypeError:
            return sorted(value, key=repr)

    def generate(self, rng: random.Random, size: float) -> Any:
        return self._wrap(self.inner.generate(rng, size))

    def scaled(self, rng: random.Random, n: int) -> Any:
        return self._wrap(self.inner.scaled(rng, n))

    def edge_cases(self) -> list[Any]:
        return [self._wrap(v) for v in self.inner.edge_cases()]

    def shrink(self, value: Any) -> Iterator[Any]:
        if not isinstance(value, (set, frozenset)):
            return
        for cand in self.inner.shrink(self._unwrap(value)):
            yield self._wrap(cand)

    def validate(self, value: Any) -> bool:
        return isinstance(value, (set, frozenset))


@register("permutation")
class PermutationGen(Generator):
    """A permutation of ``start .. start+n-1``. Options: ``n`` (range), ``start``."""

    scalable = True

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.lo, self.hi = parse_range(spec.get("n"), 0, 10)
        self.start = int(spec.get("start", 0))

    def _make(self, rng: random.Random, n: int) -> list[int]:
        perm = list(range(self.start, self.start + n))
        r = rng.random()
        if r < 0.1:
            return perm
        if r < 0.2:
            return perm[::-1]
        rng.shuffle(perm)
        return perm

    def generate(self, rng: random.Random, size: float) -> list[int]:
        return self._make(rng, sized_count(rng, self.lo, self.hi, size))

    def scaled(self, rng: random.Random, n: int) -> list[int]:
        return self._make(rng, max(self.lo, n))

    def edge_cases(self) -> list[list[int]]:
        s = self.start
        cands = [[], [s], [s, s + 1], [s + 1, s], [s + 2, s + 1, s], [s + 1, s + 2, s]]
        return [c for c in cands if self.lo <= len(c) <= self.hi] or [list(range(s, s + self.lo))]

    def shrink(self, value: Any) -> Iterator[list[int]]:
        if not isinstance(value, list):
            return
        n = len(value)
        if n > self.lo:
            for v in sorted(value, reverse=True):
                yield [x - (1 if x > v else 0) for x in value if x != v]
        for i in range(n - 1):
            if value[i] > value[i + 1]:
                new = list(value)
                new[i], new[i + 1] = new[i + 1], new[i]
                yield new

    def validate(self, value: Any) -> bool:
        return isinstance(value, list) and sorted(value) == list(range(self.start, self.start + len(value)))
