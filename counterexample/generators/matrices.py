"""Matrix and grid generators: sparse, singular, identity, zero, diagonal,
symmetric, triangular and degenerate (0xN, Nx0, 1xN, Nx1) shapes."""

from __future__ import annotations

import math
import random
from typing import Any, Iterator

from counterexample.generators.base import Generator, SpecError, build, parse_range, register, sized_count

KINDS = ("dense", "sparse", "singular", "identity", "zero", "diagonal", "symmetric", "triangular")


def _shape(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, list) or not all(isinstance(r, (list, str)) for r in value):
        return None
    if not value:
        return 0, 0
    cols = len(value[0])
    if any(len(r) != cols for r in value):
        return None
    return len(value), cols


class _GridBase(Generator):
    scalable = True

    def __init__(self, spec: dict[str, Any], default_hi: int):
        super().__init__(spec)
        self.r_lo, self.r_hi = parse_range(spec.get("rows"), 0, default_hi)
        self.square = bool(spec.get("square", False))
        if self.square:
            self.c_lo, self.c_hi = self.r_lo, self.r_hi
        else:
            self.c_lo, self.c_hi = parse_range(spec.get("cols"), 0, default_hi)

    def _dims(self, rng: random.Random, size: float) -> tuple[int, int]:
        r = sized_count(rng, self.r_lo, self.r_hi, size)
        c = r if self.square else sized_count(rng, self.c_lo, self.c_hi, size)
        if r == 0 and c > 0 and rng.random() < 0.5:
            c = 0
        return r, c

    def _scaled_dims(self, n: int) -> tuple[int, int]:
        side = max(1, int(round(math.sqrt(max(1, n)))))
        return max(self.r_lo, side), max(self.c_lo, side)

    def _cell_shrink(self, cell: Any) -> Iterator[Any]:
        return iter(())

    def _rows(self, value: Any) -> list[list[Any]]:
        return [list(r) for r in value]

    def _out(self, rows: list[list[Any]]) -> Any:
        return rows

    def shrink(self, value: Any) -> Iterator[Any]:
        shape = _shape(value)
        if shape is None:
            return
        r, c = shape
        rows = self._rows(value)
        if self.square:
            if r > self.r_lo:
                for i in range(r - 1, -1, -1):
                    yield self._out([row[:i] + row[i + 1 :] for k, row in enumerate(rows) if k != i])
        else:
            if r > self.r_lo:
                if self.r_lo == 0:
                    yield self._out([])
                for i in range(r - 1, -1, -1):
                    yield self._out(rows[:i] + rows[i + 1 :])
            if c > self.c_lo and r > 0:
                for j in range(c - 1, -1, -1):
                    yield self._out([row[:j] + row[j + 1 :] for row in rows])
        for i in range(r):
            for j in range(c):
                for cand in self._cell_shrink(rows[i][j]):
                    new = [list(row) for row in rows]
                    new[i][j] = cand
                    yield self._out(new)

    def validate(self, value: Any) -> bool:
        shape = _shape(value)
        if shape is None:
            return False
        r, c = shape
        ok = self.r_lo <= r <= self.r_hi and (r == 0 or self.c_lo <= c <= self.c_hi)
        return ok and (not self.square or r == c)


