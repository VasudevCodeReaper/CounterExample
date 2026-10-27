"""Graph generators: general (di)graphs, DAGs and trees.

Adversarial structure on by default: disconnected components, isolated nodes,
shuffled node labels, skewed densities, optional self-loops, multi-edges,
negative weights and injected negative cycles.

Output formats (``format``):

* ``edge_list`` (default) - ``(n, [[u, v], ...])`` or ``(n, [[u, v, w], ...])``.
  Add ``"spread": true`` on the argument to call ``f(n, edges)``.
* ``edges``             - just ``[[u, v(, w)], ...]``.
* ``adjacency_list``    - ``adj[u] = [v, ...]`` or ``[[v, w], ...]``.
* ``adjacency_matrix``  - ``n x n``; 0/1 unweighted, weight or ``no_edge`` weighted.
* ``dict``              - ``{u: [v, ...]}`` including isolated nodes.
* ``parent`` (trees)    - ``parent[i]``, with ``-1`` for the root.
"""

from __future__ import annotations

import random
from typing import Any, Iterator

from counterexample.generators.base import (
    Generator,
    SpecError,
    build,
    parse_range,
    register,
    remove_chunks,
    sized_count,
)

Edge = tuple  # (u, v) or (u, v, w), 0-based


def _is_connected(n: int, edges: list[Edge]) -> bool:
    if n <= 1:
        return True
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        a, b = find(e[0]), find(e[1])
        if a != b:
            parent[a] = b
    root = find(0)
    return all(find(i) == root for i in range(n))


