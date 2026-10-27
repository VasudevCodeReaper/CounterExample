"""The delta shrinker against in-process predicates (no sandbox needed)."""

import random

from counterexample.engine.shrinker import DeltaShrinker
from counterexample.generators import CaseGenerator


def shrink_with(spec, predicate, seed=0, tries=2000):
    gen = CaseGenerator(spec)
    rng = random.Random(seed)
    for i in range(tries):
        case = gen.generate(rng, min(1.0, 0.1 + i / 200))
        args, kwargs = gen.materialize(case)
        if predicate(*args, **kwargs):
            break
    else:
        raise AssertionError("no failing input generated")

    def evaluate(batch):
        for idx, cand in enumerate(batch):
            a, k = gen.materialize(cand)
            if predicate(*a, **k):
                return idx, True
        return None

    result = DeltaShrinker(gen, evaluate, max_evals=20000, budget_s=30).shrink(case, True)
    return gen.materialize(result.case)[0], result


def test_sum_threshold_shrinks_to_single_element():
    args, res = shrink_with({"type": "list", "elements": {"type": "int", "min": 0, "max": 1000}, "max_len": 50}, lambda a: sum(a) > 500)
    assert args == [[501]]
    assert res.local_minimum


def test_length_threshold_is_exact():
    args, _ = shrink_with({"type": "list", "elements": "int(-1000,1000)", "max_len": 60}, lambda a: len(a) >= 20)
    assert args == [[0] * 20]


def test_duplicate_bug_shrinks_to_pair():
    args, _ = shrink_with({"type": "list", "elements": "int(-50,50)", "max_len": 50}, lambda a: len(set(a)) != len(a))
    assert args == [[0, 0]]


def test_negative_then_zero_pattern():
    # Mirrors the README example: a reducer that mishandles a negative between zeros.
    def bad(a):
        return any(a[i] == 0 and a[i + 1] < 0 and a[i + 2] == 0 for i in range(len(a) - 2))

    args, _ = shrink_with({"type": "list", "elements": "int(-9,9)", "max_len": 50}, bad, tries=20000)
    assert args == [[0, -1, 0]]


def test_graph_cycle_shrinks_to_minimal_cycle():
    from counterexample.generators.graphs import DagGen

    def has_cycle(n, edges):
        return not DagGen._acyclic((n, [tuple(e) for e in edges]))

    args, _ = shrink_with({"type": "graph", "directed": True, "nodes": [1, 12], "spread": True}, has_cycle)
    n, edges = args
    assert n == 2 and len(edges) == 2


def test_overlapping_intervals_translate_to_origin():
    def overlaps(ivs):
        s = sorted(ivs)
        return any(s[i][1] > s[i + 1][0] for i in range(len(s) - 1))

    args, _ = shrink_with({"type": "intervals", "allow_empty": False}, overlaps)
    (ivs,) = args
    assert len(ivs) == 2
    assert min(a for a, _ in ivs) == 0
    assert max(b for _, b in ivs) <= 3
