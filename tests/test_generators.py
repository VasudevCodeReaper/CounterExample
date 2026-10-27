import random

import pytest

from counterexample.engine.shrinker import complexity
from counterexample.generators import CaseGenerator, SpecError, build, parse_shorthand, spec_from_cpp_type, spec_from_python

SPECS = [
    {"type": "int"},
    {"type": "int", "min": 5, "max": 9},
    {"type": "int", "bits": 64},
    {"type": "float", "special": True},
    {"type": "bool"},
    {"type": "str", "alphabet": "unicode"},
    {"type": "str", "alphabet": "ab", "min_len": 2, "max_len": 6},
    {"type": "list", "elements": {"type": "int", "min": -3, "max": 3}, "min_len": 1, "max_len": 30},
    {"type": "list", "unique": True, "sorted": True},
    {"type": "tuple", "items": ["int", "str"]},
    {"type": "dict", "keys": "str", "values": "int"},
    {"type": "set", "elements": "int"},
    {"type": "permutation", "n": [0, 12]},
    {"type": "choice", "values": ["a", "b", "c"]},
    {"type": "one_of", "options": ["int", "str"]},
    {"type": "optional", "of": "int"},
    {"type": "graph"},
    {"type": "graph", "directed": True, "weighted": True, "negative_cycles": True},
    {"type": "graph", "connected": True, "nodes": [2, 10]},
    {"type": "graph", "format": "adjacency_matrix", "weighted": True},
    {"type": "graph", "format": "adjacency_list"},
    {"type": "graph", "format": "dict", "index_base": 1},
    {"type": "dag", "format": "edges"},
    {"type": "tree"},
    {"type": "tree", "format": "parent", "weighted": True},
    {"type": "intervals"},
    {"type": "intervals", "allow_empty": False, "sorted": True, "format": "tuple"},
    {"type": "intervals", "float": True},
    {"type": "interval"},
    {"type": "matrix"},
    {"type": "matrix", "square": True, "kind": "singular", "rows": [2, 5]},
    {"type": "grid", "cells": [".", "#"], "format": "strings"},
    {"type": "http_request", "method": "POST", "path": "/x/{id}", "path_params": {"id": "int(0,5)"}, "json": "list[int]"},
]


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: str(s)[:60])
def test_generate_edge_shrink_are_valid(spec):
    gen = build(spec)
    rng = random.Random(42)
    for i in range(150):
        v = gen.generate(rng, i / 150)
        assert gen.validate(v), (spec, v)
    for v in gen.edge_cases():
        assert gen.validate(v), (spec, v)
    for _ in range(30):
        v = gen.generate(rng, 1.0)
        for cand in list(gen.shrink(v))[:200]:
            assert gen.validate(cand), (spec, v, cand)


@pytest.mark.parametrize("spec", [s for s in SPECS if build(s).scalable], ids=lambda s: str(s)[:60])
def test_scaled_hits_requested_size(spec):
    case_gen = CaseGenerator(spec)
    small = case_gen.size_of(case_gen.scaled(random.Random(0), 10))
    big = case_gen.size_of(case_gen.scaled(random.Random(0), 1000))
    assert big > small


def test_shrink_candidates_are_never_more_complex():
    case_gen = CaseGenerator({"type": "list", "elements": {"type": "int", "min": -100, "max": 100}})
    case = {"args": [[50, -7, 3, 3, 99]], "kwargs": {}}
    bound = complexity(case_gen, case)
    cands = list(case_gen.shrink(case))
    assert cands
    assert sum(complexity(case_gen, c) < bound for c in cands) >= len(cands) * 0.9


def test_generation_is_deterministic():
    gen = CaseGenerator({"args": ["list[int]", {"type": "graph"}]})
    a = [gen.generate(random.Random(7), 0.5) for _ in range(5)]
    b = [gen.generate(random.Random(7), 0.5) for _ in range(5)]
    assert a == b


def test_int_defaults_hit_int_min_and_max():
    edges = build("int").edge_cases()
    assert -(2**31) in edges and 2**31 - 1 in edges and 0 in edges and -1 in edges


def test_list_edge_cases_include_empty_singleton_duplicates():
    edges = build("list[int]").edge_cases()
    assert [] in edges and [0] in edges and [0, 0] in edges


def test_shorthand():
    assert parse_shorthand("list[int(-5,5)]") == {"type": "list", "elements": {"type": "int", "min": -5, "max": 5}}
    assert parse_shorthand("dict[str, list[int]]")["values"]["type"] == "list"
    assert parse_shorthand("tuple[int, float(0,1)]")["items"][1] == {"type": "float", "min": 0, "max": 1}
    with pytest.raises(SpecError):
        parse_shorthand("list[")
    with pytest.raises(SpecError):
        build({"type": "nope"})


def test_len_of_and_spread():
    gen = CaseGenerator({"args": [{"type": "len_of", "arg": 1}, "list[int]", {"type": "graph", "spread": True}]})
    case = gen.generate(random.Random(1), 0.5)
    args, _ = gen.materialize(case)
    assert args[0] == len(args[1])
    assert len(args) == 4  # n, list, graph_n, graph_edges
    for cand in list(gen.shrink(case))[:50]:
        a, _ = gen.materialize(cand)
        assert a[0] == len(a[1])


def test_negative_cycle_graph_always_has_negative_cycle():
    gen = build({"type": "negative_cycle_graph", "nodes": [3, 8]})
    rng = random.Random(3)
    for _ in range(30):
        n, edges = gen.generate(rng, 1.0)
        # Bellman-Ford from a virtual source detects any negative cycle.
        dist = [0] * n
        for _ in range(n):
            for u, v, w in edges:
                if dist[u] + w < dist[v]:
                    dist[v] = dist[u] + w
        assert any(dist[u] + w < dist[v] for u, v, w in edges)


def test_dag_is_acyclic_and_tree_is_tree():
    from counterexample.generators.graphs import DagGen, _is_connected

    rng = random.Random(5)
    dag = build({"type": "dag"})
    tree = build({"type": "tree"})
    for _ in range(50):
        assert DagGen._acyclic(dag.parse(dag.generate(rng, 1.0)))
        n, edges = tree.generate(rng, 1.0)
        assert len(edges) == n - 1 and _is_connected(n, edges)


def test_spec_inference_python_and_cpp():
    code = "def f(a: list[int], k: int, m: dict[str, float] | None = None, *, flag: bool = False) -> int: ..."
    spec = spec_from_python(code, "f")
    assert spec == {"args": [{"type": "list", "elements": {"type": "int"}}, {"type": "int"}]}
    assert spec_from_cpp_type("const std::vector<std::pair<int, std::string>>&") == {
        "type": "list",
        "elements": {"type": "tuple", "items": [{"type": "int", "bits": 32}, {"type": "str", "alphabet": "ascii"}]},
    }
    with pytest.raises(SpecError):
        spec_from_python("def f(x): ...", "f")