class _GraphBase(Generator):
    scalable = True
    default_format = "edge_list"
    formats = ("edge_list", "edges", "adjacency_list", "adjacency_matrix", "dict")

    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        self.n_lo, self.n_hi = parse_range(spec.get("nodes", spec.get("n")), 1, 12)
        if self.n_lo < 0:
            raise SpecError("graph node count cannot be negative")
        self.directed = bool(spec.get("directed", spec.get("type") in ("digraph", "dag")))
        neg = bool(spec.get("negative_weights", False)) or bool(spec.get("negative_cycles", False))
        self.weighted = bool(spec.get("weighted", "weights" in spec or neg))
        self.weight_gen = build(spec.get("weights", {"type": "int", "min": -10 if neg else 1, "max": 10}))
        self.format = spec.get("format", self.default_format)
        if self.format not in self.formats:
            raise SpecError(f"unsupported {self.type_name} format {self.format!r}; choose from {self.formats}")
        self.base = int(spec.get("index_base", 0))
        no_edge = spec.get("no_edge", None if self.weighted else 0)
        self.no_edge = float(no_edge) if isinstance(no_edge, str) and no_edge.lstrip("+-") in ("inf", "infinity") else no_edge
        self.as_lists = spec.get("edge_type", "list") != "tuple"

    # -- format conversion ------------------------------------------------- #
    def _edge_out(self, e: Edge) -> Any:
        items = [e[0] + self.base, e[1] + self.base] + ([e[2]] if self.weighted else [])
        return items if self.as_lists else tuple(items)

    def emit(self, n: int, edges: list[Edge]) -> Any:
        fmt = self.format
        if fmt == "edge_list":
            return (n, [self._edge_out(e) for e in edges])
        if fmt == "edges":
            return [self._edge_out(e) for e in edges]
        if fmt in ("adjacency_list", "dict"):
            adj: list[list[Any]] = [[] for _ in range(n)]
            for e in edges:
                u, v = e[0], e[1]
                adj[u].append([v + self.base, e[2]] if self.weighted else v + self.base)
                if not self.directed and u != v:
                    adj[v].append([u + self.base, e[2]] if self.weighted else u + self.base)
            if fmt == "dict":
                return {u + self.base: adj[u] for u in range(n)}
            return ([[]] * self.base if self.base else []) + adj
        if fmt == "adjacency_matrix":
            mat = [[self.no_edge for _ in range(n)] for _ in range(n)]
            for e in edges:
                u, v = e[0], e[1]
                val = e[2] if self.weighted else 1
                if self.weighted and mat[u][v] is not None and mat[u][v] != self.no_edge:
                    val = min(mat[u][v], val)
                mat[u][v] = val
                if not self.directed:
                    mat[v][u] = val
            return mat
        raise SpecError(f"unsupported format {fmt!r}")

    def parse(self, value: Any) -> tuple[int, list[Edge]] | None:
        """Inverse of :meth:`emit` (used by the shrinker)."""
        fmt = self.format
        b = self.base
        try:
            if fmt == "edge_list":
                n, raw = value
                return int(n), [self._edge_in(e) for e in raw]
            if fmt == "edges":
                edges = [self._edge_in(e) for e in value]
                # Isolated nodes are invisible in a bare edge list.
                n = max(max((max(e[0], e[1]) for e in edges), default=-1) + 1, self.n_lo)
                return n, edges
            if fmt in ("adjacency_list", "dict"):
                if fmt == "dict":
                    keys = sorted(value)
                    rows = [value[k] for k in keys]
                else:
                    rows = value[b:]
                n = len(rows)
                edges = []
                for u, nbrs in enumerate(rows):
                    for item in nbrs:
                        v, w = (item[0], item[1]) if self.weighted else (item, None)
                        v -= b
                        if self.directed or u <= v:
                            edges.append((u, v, w) if self.weighted else (u, v))
                return n, edges
            if fmt == "adjacency_matrix":
                n = len(value)
                edges = []
                for u in range(n):
                    for v in range(n):
                        cell = value[u][v]
                        if cell is None or cell == self.no_edge:
                            continue
                        if not self.directed and v < u:
                            continue
                        edges.append((u, v, cell) if self.weighted else (u, v))
                return n, edges
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        return None

    def _edge_in(self, e: Any) -> Edge:
        u, v = int(e[0]) - self.base, int(e[1]) - self.base
        return (u, v, e[2]) if self.weighted else (u, v)

    # -- helpers ----------------------------------------------------------- #
    def _weight(self, rng: random.Random, size: float) -> Any:
        return self.weight_gen.generate(rng, size)

    @staticmethod
    def _remove_node(n: int, edges: list[Edge], v: int) -> tuple[int, list[Edge]]:
        out = []
        for e in edges:
            if e[0] == v or e[1] == v:
                continue
            a = e[0] - (1 if e[0] > v else 0)
            c = e[1] - (1 if e[1] > v else 0)
            out.append((a, c) + tuple(e[2:]))
        return n - 1, out

    def _contract(self, n: int, edges: list[Edge], keep: int, drop: int) -> tuple[int, list[Edge]]:
        """Merge node ``drop`` into ``keep`` (edge contraction), then renumber."""
        merged: list[Edge] = []
        seen: set[tuple[int, int]] = set()
        allow_loops = bool(self.spec.get("allow_self_loops", False))
        allow_multi = bool(self.spec.get("multi_edges", False))
        for e in edges:
            u = keep if e[0] == drop else e[0]
            v = keep if e[1] == drop else e[1]
            if u == v and not allow_loops:
                continue
            key = (u, v) if self.directed else (min(u, v), max(u, v))
            if key in seen and not allow_multi:
                continue
            seen.add(key)
            merged.append((u, v) + tuple(e[2:]))
        return self._remove_node(n, merged, drop)

    def _valid(self, n: int, edges: list[Edge]) -> bool:
        return self.n_lo <= n

    def shrink(self, value: Any) -> Iterator[Any]:
        parsed = self.parse(value)
        if parsed is None:
            return
        n, edges = parsed
        for cand in remove_chunks(edges, 0):
            if self._valid(n, cand):
                yield self.emit(n, cand)
        if n > self.n_lo:
            for v in range(n - 1, -1, -1):
                n2, e2 = self._remove_node(n, edges, v)
                if self._valid(n2, e2):
                    yield self.emit(n2, e2)
            # Contracting an edge shortens cycles and paths that node removal would break.
            for e in edges:
                a, b = e[0], e[1]
                if a != b:
                    n2, e2 = self._contract(n, edges, min(a, b), max(a, b))
                    if self._valid(n2, e2):
                        yield self.emit(n2, e2)
        if self.weighted:
            for i, e in enumerate(edges):
                for w in self.weight_gen.shrink(e[2]):
                    new = list(edges)
                    new[i] = (e[0], e[1], w)
                    yield self.emit(n, new)

    def validate(self, value: Any) -> bool:
        parsed = self.parse(value)
        return parsed is not None and self._valid(*parsed)

    def edge_cases(self) -> list[Any]:
        cands: list[tuple[int, list[Edge]]] = []
        w0 = (self.weight_gen.edge_cases() or [1])[0]
        wneg = next((w for w in self.weight_gen.edge_cases() if isinstance(w, (int, float)) and w < 0), w0)

        def E(u: int, v: int, w: Any = None) -> Edge:
            return (u, v, w0 if w is None else w) if self.weighted else (u, v)

        cands.append((self.n_lo, []))
        cands.append((max(self.n_lo, 1), []))
        cands.append((max(self.n_lo, 2), []))  # two isolated nodes: disconnected
        cands.append((max(self.n_lo, 2), [E(0, 1)]))
        cands.append((max(self.n_lo, 3), [E(0, 1)]))  # isolated third node
        cands.append((max(self.n_lo, 3), [E(0, 1), E(1, 2), E(2, 0)]))  # cycle
        if self.weighted:
            cands.append((max(self.n_lo, 2), [E(0, 1, wneg)]))
        return [self.emit(n, e) for n, e in cands if self.n_lo <= n <= self.n_hi and self._valid(n, e)]


