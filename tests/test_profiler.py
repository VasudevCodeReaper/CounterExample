from counterexample import profile_empirical_complexity
from counterexample.engine.profiler import fit_models, normalize_complexity

LINEAR = "def f(a: list[int]) -> int:\n    return sum(a)\n"
QUADRATIC = (
    "def f(a: list[int]) -> int:\n"
    "    c = 0\n"
    "    for i in range(len(a)):\n"
    "        for j in range(i + 1, len(a)):\n"
    "            c += a[i] > a[j]\n"
    "    return c\n"
)


def test_fit_models_recovers_synthetic_curves():
    ns = [10, 100, 1000, 10000]
    assert fit_models(ns, [3 + 0.01 * n for n in ns])[0]["model"] == "O(N)"
    assert fit_models(ns, [1 + 1e-5 * n * n for n in ns])[0]["model"] == "O(N^2)"


def test_normalize_complexity():
    assert normalize_complexity("n log n") == "O(N log N)"
    assert normalize_complexity("O(N^2)") == "O(N^2)"
    assert normalize_complexity("quadratic") == "O(N^2)"
    assert normalize_complexity("weird") is None


def test_linear_is_ok():
    r = profile_empirical_complexity(LINEAR, None, [100, 1000, 10000, 100000], repeats=3)
    assert r["status"] == "OK", r["summary"]
    assert r["time_complexity"]["best_fit"] in ("O(N)", "O(N log N)")
    assert r["steps"][0]["peak_memory_kb"] is not None


def test_quadratic_is_flagged_as_regression():
    r = profile_empirical_complexity(QUADRATIC, None, [50, 200, 800, 1600], expected_complexity="O(N log N)", repeats=3)
    assert r["status"] == "FLAGGED"
    assert r["time_complexity"]["best_fit"] in ("O(N^2)", "O(N^2 log N)")
    assert any(f.startswith("REGRESSION") for f in r["flags"])
    assert any(f.startswith("SUPERLINEAR") for f in r["flags"])


def test_scale_flag_on_int_argument():
    code = "def fib(n: int) -> int:\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n"
    r = profile_empirical_complexity(code, {"args": [{"type": "int", "min": 0, "max": 10**6, "scale": True}]}, [1000, 4000, 16000], repeats=3)
    assert r["status"] in ("OK", "FLAGGED")
    assert r["time_complexity"]["asymptotic_slope"] > 0.8


def test_profile_errors():
    r = profile_empirical_complexity("def f(a: list[int]) -> int:\n    raise ValueError\n", None, [10, 100])
    assert r["status"] == "ERROR"
