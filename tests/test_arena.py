import json

from counterexample import run_differential_arena

BRUTE_MAX_SUBARRAY = '''
def max_subarray(nums: list[int]) -> int:
    best = nums[0]
    for i in range(len(nums)):
        total = 0
        for j in range(i, len(nums)):
            total += nums[j]
            best = max(best, total)
    return best
'''

BUGGY_KADANE = '''
def max_subarray(nums: list[int]) -> int:
    best = cur = 0
    for x in nums:
        cur = max(0, cur + x)
        best = max(best, cur)
    return best
'''

GOOD_KADANE = '''
def max_subarray(nums: list[int]) -> int:
    best = cur = nums[0]
    for x in nums[1:]:
        cur = max(x, cur + x)
        best = max(best, cur)
    return best
'''

NONEMPTY = {"type": "list", "elements": {"type": "int", "min": -50, "max": 50}, "min_len": 1, "max_len": 40}


def test_verified_with_speedup():
    r = run_differential_arena(BRUTE_MAX_SUBARRAY, GOOD_KADANE, NONEMPTY, num_runs=1500)
    assert r["status"] == "VERIFIED", r
    assert r["pass_rate"] == 1.0
    assert r["runs"]["valid"] == 1500
    assert r["timing"]["speedup"] > 1
    assert r["timing"]["speedup_text"].endswith("faster")
    json.dumps(r)  # MCP-safe


def test_falsified_minimal_counterexample_and_payload():
    r = run_differential_arena(BRUTE_MAX_SUBARRAY, BUGGY_KADANE, NONEMPTY, num_runs=1000)
    assert r["status"] == "FALSIFIED"
    assert r["failure_kind"] == "mismatch"
    assert r["shrunk_counterexample"]["call"] == "max_subarray([-1])"
    assert r["oracle_output"] == "-1" and r["candidate_output"] == "0"
    assert r["diff"][0]["path"] == "$"
    assert r["counterexample_payload"]["expected"] == -1
    assert r["candidate_trace"]["events"]
    json.dumps(r)


def test_shrinks_large_failure():
    oracle = "def f(a: list[int]) -> list[int]:\n    return sorted(a)\n"
    cand = (
        "def f(a: list[int]) -> list[int]:\n"
        "    out = sorted(a)\n"
        "    if len(a) > 35 and any(x < 0 for x in a):\n"
        "        out[-1] += 1\n"
        "    return out\n"
    )
    spec = {"type": "list", "elements": "int(-1000,1000)", "min_len": 30, "max_len": 50}
    r = run_differential_arena(oracle, cand, spec, num_runs=200, edge_cases=False)
    assert r["status"] == "FALSIFIED"
    assert r["shrunk_counterexample"]["size"] == 36  # respects min_len=30, needs > 35
    assert r["shrink_stats"]["original_size"] >= 30
    assert r["shrunk_counterexample"]["call"].count("-1") == 1


def test_candidate_exception_timeout_and_crash():
    oracle = "def f(x: int) -> int:\n    return x\n"
    raising = "def f(x: int) -> int:\n    if x == 3:\n        raise KeyError(x)\n    return x\n"
    r = run_differential_arena(oracle, raising, "int(0,9)", num_runs=10)
    assert r["status"] == "FALSIFIED" and r["failure_kind"] == "candidate_exception:KeyError"
    assert r["candidate_error"]["line"] == 3

    hanging = "def f(x: int) -> int:\n    while x == 3:\n        pass\n    return x\n"
    r = run_differential_arena(oracle, hanging, "int(0,9)", num_runs=10, timeout_ms=300)
    assert r["status"] == "FALSIFIED" and r["failure_kind"] == "candidate_timeout"
    assert r["shrunk_counterexample"]["call"] == "f(3)"

    crashing = "import os\ndef f(x: int) -> int:\n    if x == 3:\n        os._exit(9)\n    return x\n"
    r = run_differential_arena(oracle, crashing, "int(0,9)", num_runs=10)
    assert r["status"] == "FALSIFIED" and r["failure_kind"] == "candidate_crash"


def test_oracle_errors_discard_vs_fail():
    oracle = "def f(x: int) -> float:\n    if x == 0:\n        raise ZeroDivisionError\n    return 1 / x\n"
    cand = "def f(x: int) -> float:\n    return 1 / x if x else 0.0\n"
    r = run_differential_arena(oracle, cand, "int(-3,3)", num_runs=20)
    assert r["status"] == "VERIFIED"
    assert r["runs"]["discarded"] == 1 and r["warnings"]
    r = run_differential_arena(oracle, cand, "int(-3,3)", num_runs=20, oracle_errors="fail")
    assert r["status"] == "FALSIFIED" and r["failure_kind"] == "unexpected_success"
    assert r["counterexample_payload"]["expected_kind"] == "exception"


