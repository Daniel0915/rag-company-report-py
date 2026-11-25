import pytest

from app.agent.calculator import calculate


def test_calculate_basic_and_rejects_unsafe():
    assert calculate("(195.00 - 171.50) / 171.50 * 100") == pytest.approx(13.7026, rel=1e-4)
    assert calculate("-3 + 5") == 2
    for bad in ["__import__('os')", "2**10", "abs(-1)", "1/0", "a+1", "(1).real", "'a'", "1 +"]:
        with pytest.raises(ValueError):
            calculate(bad)


@pytest.mark.parametrize(
    "expr",
    [
        "*".join(["9" * 200] * 6),
        "1/" + "9" * 400 + "*" + "9" * 400,
        "(" * 500 + "1" + ")" * 500,
        "*".join(["1"] * 20000),
        "1e308*10",
        "1e308*10-1e308*10",
    ],
)
def test_calculate_rejects_overflow_deep_nesting_and_non_finite(expr):
    with pytest.raises(ValueError):
        calculate(expr)
