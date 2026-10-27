# Examples

Each folder holds a dead-simple oracle, an optimized candidate with a realistic bug, and the
`spec.json` used to falsify it. Run them all:

```bash
python examples/run_examples.py
```

| Example | Oracle | Candidate | Minimal counterexample |
| --- | --- | --- | --- |
| `max_subarray` | O(N²) brute force | Kadane that returns 0 for all-negative input | `max_subarray([-1])` |
| `max_subarray` (fixed) | O(N²) brute force | correct Kadane | VERIFIED, ~7x faster, profiled O(N) |
| `merge_intervals` | point-overlap fixpoint | sort + sweep that ignores touching ranges | `merge([[0, 0], [0, 0]])` |
| `shortest_path` | Bellman-Ford | Dijkstra on a DAG with negative weights | `shortest(3, [[0, 2, 0], [0, 1, 1], [1, 2, -2]], 0)` |
| `cpp_candidate` | Python O(N²) DP | C++ patience sort using `upper_bound` | `Solution::lengthOfLIS([0, 0])` |
| `fastapi_migration` | legacy FastAPI app | rewrite whose coupon threshold drifted | `{"prices": [100], "coupon": "HALF"}` |

From the CLI:

```bash
counterexample arena --oracle examples/merge_intervals/oracle.py \
                     --candidate examples/merge_intervals/candidate_buggy.py \
                     --spec @examples/merge_intervals/spec.json --save-payload payload.json
counterexample lock payload.json tests/test_merge_regressions.py --module my_project.intervals
```