def test_inplace_mutation_is_compared():
    oracle = "def s(a: list[int]) -> None:\n    a.sort()\n"
    cand = "def s(a: list[int]) -> None:\n    a.sort()\n    if len(a) > 2:\n        a.pop()\n"
    r = run_differential_arena(oracle, cand, None, num_runs=300)
    assert r["status"] == "FALSIFIED"
    assert r["counterexample_payload"]["expected_kind"] == "args_after"
    assert r["shrunk_counterexample"]["call"] == "s([0, 0, 0])"


def test_comparators():
    oracle = "def f(a: list[int]) -> list[int]:\n    return sorted(set(a))\n"
    cand = "def f(a: list[int]) -> list[int]:\n    return list(set(a))\n"
    assert run_differential_arena(oracle, cand, "list[int(-99,99)]", num_runs=300)["status"] == "FALSIFIED"
    assert run_differential_arena(oracle, cand, "list[int(-99,99)]", num_runs=300, comparator="unordered")["status"] == "VERIFIED"
    fo = "def g(a: list[float]) -> float:\n    return sum(a)\n"
    fc = "import math\ndef g(a: list[float]) -> float:\n    return math.fsum(sorted(a))\n"
    spec = {"type": "list", "elements": {"type": "float", "min": -1, "max": 1}}
    assert run_differential_arena(fo, fc, spec, num_runs=300)["status"] == "VERIFIED"


def test_solution_class_and_inferred_spec():
    oracle = "class Solution:\n    def twoSum(self, nums: list[int], target: int) -> bool:\n        return any(nums[i] + nums[j] == target for i in range(len(nums)) for j in range(i + 1, len(nums)))\n"
    cand = "class Solution:\n    def twoSum(self, nums: list[int], target: int) -> bool:\n        seen = set()\n        for x in nums:\n            if target - x in seen:\n                return True\n            seen.add(x)\n        return False\n"
    r = run_differential_arena(oracle, cand, {"args": ["list[int(-9,9)]", "int(-18,18)"]}, num_runs=800)
    assert r["status"] == "VERIFIED", r.get("summary")
    assert r["oracle"]["entrypoint"] == "Solution.twoSum"


def test_errors_are_reported():
    r = run_differential_arena("def f(x):\n  return x", "def f(x) return", "int", num_runs=5)
    assert r["status"] == "ERROR" and "syntax error" in r["error"]
    r = run_differential_arena("def f(x):\n  return x", "def f(x):\n  return x", None, num_runs=5)
    assert r["status"] == "ERROR" and r["phase"] == "spec"
    r = run_differential_arena("def f(x):\n  return x", "def f(x):\n  return x", {"type": "bogus"}, num_runs=5)
    assert r["status"] == "ERROR"


def test_deterministic_given_seed():
    a = run_differential_arena(BRUTE_MAX_SUBARRAY, BUGGY_KADANE, NONEMPTY, num_runs=300, seed=11, edge_cases=False)
    b = run_differential_arena(BRUTE_MAX_SUBARRAY, BUGGY_KADANE, NONEMPTY, num_runs=300, seed=11, edge_cases=False)
    assert a["shrunk_counterexample"] == b["shrunk_counterexample"]
    assert a["runs"] == b["runs"]


def test_inconclusive_when_oracle_rejects_everything():
    oracle = "def f(x: int) -> int:\n    raise ValueError('nope')\n"
    r = run_differential_arena(oracle, "def f(x: int) -> int:\n    return x\n", "int(0,5)", num_runs=6)
    assert r["status"] == "INCONCLUSIVE"


def test_stdout_noise_does_not_break_protocol():
    noisy = "def f(x: int) -> int:\n    print('debug', x)\n    import sys\n    sys.stdout.write('{\\\"ok\\\": false}\\n')\n    return x\n"
    r = run_differential_arena("def f(x: int) -> int:\n    return x\n", noisy, "int(0,99)", num_runs=200)
    assert r["status"] == "VERIFIED"


def test_async_entrypoints_are_awaited():
    oracle = "async def solve(x: int) -> int:\n    return x * 2\n"
    candidate = "async def solve(x: int) -> int:\n    return x * 2 + 1\n"
    r = run_differential_arena(oracle, candidate, {"type": "int", "min": 0, "max": 3}, num_runs=30)

    assert r["status"] == "FALSIFIED"
    assert r["shrunk_counterexample"]["call"] == "solve(0)"
    # Display fields are repr strings; receiving concrete numbers here proves
    # the workers awaited the coroutines instead of comparing their reprs.
    assert r["oracle_output"] == "0"
    assert r["candidate_output"] == "1"
