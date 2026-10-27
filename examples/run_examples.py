"""Run every example and print the arena verdicts.

    python examples/run_examples.py            # all examples
    python examples/run_examples.py max_subarray merge_intervals
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from counterexample import profile_empirical_complexity, run_differential_arena

HERE = Path(__file__).resolve().parent

EXAMPLES = {
    "max_subarray": ("oracle.py", "candidate_buggy.py", {}),
    "max_subarray_fixed": ("oracle.py", "candidate_fixed.py", {"dir": "max_subarray"}),
    "merge_intervals": ("oracle.py", "candidate_buggy.py", {}),
    "shortest_path": ("oracle.py", "candidate.py", {}),
    "cpp_candidate": ("oracle.py", "candidate.cpp", {"requires": "cpp"}),
    "fastapi_migration": ("legacy_app.py", "new_app.py", {"requires": "fastapi"}),
}


def run(name: str) -> dict:
    oracle_file, candidate_file, opts = EXAMPLES[name]
    folder = HERE / opts.get("dir", name)
    spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    return run_differential_arena(
        (folder / oracle_file).read_text(encoding="utf-8"),
        (folder / candidate_file).read_text(encoding="utf-8"),
        spec,
        num_runs=3000,
    )


def main(names: list[str]) -> int:
    for name in names or list(EXAMPLES):
        result = run(name)
        print(f"\n=== {name}: {result['status']} ===")
        print(result.get("summary") or result.get("error"))
        if result.get("shrink_stats"):
            s = result["shrink_stats"]
            print(f"shrunk size {s['original_size']} -> {s['shrunk_size']} in {s['evaluations']} evaluations")
        if result.get("timing", {}).get("speedup_text"):
            print("speed:", result["timing"]["speedup_text"])
    fixed = (HERE / "max_subarray" / "candidate_fixed.py").read_text(encoding="utf-8")
    prof = profile_empirical_complexity(fixed, json.loads((HERE / "max_subarray" / "spec.json").read_text()), expected_complexity="O(N)")
    print(f"\n=== profile max_subarray (fixed) ===\n{prof['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
