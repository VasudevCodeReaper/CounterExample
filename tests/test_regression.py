import subprocess
import sys

from counterexample import lock_regression_test, run_differential_arena
from counterexample.codec import encode

BRUTE = "def max_subarray(nums: list[int]) -> int:\n    return max(sum(nums[i:j]) for i in range(len(nums)) for j in range(i + 1, len(nums) + 1))\n"
BUGGY = "def max_subarray(nums: list[int]) -> int:\n    best = cur = 0\n    for x in nums:\n        cur = max(0, cur + x)\n        best = max(best, cur)\n    return best\n"
FIXED = "def max_subarray(nums: list[int]) -> int:\n    best = cur = nums[0]\n    for x in nums[1:]:\n        cur = max(x, cur + x)\n        best = max(best, cur)\n    return best\n"


def _payload():
    r = run_differential_arena(BRUTE, BUGGY, {"type": "list", "elements": "int(-9,9)", "min_len": 1}, num_runs=300)
    assert r["status"] == "FALSIFIED"
    return r["counterexample_payload"]


def test_pytest_lock_runs_and_is_idempotent(tmp_path):
    (tmp_path / "algos.py").write_text(FIXED, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    test_file = tmp_path / "tests" / "test_locked.py"
    payload = _payload()

    r = lock_regression_test(payload, str(test_file), "pytest")
    assert r["status"] == "LOCKED", r
    assert r["import_line"] == "from algos import max_subarray"
    text = test_file.read_text(encoding="utf-8")
    assert "result = max_subarray([-1])\n    assert result == -1" in text

    again = lock_regression_test(payload, str(test_file), "pytest")
    assert again["status"] == "ALREADY_LOCKED"

    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", str(test_file)], cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    (tmp_path / "algos.py").write_text(BUGGY, encoding="utf-8")
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test_file)], cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode != 0  # the locked test catches the regression


def test_float_exception_and_inplace_payloads(tmp_path):
    base = {"schema": "counterexample/v1", "function": "f", "kwargs": {}, "comparator": "auto", "float_tolerance": 1e-9, "failure_kind": "mismatch"}
    t = tmp_path / "test_x.py"
    lock_regression_test({**base, "args": encode([[0.1, 0.2]]), "expected_kind": "return", "expected": 0.30000000000000004}, str(t), module="m")
    lock_regression_test({**base, "args": encode([0]), "expected_kind": "exception", "expected_exception": "ZeroDivisionError", "expected": None}, str(t), module="m")
    lock_regression_test({**base, "args": encode([[3, 1]]), "expected_kind": "args_after", "expected": encode([[1, 3]])}, str(t), module="m")
    text = t.read_text(encoding="utf-8")
    assert text.count("from m import f") == 1
    assert "_counterexample_equal(result, 0.30000000000000004" in text
    assert "with pytest.raises(ZeroDivisionError):" in text and "import pytest" in text
    assert "args = [[3, 1]]" in text and "assert args == [[1, 3]]" in text
    compile(text, str(t), "exec")


def test_jest_and_gtest_rendering(tmp_path):
    payload = {
        "schema": "counterexample/v1",
        "function": "Solution::maxSubArray",
        "args": encode([[-1]]),
        "kwargs": {},
        "expected_kind": "return",
        "expected": -1,
        "comparator": "auto",
        "cpp_signature": {"params": ["vector<int>&"], "return": "int"},
    }
    js = tmp_path / "max.test.js"
    r = lock_regression_test(payload, str(js), "jest", module="./max")
    assert r["status"] == "LOCKED"
    text = js.read_text(encoding="utf-8")
    assert 'const { Solution } = require("./max");' in text
    assert "new Solution().maxSubArray([-1])" in text and "toEqual(-1)" in text

    cc = tmp_path / "max_test.cc"
    r = lock_regression_test(payload, str(cc), "gtest", module="max.h")
    text = cc.read_text(encoding="utf-8")
    assert "#include <gtest/gtest.h>" in text and '#include "max.h"' in text
    assert "std::vector<int> arg0 = {-1};" in text
    assert "solver.maxSubArray(arg0)" in text and "EXPECT_EQ(result, expected);" in text


def test_bad_payload():
    assert lock_regression_test({"nope": 1}, "x.py")["status"] == "ERROR"
    assert lock_regression_test({"args": [], "function": "f"}, "x.py", "mocha")["status"] == "ERROR"
