import json

import pytest
from conftest import requires_cxx

from counterexample import profile_empirical_complexity, run_differential_arena
from counterexample.adapters.cpp_outline import outline

pytestmark = [pytest.mark.cpp, requires_cxx]

PY_ORACLE = '''
def max_subarray(nums: list[int]) -> int:
    best = nums[0]
    for i in range(len(nums)):
        s = 0
        for j in range(i, len(nums)):
            s += nums[j]
            best = max(best, s)
    return best
'''

CPP_BUGGY = r'''
#include <bits/stdc++.h>
using namespace std;
class Solution {
public:
    int maxSubArray(vector<int>& nums) {
        int best = 0, cur = 0;
        for (int x : nums) { cur = max(0, cur + x); best = max(best, cur); }
        return best;
    }
};
'''

CPP_FIXED = CPP_BUGGY.replace("int best = 0, cur = 0;\n        for (int x : nums) { cur = max(0, cur + x); best = max(best, cur); }",
                              "int best = nums[0], cur = 0;\n        for (int x : nums) { cur = max(x, cur + x); best = max(best, cur); }")

SPEC = {"type": "list", "elements": "int(-50,50)", "min_len": 1, "max_len": 30}


def test_outline_finds_methods():
    names = [d.qualified for d in outline(CPP_BUGGY)]
    assert "Solution::maxSubArray" in names


def test_cross_language_falsified():
    r = run_differential_arena(PY_ORACLE, CPP_BUGGY, SPEC, num_runs=500)
    assert r["status"] == "FALSIFIED", r.get("error") or r.get("details")
    assert r["shrunk_counterexample"]["call"] == "Solution::maxSubArray([-1])"
    assert r["counterexample_payload"]["cpp_signature"]["params"] == ["vector<int>&"]
    json.dumps(r)


def test_cross_language_verified_and_fast():
    r = run_differential_arena(PY_ORACLE, CPP_FIXED, SPEC, num_runs=2000)
    assert r["status"] == "VERIFIED", r.get("summary")
    assert r["timing"]["speedup"] > 1


def test_cpp_vs_cpp_with_exceptions_and_inferred_spec():
    oracle = "#include <vector>\nlong long total(std::vector<int> v) { long long s = 0; for (int x : v) s += x; return s; }\n"
    cand = "#include <vector>\n#include <stdexcept>\nlong long total(std::vector<int> v) { if (v.size() == 3) throw std::length_error(\"three\"); long long s = 0; for (int x : v) s += x; return s; }\n"
    r = run_differential_arena(oracle, cand, None, num_runs=300)
    assert r["status"] == "FALSIFIED"
    assert r["failure_kind"] == "candidate_exception:std::length_error"
    assert r["shrunk_counterexample"]["call"] == "total([0, 0, 0])"


def test_overflow_bug_found_with_int_extremes():
    oracle = "def mid(a: int, b: int) -> int:\n    return (a + b) // 2 if (a + b) >= 0 else -((-(a + b)) // 2)\n"
    cand = "int mid(int a, int b) { return (a + b) / 2; }\n"
    r = run_differential_arena(oracle, cand, {"args": ["int", "int"]}, num_runs=500, oracle_language="python")
    assert r["status"] == "FALSIFIED"


def test_compile_error_is_reported():
    r = run_differential_arena(PY_ORACLE, "int f(int x) { return x + ; }", SPEC, num_runs=5, candidate_language="cpp")
    assert r["status"] == "ERROR" and r["phase"] == "compile"
    assert "candidate.cpp:1" in r["details"]["compiler_output"]


def test_cpp_profile():
    code = "#include <vector>\n#include <algorithm>\nstd::vector<int> s(std::vector<int> v) { std::sort(v.begin(), v.end()); return v; }\n"
    r = profile_empirical_complexity(code, None, [1000, 10000, 100000], repeats=3)
    assert r["status"] in ("OK", "FLAGGED"), r
    assert r["time_complexity"]["best_fit"] in ("O(N)", "O(N log N)")
