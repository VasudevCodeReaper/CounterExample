"""Composable adversarial fuzz generators.

>>> from counterexample.generators import CaseGenerator
>>> gen = CaseGenerator({"args": ["list[int(-5,5)]", {"type": "int", "min": 0, "max": 3}]})
"""

from __future__ import annotations

from counterexample.generators.base import (
    CaseGenerator,
    Generator,
    SpecError,
    available_types,
    build,
    parse_shorthand,
)
from counterexample.generators.infer import spec_from_cpp_type, spec_from_python

GENERATOR_REFERENCE = """\
# Counterexample generator_spec reference

A generator_spec is JSON. For a multi-argument function list one spec per
positional argument (and optionally kwargs):

    {"args": [<spec>, <spec>, ...], "kwargs": {"name": <spec>}}

A bare <spec> means a single-argument function. Omit generator_spec entirely to
infer it from Python type hints or the C++ signature.

## Shorthand strings
    "int"  "int(-5,5)"  "float(0,1)"  "bool"  "str"  "list[int(0,9)]"
    "tuple[int, str]"  "dict[str, int]"  "set[int]"  "optional[int]"
    "graph"  "dag"  "tree"  "intervals"  "matrix"  "grid"  "permutation"

## Scalars
- int: min, max (default full 32-bit range: INT_MIN/INT_MAX are hit), bits (8/16/32/64), unsigned
- float: min, max, allow_nan, allow_inf, special (= NaN + Inf)
- bool
- str: min_len, max_len (default 0..20), alphabet (ascii, lower, upper, digits, alnum,
  hex, binary, ab, abc, whitespace, brackets, printable, unicode, or literal chars)
- const: value          - choice: values          - one_of: options
- optional: of, null_prob

## Collections (boundary cases always included: empty, singleton, duplicates)
- list: elements, min_len, max_len (default 0..50), unique, sorted (true/"asc"/"desc"), dup_bias
- tuple: items          - set: elements, min_size, max_size
- dict: keys, values, min_size, max_size
- permutation: n (range), start

## Graphs
- graph: nodes (range, default 1..12), directed, weighted, weights (any int/float spec),
  negative_weights, negative_cycles, density, allow_self_loops, multi_edges, connected,
  format (edge_list -> (n, edges) | edges | adjacency_list | adjacency_matrix | dict),
  index_base, no_edge (matrix filler, e.g. "inf")
- digraph, negative_cycle_graph: graph presets
- dag: nodes, density, weighted, weights, format (labels are shuffled vs. topological order)
- tree: nodes, shape (mixed|random|path|star|caterpillar|binary), weighted, weights,
  format (edge_list | edges | adjacency_list | dict | parent)

## Intervals
- intervals: count (range, default 0..20), min, max (coords, default -100..100),
  allow_empty (zero-length), mode (mixed|random|nested|overlapping|touching|disjoint|duplicates),
  sorted, format (list|tuple), float, resolution
- interval: a single [lo, hi]

## Matrices & grids
- matrix: rows, cols (ranges, default 0..6), square, elements (spec),
  kind (mixed|dense|sparse|singular|identity|zero|diagonal|symmetric|triangular), density
- grid: rows, cols (default 0..8), cells (default [0, 1]; first = open), density,
  format (lists|strings)

## HTTP (FastAPI adapter)
- http_request: method (or list), path ("/items/{id}"), path_params, query, json, headers

## Call-level helpers
- {"type": "len_of", "arg": 1, "offset": 0}: derived argument = len(args[1]) + offset
- "spread": true on an argument: splat a tuple value (e.g. a graph's (n, edges)) into
  separate positional arguments
- "scale": true on an int argument: profile_empirical_complexity sets it to N
"""

__all__ = [
    "GENERATOR_REFERENCE",
    "CaseGenerator",
    "Generator",
    "SpecError",
    "available_types",
    "build",
    "parse_shorthand",
    "spec_from_cpp_type",
    "spec_from_python",
]