@register("matrix")
class MatrixGen(_GridBase):
    """Numeric matrices as lists of rows. Options: ``rows``, ``cols`` (ranges,
    default 0..6), ``square``, ``elements`` (spec, default int -10..10), ``kind``
    (mixed or one of dense/sparse/singular/identity/zero/diagonal/symmetric/
    triangular), ``density`` (non-zero fraction for sparse)."""

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec, 6)
        self.elem = build(spec.get("elements", {"type": "int", "min": -10, "max": 10}))
        edges = self.elem.edge_cases()
        self.zero = edges[0] if edges else 0
        self.one = next((e for e in edges if e == 1), self.zero)
        self.kind = spec.get("kind", "mixed")
        if self.kind != "mixed" and self.kind not in KINDS:
            raise SpecError(f"unknown matrix kind {self.kind!r}; choose mixed or one of {KINDS}")
        self.density = spec.get("density")

    def _cell(self, rng: random.Random, size: float) -> Any:
        return self.elem.generate(rng, size)

    def _make(self, rng: random.Random, r: int, c: int, size: float) -> list[list[Any]]:
        kind = self.kind if self.kind != "mixed" else rng.choice(KINDS + ("dense", "dense"))
        sq = r == c
        if kind == "zero":
            return [[self.zero] * c for _ in range(r)]
        if kind == "identity" and sq:
            return [[self.one if i == j else self.zero for j in range(c)] for i in range(r)]
        if kind == "diagonal" and sq:
            return [[self._cell(rng, size) if i == j else self.zero for j in range(c)] for i in range(r)]
        if kind == "sparse":
            density = float(self.density) if self.density is not None else rng.choice([0.05, 0.1, 0.2, 0.3])
            return [[self._cell(rng, size) if rng.random() < density else self.zero for _ in range(c)] for _ in range(r)]
        m = [[self._cell(rng, size) for _ in range(c)] for _ in range(r)]
        if kind == "symmetric" and sq:
            for i in range(r):
                for j in range(i):
                    m[i][j] = m[j][i]
        elif kind == "triangular" and sq:
            for i in range(r):
                for j in range(i):
                    m[i][j] = self.zero
        elif kind == "singular" and r >= 1 and c >= 1:
            trick = rng.choice(["dup_row", "zero_row", "dup_col", "scaled_row"])
            i = rng.randrange(r)
            k = rng.randrange(r)
            if trick == "zero_row" or r == 1:
                m[i] = [self.zero] * c
            elif trick == "dup_row":
                m[i] = list(m[k if k != i else (i + 1) % r])
            elif trick == "dup_col" and c >= 2:
                j, l = rng.sample(range(c), 2)
                for row in m:
                    row[j] = row[l]
            else:
                src = m[k if k != i else (i + 1) % r]
                try:
                    m[i] = [x * 2 for x in src]
                except TypeError:
                    m[i] = list(src)
        return m

    def generate(self, rng: random.Random, size: float) -> list[list[Any]]:
        r, c = self._dims(rng, size)
        return self._make(rng, r, c, size)

    def scaled(self, rng: random.Random, n: int) -> list[list[Any]]:
        r, c = self._scaled_dims(n)
        return [[self._cell(rng, 1.0) for _ in range(c)] for _ in range(r)]

    def edge_cases(self) -> list[list[list[Any]]]:
        z, o = self.zero, self.one
        e = self.elem.edge_cases() or [z]
        two = next((x for x in e if x not in (z, o)), o)
        cands = [
            [],
            [[z]],
            [[o]],
            [[e[-1]]],
            [[z, z], [z, z]],
            [[o, z], [z, o]],
            [[o, two], [o, two]],  # singular: duplicate rows
            [[o, two, o]],  # row vector
            [[o], [two], [o]],  # column vector
        ]
        return [m for m in cands if self.validate(m)]

    def _cell_shrink(self, cell: Any) -> Iterator[Any]:
        return self.elem.shrink(cell)


@register("grid")
class GridGen(_GridBase):
    """Cell grids (mazes, islands, boards). Options: ``rows``, ``cols`` (default
    0..8), ``cells`` (values, default ``[0, 1]``; first is the "open" cell),
    ``density`` (fraction of non-open cells), ``format`` (``lists`` or ``strings``
    when cells are single characters)."""

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec, 8)
        self.cells = list(spec.get("cells", [0, 1]))
        if not self.cells:
            raise SpecError("grid needs a non-empty 'cells' list")
        self.as_strings = spec.get("format", "lists") == "strings"
        if self.as_strings and not all(isinstance(c, str) and len(c) == 1 for c in self.cells):
            raise SpecError("grid format 'strings' needs single-character cells")
        self.density = spec.get("density")

    def _out(self, rows: list[list[Any]]) -> Any:
        return ["".join(r) for r in rows] if self.as_strings else rows

    def _make(self, rng: random.Random, r: int, c: int, size: float) -> Any:
        open_cell = self.cells[0]
        others = self.cells[1:] or [open_cell]
        style = rng.choice(["random", "random", "open", "full", "border", "snake"])
        if style == "open":
            rows = [[open_cell] * c for _ in range(r)]
        elif style == "full":
            rows = [[others[0]] * c for _ in range(r)]
        elif style == "border":
            rows = [[others[0] if i in (0, r - 1) or j in (0, c - 1) else open_cell for j in range(c)] for i in range(r)]
        elif style == "snake":
            rows = [[others[0] if (i % 2 == 1 and j != (c - 1 if i % 4 == 1 else 0)) else open_cell for j in range(c)] for i in range(r)]
        else:
            density = float(self.density) if self.density is not None else rng.choice([0.1, 0.3, 0.5, 0.7])
            rows = [[rng.choice(others) if rng.random() < density else open_cell for _ in range(c)] for _ in range(r)]
        return self._out(rows)

    def generate(self, rng: random.Random, size: float) -> Any:
        r, c = self._dims(rng, size)
        return self._make(rng, r, c, size)

    def scaled(self, rng: random.Random, n: int) -> Any:
        r, c = self._scaled_dims(n)
        return self._make(rng, r, c, 1.0)

    def edge_cases(self) -> list[Any]:
        o = self.cells[0]
        x = self.cells[1] if len(self.cells) > 1 else o
        cands = [[], [[o]], [[x]], [[o, x], [x, o]], [[o, o], [o, o]], [[x, o, x]], [[o], [x], [o]]]
        return [self._out(m) for m in cands if self.validate(self._out(m))]

    def _cell_shrink(self, cell: Any) -> Iterator[Any]:
        if cell != self.cells[0]:
            yield self.cells[0]