@register("graph", "digraph", "negative_cycle_graph")
class GraphGen(_GraphBase):
    """General graphs. Options: ``nodes``, ``directed``, ``weighted``, ``weights``
    (any generator spec), ``negative_weights``, ``negative_cycles``, ``density``,
    ``allow_self_loops``, ``multi_edges``, ``connected``, ``format``, ``index_base``."""

    def __init__(self, spec: dict[str, Any]):
        if spec.get("type") == "negative_cycle_graph":
            spec = {"directed": True, "weighted": True, "negative_cycles": True, **spec}
        super().__init__(spec)
        self.density = spec.get("density")
        self.self_loops = bool(spec.get("allow_self_loops", False))
        self.multi = bool(spec.get("multi_edges", False))
        self.connected = bool(spec.get("connected", False))
        self.neg_cycles = bool(spec.get("negative_cycles", False))
        self.always_neg_cycle = spec.get("type") == "negative_cycle_graph"

    def _valid(self, n: int, edges: list[Edge]) -> bool:
        return n >= self.n_lo and (not self.connected or _is_connected(n, edges))

    def _pairs(self, rng: random.Random, n: int, density: float, groups: list[int] | None) -> list[tuple[int, int]]:
        pairs = []
        for u in range(n):
            for v in range(n):
                if u == v and not self.self_loops:
                    continue
                if not self.directed and v < u:
                    continue
                if groups is not None and groups[u] != groups[v]:
                    continue
                if rng.random() < density:
                    pairs.append((u, v))
        return pairs

    def _sparse_pairs(self, rng: random.Random, n: int, m: int) -> list[tuple[int, int]]:
        seen: set[tuple[int, int]] = set()
        pairs = []
        attempts = 0
        while len(pairs) < m and attempts < m * 4 + 10:
            attempts += 1
            u, v = rng.randrange(n), rng.randrange(n)
            if u == v and not self.self_loops:
                continue
            key = (u, v) if self.directed else (min(u, v), max(u, v))
            if key in seen and not self.multi:
                continue
            seen.add(key)
            pairs.append(key)
        return pairs

    def _decorate(self, rng: random.Random, n: int, pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if self.connected and n > 1:
            order = list(range(n))
            rng.shuffle(order)
            have = {(min(a, b), max(a, b)) for a, b in pairs}
            for i in range(1, n):
                a, b = order[i], order[rng.randrange(i)]
                if (min(a, b), max(a, b)) not in have:
                    pairs.append((b, a) if self.directed and rng.random() < 0.5 else (a, b))
                    have.add((min(a, b), max(a, b)))
        if self.multi and pairs and rng.random() < 0.5:
            pairs += [rng.choice(pairs) for _ in range(rng.randint(1, 3))]
        if self.self_loops and n and rng.random() < 0.3:
            v = rng.randrange(n)
            pairs.append((v, v))
        return pairs

    def _inject_negative_cycle(self, rng: random.Random, n: int, edges: list[Edge]) -> list[Edge]:
        if n < 2 and not self.self_loops:
            return edges
        k = rng.randint(2, min(n, 4)) if n >= 2 else 1
        nodes = rng.sample(range(n), k)
        cyc = [(nodes[i], nodes[(i + 1) % k]) for i in range(k)] if k > 1 else [(nodes[0], nodes[0])]
        if not self.directed:
            cyc = cyc[:1]  # a single negative undirected edge already loops u-v-u
        weights = [rng.randint(-5, 5) for _ in cyc]
        total = sum(weights)
        if total >= 0:
            weights[0] -= total + rng.randint(1, 3)
        lo = getattr(self.weight_gen, "lo", None)
        if lo is not None:
            weights = [max(lo, w) for w in weights]
            if sum(weights) >= 0:
                return edges
        taken = set(cyc) if self.directed else {(min(u, v), max(u, v)) for u, v in cyc}

        def clashes(e: Edge) -> bool:
            key = (e[0], e[1]) if self.directed else (min(e[0], e[1]), max(e[0], e[1]))
            return key in taken and not self.multi

        return [e for e in edges if not clashes(e)] + [(u, v, w) for (u, v), w in zip(cyc, weights)]

    def _build(self, rng: random.Random, n: int, pairs: list[tuple[int, int]], size: float) -> Any:
        pairs = self._decorate(rng, n, pairs)
        if rng.random() < 0.5 and n > 1:
            labels = list(range(n))
            rng.shuffle(labels)
            pairs = [(labels[u], labels[v]) for u, v in pairs]
        rng.shuffle(pairs)
        edges: list[Edge] = [(u, v, self._weight(rng, size)) if self.weighted else (u, v) for u, v in pairs]
        if self.weighted and self.neg_cycles and n and (self.always_neg_cycle or rng.random() < 0.5):
            edges = self._inject_negative_cycle(rng, n, edges)
        return self.emit(n, edges)

    def generate(self, rng: random.Random, size: float) -> Any:
        n = sized_count(rng, self.n_lo, self.n_hi, size)
        if self.density is not None:
            density = float(self.density)
        else:
            density = rng.choice([0.0, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0])
        groups = None
        if not self.connected and n >= 3 and rng.random() < 0.25:
            k = rng.randint(2, min(4, n))
            groups = [rng.randrange(k) for _ in range(n)]
        return self._build(rng, n, self._pairs(rng, n, density, groups), size)

    def scaled(self, rng: random.Random, n: int) -> Any:
        n = max(self.n_lo, n)
        avg_degree = float(self.spec.get("avg_degree", 4))
        if self.density is not None:
            m = int(float(self.density) * n * (n - 1) / (1 if self.directed else 2))
        else:
            m = int(avg_degree * n / (1 if self.directed else 2))
        return self._build(rng, n, self._sparse_pairs(rng, n, m), 1.0)


@register("dag")
class DagGen(_GraphBase):
    """Directed acyclic graphs with shuffled labels (topological order != label order)."""

    def __init__(self, spec: dict[str, Any]):
        super().__init__({**spec, "directed": True})
        self.density = spec.get("density")

    def _make(self, rng: random.Random, n: int, m: int | None, density: float, size: float) -> Any:
        order = list(range(n))
        rng.shuffle(order)
        pairs: list[tuple[int, int]] = []
        if m is None:
            for i in range(n):
                for j in range(i + 1, n):
                    if rng.random() < density:
                        pairs.append((order[i], order[j]))
        else:
            seen = set()
            attempts = 0
            while len(pairs) < m and attempts < m * 4 + 10 and n > 1:
                attempts += 1
                i, j = sorted(rng.sample(range(n), 2))
                if (i, j) not in seen:
                    seen.add((i, j))
                    pairs.append((order[i], order[j]))
        rng.shuffle(pairs)
        edges = [(u, v, self._weight(rng, size)) if self.weighted else (u, v) for u, v in pairs]
        return self.emit(n, edges)

    def generate(self, rng: random.Random, size: float) -> Any:
        n = sized_count(rng, self.n_lo, self.n_hi, size)
        density = float(self.density) if self.density is not None else rng.choice([0.0, 0.15, 0.3, 0.6, 1.0])
        return self._make(rng, n, None, density, size)

    def scaled(self, rng: random.Random, n: int) -> Any:
        n = max(self.n_lo, n)
        return self._make(rng, n, int(float(self.spec.get("avg_degree", 2)) * n), 0.0, 1.0)

    def edge_cases(self) -> list[Any]:
        return [g for g in super().edge_cases() if self._acyclic(self.parse(g))]

    def _valid(self, n: int, edges: list[Edge]) -> bool:
        return n >= self.n_lo and self._acyclic((n, edges))

    @staticmethod
    def _acyclic(parsed: tuple[int, list[Edge]] | None) -> bool:
        if parsed is None:
            return False
        n, edges = parsed
        indeg = [0] * n
        out: list[list[int]] = [[] for _ in range(n)]
        for e in edges:
            out[e[0]].append(e[1])
            indeg[e[1]] += 1
        stack = [v for v in range(n) if indeg[v] == 0]
        seen = 0
        while stack:
            v = stack.pop()
            seen += 1
            for w in out[v]:
                indeg[w] -= 1
                if indeg[w] == 0:
                    stack.append(w)
        return seen == n


@register("tree")
class TreeGen(_GraphBase):
    """Trees with adversarial shapes. Options: ``nodes``, ``shape`` (random, path,
    star, caterpillar, binary, mixed), ``weighted``, ``weights``, ``format``
    (``edge_list``, ``edges``, ``adjacency_list``, ``parent``)."""

    formats = ("edge_list", "edges", "adjacency_list", "dict", "parent")

    def __init__(self, spec: dict[str, Any]):
        super().__init__({**spec, "directed": False})
        self.shape = spec.get("shape", "mixed")

    def _parents(self, rng: random.Random, n: int, shape: str) -> list[int]:
        parents = [-1] * n
        for i in range(1, n):
            if shape == "path":
                parents[i] = i - 1
            elif shape == "star":
                parents[i] = 0
            elif shape == "binary":
                parents[i] = (i - 1) // 2
            elif shape == "caterpillar":
                spine = max(1, n // 2)
                parents[i] = i - 1 if i < spine else rng.randrange(spine)
            else:
                parents[i] = rng.randrange(i)
        return parents

    def _make(self, rng: random.Random, n: int, size: float) -> Any:
        shape = self.shape
        if shape == "mixed":
            shape = rng.choice(["random", "random", "path", "star", "caterpillar", "binary"])
        parents = self._parents(rng, n, shape)
        labels = list(range(n))
        if self.format != "parent" or rng.random() < 0.5:
            rest = labels[1:]
            rng.shuffle(rest)
            labels = [0] + rest if self.format == "parent" else rng.sample(labels, n)
        pairs = []
        for i in range(1, n):
            a, b = labels[parents[i]], labels[i]
            pairs.append((a, b) if (self.format == "parent" or rng.random() < 0.5) else (b, a))
        if self.format != "parent":
            rng.shuffle(pairs)
        edges = [(u, v, self._weight(rng, size)) if self.weighted else (u, v) for u, v in pairs]
        return self.emit(n, edges)

    def generate(self, rng: random.Random, size: float) -> Any:
        return self._make(rng, sized_count(rng, max(1, self.n_lo), max(1, self.n_hi), size), size)

    def scaled(self, rng: random.Random, n: int) -> Any:
        return self._make(rng, max(1, self.n_lo, n), 1.0)

    def emit(self, n: int, edges: list[Edge]) -> Any:
        """``parent`` format: ``parent[i]`` (root = -1); weighted trees return
        ``(parent, weight)`` where ``weight[i]`` is the edge to i's parent."""
        if self.format != "parent":
            return super().emit(n, edges)
        parent = [-1] * n
        weight = [0] * n
        adj: list[list[tuple[int, Any]]] = [[] for _ in range(n)]
        for e in edges:
            w = e[2] if self.weighted else None
            adj[e[0]].append((e[1], w))
            adj[e[1]].append((e[0], w))
        if n:
            seen = {0}
            stack = [0]
            while stack:
                u = stack.pop()
                for v, w in adj[u]:
                    if v not in seen:
                        seen.add(v)
                        parent[v] = u + self.base
                        weight[v] = w
                        stack.append(v)
        return (parent, weight) if self.weighted else parent

    def parse(self, value: Any) -> tuple[int, list[Edge]] | None:
        if self.format != "parent":
            return super().parse(value)
        if self.weighted:
            if not (isinstance(value, tuple) and len(value) == 2):
                return None
            parent, weight = value
        else:
            parent, weight = value, None
        if not isinstance(parent, list):
            return None
        edges: list[Edge] = []
        for i, p in enumerate(parent):
            if p != -1:
                edges.append((p - self.base, i, weight[i]) if weight is not None else (p - self.base, i))
        return len(parent), edges

    def _valid(self, n: int, edges: list[Edge]) -> bool:
        return n >= max(1, self.n_lo) and len(edges) == n - 1 and _is_connected(n, edges)

    def shrink(self, value: Any) -> Iterator[Any]:
        parsed = self.parse(value)
        if parsed is None:
            return
        n, edges = parsed
        if n > max(1, self.n_lo):
            degree = [0] * n
            for e in edges:
                degree[e[0]] += 1
                degree[e[1]] += 1
            for v in range(n - 1, -1, -1):
                if degree[v] == 1 and not (self.format == "parent" and v == 0):
                    n2, e2 = self._remove_node(n, edges, v)
                    if self._valid(n2, e2):
                        yield self.emit(n2, e2)
        if self.weighted:
            for i, e in enumerate(edges):
                for w in self.weight_gen.shrink(e[2]):
                    new = list(edges)
                    new[i] = (e[0], e[1], w)
                    yield self.emit(n, new)

    def edge_cases(self) -> list[Any]:
        rng = random.Random(0)
        out = []
        for n in (1, 2, 3):
            if self.n_lo <= n <= self.n_hi:
                out.append(self._make(rng, n, 0.0))
        for shape in ("path", "star"):
            n = min(max(4, self.n_lo), self.n_hi)
            if n >= 4:
                parents = self._parents(rng, n, shape)
                w = (self.weight_gen.edge_cases() or [1])[0]
                out.append(self.emit(n, [(parents[i], i, w) if self.weighted else (parents[i], i) for i in range(1, n)]))
        return out
