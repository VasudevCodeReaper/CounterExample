import json
import math
from fractions import Fraction

import pytest

from counterexample.codec import cpp_literal, decode, dumps, encode, js_literal, loads, py_literal, value_size

TRICKY = [
    None,
    True,
    0,
    -1,
    2**70,
    1.5,
    -0.0,
    "",
    "é\U0001F600\x00",
    [1, [2, (3, 4)]],
    (1,),
    {"a": 1, "b": [None]},
    {1: "x", (2, 3): [4]},
    {"$dollar": 1},
    {1, 2, 3},
    frozenset({"a"}),
    b"\x00\xff",
    complex(1, -2),
    Fraction(1, 3),
]


@pytest.mark.parametrize("value", TRICKY, ids=repr)
def test_roundtrip(value):
    text = dumps(value)
    json.loads(text)  # strict JSON (no NaN tokens)
    assert loads(text) == value
    assert type(loads(text)) is type(value)


def test_special_floats_roundtrip():
    for v in (float("inf"), float("-inf")):
        assert loads(dumps(v)) == v
    assert math.isnan(loads(dumps(float("nan"))))


def test_py_literal_evaluates_back():
    for value in TRICKY + [float("inf"), [float("-inf")]]:
        if isinstance(value, Fraction):
            continue
        assert eval(py_literal(value)) == value  # noqa: S307 - test of our own literal renderer


def test_py_literal_nan():
    assert py_literal(float("nan")) == "float('nan')"


def test_js_and_cpp_literals():
    assert js_literal([1, None, True, float("inf"), "x"]) == '[1, null, true, Infinity, "x"]'
    assert js_literal({1: 2}) == "new Map([[1, 2]])"
    assert cpp_literal([[1, 2], [3]]) == "{{1, 2}, {3}}"
    assert cpp_literal(3.0) == "3.0"
    assert cpp_literal(2**40) == f"{2**40}LL"


def test_value_size():
    assert value_size([1, 2, 3]) == 3
    assert value_size([]) == 0
    assert value_size((5, [[1, 2], [3, 4]])) == 5
    assert value_size("abc") == 3


def test_opaque_objects_encode_by_fields():
    class Point:
        def __init__(self, x, y):
            self.x, self.y = x, y

    a = decode(encode(Point(1, 2)))
    b = decode(encode(Point(1, 2)))
    c = decode(encode(Point(1, 3)))
    assert a == b and a != c
