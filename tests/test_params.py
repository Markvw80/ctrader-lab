import pytest

from ctlab.strategy.base import Strategy, all_strategies
from ctlab.strategy.params import BoolParam, ChoiceParam, FloatParam, IntParam


class Demo(Strategy):
    name = "_demo"
    n = IntParam(20, 5, 100)
    z = FloatParam(2.0, 1.0, 3.0)
    mode = ChoiceParam("a", ("a", "b"))
    flag = BoolParam(True)


def test_defaults_and_overrides():
    s = Demo(n=30)
    assert (s.n, s.z, s.mode, s.flag) == (30, 2.0, "a", True)
    assert s.params == {"n": 30, "z": 2.0, "mode": "a", "flag": True}
    assert set(Demo.param_specs()) == {"n", "z", "mode", "flag"}


@pytest.mark.parametrize("kw,exc", [
    ({"n": 4}, ValueError), ({"n": 101}, ValueError), ({"n": 2.5}, TypeError),
    ({"z": "x"}, TypeError), ({"mode": "c"}, ValueError), ({"nope": 1}, ValueError),
    ({"n": True}, TypeError),
])
def test_validation(kw, exc):
    with pytest.raises(exc):
        Demo(**kw)


def test_parse_cli_values():
    specs = Demo.param_specs()
    assert specs["n"].parse("7") == 7
    assert specs["flag"].parse("false") is False


def test_reference_strategies_registered_with_valid_defaults():
    names = set(all_strategies())
    assert {"session_breakout", "mean_reversion"} <= names
    for cls in all_strategies().values():
        cls()   # defaults must validate
