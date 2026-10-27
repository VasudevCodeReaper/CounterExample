"""Interval generators: nested, overlapping, touching, disjoint, duplicated and
zero-length ranges - the classic sources of off-by-one bugs in sweep lines,
interval merging and scheduling code.

Values are lists of ``[lo, hi]`` pairs (``"format": "tuple"`` gives tuples).
Coordinates are integers unless ``"float": true``, in which case they lie on a
``1 / resolution`` grid (default 2, i.e. halves) so shrinking stays exact.
"""

from __future__ import annotations

import random
from typing import Any, Iterator

from counterexample.generators.base import (
    Generator,
    SpecError,
    parse_range,
    register,
    remove_chunks,
    shrink_int_toward,
    sized_count,
)

MODES = ("random", "nested", "overlapping", "touching", "disjoint", "duplicates")


@register("intervals", "ranges")
class IntervalsGen(Generator):
    """Options: ``count`` (range, default 0..20), ``min``/``max`` coordinates
    (default -100..100), ``allow_empty`` (zero-length intervals, default true),
    ``mode`` (mixed, random, nested, overlapping, touching, disjoint, duplicates),
    ``sorted`` (by start), ``format`` (list/tuple), ``float``, ``resolution``."""

    scalable = True

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.c_lo, self.c_hi = parse_range(spec.get("count"), 0, 20)
        self.is_float = bool(spec.get("float", False))
        self.res = int(spec.get("resolution", 2)) if self.is_float else 1
        self.lo = int(round(float(spec.get("min", -100)) * self.res))
        self.hi = int(round(float(spec.get("max", 100)) * self.res))
        if self.lo > self.hi:
            raise SpecError("intervals min exceeds max")
        self.allow_empty = bool(spec.get("allow_empty", True))
        self.min_width = 0 if self.allow_empty else 1
        if self.hi - self.lo < self.min_width:
            raise SpecError("coordinate range too small for non-empty intervals")
        self.mode = spec.get("mode", "mixed")
        if self.mode != "mixed" and self.mode not in MODES:
            raise SpecError(f"unknown interval mode {self.mode!r}; choose mixed or one of {MODES}")
        self.sorted = bool(spec.get("sorted", False))
        self.as_tuple = spec.get("format", "list") == "tuple"
        self.target = min(max(0, self.lo), self.hi - self.min_width)

    # -- representation ---------------------------------------------------- #
    def _out(self, ivs: list[tuple[int, int]]) -> list[Any]:
        if self.sorted:
            ivs = sorted(ivs)
        conv = (lambda v: v / self.res) if self.is_float else (lambda v: v)
        return [(conv(a), conv(b)) if self.as_tuple else [conv(a), conv(b)] for a, b in ivs]

    def _in(self, value: Any) -> list[tuple[int, int]] | None:
        try:
            return [(int(round(a * self.res)), int(round(b * self.res))) for a, b in value]
        except (TypeError, ValueError):
            return None

    # -- sampling ---------------------------------------------------------- #
    def _clamp(self, a: int, b: int) -> tuple[int, int]:
        a = min(max(a, self.lo), self.hi - self.min_width)
        b = min(max(b, a + self.min_width), self.hi)
        return a, b

    def _one(self, rng: random.Random, window: tuple[int, int], max_w: int) -> tuple[int, int]:
        a = rng.randint(window[0], window[1])
        return self._clamp(a, a + rng.randint(self.min_width, max(self.min_width, max_w)))

    def _make(self, rng: random.Random, n: int, size: float) -> list[tuple[int, int]]:
        span = self.hi - self.lo
        width = max(4, min(span, int(4 + size * span / 2)))
        start = rng.randint(self.lo, max(self.lo, self.hi - width))
        window = (start, start + width)
        max_w = max(1, width // 3)
        mode = self.mode if self.mode != "mixed" else rng.choice(MODES + ("mixed",) * 2)
        out: list[tuple[int, int]] = []
        for i in range(n):
            m = mode if mode != "mixed" else rng.choice(MODES)
            if not out or m == "random":
                out.append(self._one(rng, window, max_w))
                continue
            pa, pb = rng.choice(out) if m in ("nested", "duplicates") else out[-1]
            if m == "nested":
                a = rng.randint(pa, pb)
                b = rng.randint(a, pb)
                out.append(self._clamp(a, max(b, a + self.min_width)))
            elif m == "overlapping":
                a = rng.randint(pa, pb)
                out.append(self._clamp(a, pb + rng.randint(self.min_width, max_w)))
            elif m == "touching":
                out.append(self._clamp(pb, pb + rng.randint(self.min_width, max_w)))
            elif m == "disjoint":
                a = pb + rng.randint(1, 3)
                out.append(self._clamp(a, a + rng.randint(self.min_width, max_w)))
            else:  # duplicates
                out.append((pa, pb))
        if rng.random() < 0.7:
            rng.shuffle(out)
        return out

    def generate(self, rng: random.Random, size: float) -> list[Any]:
        return self._out(self._make(rng, sized_count(rng, self.c_lo, self.c_hi, size), size))

    def scaled(self, rng: random.Random, n: int) -> list[Any]:
        return self._out(self._make(rng, max(self.c_lo, n), 1.0))

    def edge_cases(self) -> list[list[Any]]:
        t = self.target
        w = max(1, self.min_width)
        cands = [
            [],
            [(t, t + self.min_width)],
            [(t, t + w)],
            [(t, t + w), (t + w, t + 2 * w)],  # touching
            [(t, t + 2 * w), (t + w, t + 3 * w)],  # overlapping
            [(t, t + 3 * w), (t + w, t + 2 * w)],  # nested
            [(t, t + w), (t, t + w)],  # duplicates
            [(t + w, t + 2 * w), (t, t + w)],  # unsorted touching
            [(t, t + w), (t + 2 * w, t + 3 * w)],  # disjoint with gap
        ]
        out = []
        for c in cands:
            if not (self.c_lo <= len(c) <= self.c_hi):
                continue
            if any(a < self.lo or b > self.hi or b - a < self.min_width for a, b in c):
                continue
            v = self._out(c)
            if v not in out:
                out.append(v)
        return out

    # -- shrinking --------------------------------------------------------- #
    def shrink(self, value: Any) -> Iterator[list[Any]]:
        ivs = self._in(value)
        if ivs is None:
            return
        for cand in remove_chunks(ivs, self.c_lo):
            yield self._out(cand)
        if ivs:
            # Translate the whole configuration toward the origin: keeps every
            # nesting/overlap/touch relation but makes coordinates small.
            low = min(a for a, _ in ivs)
            if low != self.target:
                shift = low - self.target
                moved = [(a - shift, b - shift) for a, b in ivs]
                if all(self.lo <= a and b <= self.hi for a, b in moved):
                    yield self._out(moved)
        for i, (a, b) in enumerate(ivs):
            for nb in shrink_int_toward(b, a + self.min_width):
                if nb < b:
                    yield self._out(ivs[:i] + [(a, nb)] + ivs[i + 1 :])
            for na in shrink_int_toward(a, self.target):
                nb = b - (a - na)
                if self.lo <= na and nb <= self.hi and nb - na >= self.min_width:
                    yield self._out(ivs[:i] + [(na, nb)] + ivs[i + 1 :])

    def validate(self, value: Any) -> bool:
        ivs = self._in(value)
        return (
            ivs is not None
            and self.c_lo <= len(ivs) <= self.c_hi
            and all(self.lo <= a and b <= self.hi and b - a >= self.min_width for a, b in ivs)
        )


@register("interval")
class IntervalGen(Generator):
    """A single ``[lo, hi]`` pair (same options as ``intervals``)."""

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.inner = IntervalsGen({**spec, "count": 1, "mode": "random"})

    def generate(self, rng: random.Random, size: float) -> Any:
        return self.inner.generate(rng, size)[0]

    def edge_cases(self) -> list[Any]:
        return [v[0] for v in self.inner.edge_cases() if v]

    def shrink(self, value: Any) -> Iterator[Any]:
        for cand in self.inner.shrink([value]):
            if cand:
                yield cand[0]

    def validate(self, value: Any) -> bool:
        return self.inner.validate([value])
