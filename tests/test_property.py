from counterexample import run_property_arena


def test_property_arena_finds_and_shrinks_an_invariant_violation():
    candidate = "def normalize(values: list[int]) -> list[int]:\n    return sorted(set(values))\n"
    property_code = '''
def property(result, values):
    return result == sorted(values) and len(result) == len(values)
'''

    result = run_property_arena(candidate, property_code, num_runs=100)

    assert result["status"] == "FALSIFIED", result
    assert result["mode"] == "property"
    assert result["property"]["kind"] == "invariant"
    assert result["shrunk_counterexample"]["call"] == "normalize([0, 0])"
    assert "__counterexample_property" not in result["summary"]
    assert result["property_counterexample"]["args"] == [[0, 0]]
    assert "counterexample_payload" not in result


def test_property_arena_supports_metamorphic_relations():
    candidate = '''
def total(values: list[int]) -> int:
    return sum(values) + int(values == sorted(values))
'''
    property_code = '''
def property(result, values):
    return result == total(list(reversed(values)))
'''

    result = run_property_arena(candidate, property_code, "list[int(-3,3)]", num_runs=200)

    assert result["status"] == "FALSIFIED", result
    assert result["property"]["kind"] == "metamorphic"


def test_property_arena_accepts_async_candidate_and_property_with_inferred_spec():
    candidate = "async def twice(value: int) -> int:\n    return value * 2\n"
    property_code = "async def property(result, value):\n    return result == value * 2\n"

    result = run_property_arena(candidate, property_code, num_runs=30)

    assert result["status"] == "VERIFIED", result
    assert result["runs"]["valid"] == 30
    assert result["summary"].startswith("PROPERTY VERIFIED")
    assert result["warnings"]


def test_property_receives_the_original_input_when_candidate_mutates_it():
    candidate = '''
def sort_in_place(values: list[int]) -> None:
    values.sort()
'''
    property_code = '''
def property(result, values):
    return values == sorted(values)
'''

    result = run_property_arena(candidate, property_code, "list[int(-3,3)]", num_runs=100)

    assert result["status"] == "FALSIFIED", result


def test_property_arena_supports_solution_methods():
    candidate = '''
class Solution:
    def twice(self, value: int) -> int:
        return value * 2
'''
    property_code = "def property(result, value):\n    return result == value * 2\n"

    result = run_property_arena(candidate, property_code, num_runs=30)

    assert result["status"] == "VERIFIED", result
    assert result["property"]["candidate_entrypoint"] == "Solution.twice"


def test_property_kind_does_not_confuse_a_single_character_entrypoint_with_a_relation():
    candidate = "def f(value: int) -> int:\n    return value\n"
    property_code = "def property(result, value):\n    return result == value\n"

    result = run_property_arena(candidate, property_code, num_runs=20)

    assert result["status"] == "VERIFIED"
    assert result["property"]["kind"] == "invariant"


def test_property_arena_rejects_a_missing_property_entrypoint():
    result = run_property_arena("def f(x: int) -> int:\n    return x\n", "def check(result, x):\n    return True\n", "int")

    assert result["status"] == "ERROR"
    assert "must define" in result["error"]
