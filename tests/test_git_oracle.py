import textwrap

from conftest import requires_git

from counterexample import extract_git_oracle, run_differential_arena
from counterexample.engine.git_oracle import extract_cpp_symbol, extract_python_symbol

PY_V1 = textwrap.dedent(
    '''
    import math
    from collections import defaultdict

    LIMIT = 10

    def _clamp(x):
        return max(-LIMIT, min(LIMIT, x))

    def unrelated():
        return defaultdict(int)

    def score(values):
        """Original implementation."""
        return sum(_clamp(v) for v in values) + math.floor(0.5)

    class Ledger:
        def total(self, xs):
            return sum(xs)
    '''
)

PY_V2 = PY_V1.replace("return sum(_clamp(v) for v in values) + math.floor(0.5)", "return sum(values)")

CPP = textwrap.dedent(
    '''
    #include <vector>
    #include <algorithm>
    using namespace std;
    const int MOD = 1000000007;
    typedef long long ll;

    static ll helper(ll x) { return x % MOD; }
    int unused(int y) { return y; }

    ll solve(const vector<int>& a) {
        ll s = 0;
        for (int x : a) s = helper(s + x);
        return s;
    }

    class Solution {
    public:
        int twice(int x) { return x * 2; }
    };
    '''
)


def test_extract_python_symbol_with_dependencies():
    found = extract_python_symbol(PY_V1, "score")
    assert found["entrypoint"] == "score"
    code = found["code"]
    assert "import math" in code and "def _clamp" in code and "LIMIT = 10" in code
    assert "unrelated" not in code and "defaultdict" not in code
    assert sorted(found["dependencies"]) == ["LIMIT", "_clamp", "math"]
    compile(code, "<oracle>", "exec")


def test_extract_python_method():
    found = extract_python_symbol(PY_V1, "Ledger.total")
    assert found["entrypoint"] == "Ledger.total"
    assert "class Ledger" in found["code"]


def test_extract_cpp_symbol_with_dependencies():
    found = extract_cpp_symbol(CPP, "solve")
    code = found["code"]
    assert "#include <vector>" in code and "using namespace std;" in code
    assert "helper" in code and "const int MOD" in code and "typedef long long ll;" in code
    assert "unused" not in code
    method = extract_cpp_symbol(CPP, "Solution::twice")
    assert "class Solution" in method["code"] and method["entrypoint"] == "Solution::twice"


@requires_git
def test_extract_git_oracle_end_to_end(git_repo):
    src = git_repo / "pkg" / "scoring.py"
    src.parent.mkdir()
    src.write_text(PY_V1, encoding="utf-8")
    git_repo.git("add", ".")
    git_repo.git("commit", "-qm", "v1")
    src.write_text(PY_V2, encoding="utf-8")  # the "refactor" (it drops clamping!)

    r = extract_git_oracle("pkg/scoring.py", "score", "HEAD", repo_path=str(git_repo))
    assert r["status"] == "OK", r
    assert r["changed_since_ref"] is True
    assert "diff_vs_working_tree" in r
    assert r["commit_summary"]["subject"] == "v1"

    current = extract_python_symbol(PY_V2, "score", include_dependencies=True)["code"]
    arena = run_differential_arena(
        r["oracle_code"], current, {"type": "list", "elements": "int(-50,50)"}, num_runs=400, oracle_entrypoint=r["entrypoint"]
    )
    assert arena["status"] == "FALSIFIED"
    assert arena["shrunk_counterexample"]["call"] in ("score([11])", "score([-11])")


@requires_git
def test_extract_git_oracle_errors(git_repo):
    (git_repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git_repo.git("add", ".")
    git_repo.git("commit", "-qm", "init")
    r = extract_git_oracle("a.py", "g", repo_path=str(git_repo))
    assert r["status"] == "ERROR" and r["available_symbols"] == ["f"]
    r = extract_git_oracle("missing.py", "f", repo_path=str(git_repo))
    assert r["status"] == "ERROR"
    r = extract_git_oracle("a.py", "f", "no-such-ref", repo_path=str(git_repo))
    assert r["status"] == "ERROR"
    r = extract_git_oracle("a.py", "f", repo_path=str(git_repo))
    assert r["status"] == "OK" and r["changed_since_ref"] is False
