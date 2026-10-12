import pytest

from ledger_api.money import Money, UnknownCurrency


def test_rejects_unknown_currency() -> None:
    with pytest.raises(UnknownCurrency):
        Money(100, "XYZ")


@pytest.mark.parametrize("amount", [1.5, True, "100"])
def test_rejects_non_integer_amounts(amount: object) -> None:
    with pytest.raises(TypeError):
        Money(amount, "EUR")  # type: ignore[arg-type]
