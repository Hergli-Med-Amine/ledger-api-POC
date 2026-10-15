from decimal import Decimal

import pytest

from ledger_api.money import Money, UnknownCurrency, convert


def test_rejects_unknown_currency() -> None:
    with pytest.raises(UnknownCurrency):
        Money(100, "XYZ")


@pytest.mark.parametrize("amount", [1.5, True, "100"])
def test_rejects_non_integer_amounts(amount: object) -> None:
    with pytest.raises(TypeError):
        Money(amount, "EUR")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("amount", "to_currency", "rate", "expected"),
    [
        (Money(100_00, "EUR"), "USD", Decimal("1.0850"), Money(108_50, "USD")),
        # Minor units differ: 1.00 EUR (100 cents) is 162.35 JPY, and JPY has no minor unit.
        (Money(1_00, "EUR"), "JPY", Decimal("162.35"), Money(162, "JPY")),
        (Money(1_000, "JPY"), "EUR", Decimal("0.006159"), Money(6_16, "EUR")),
        (Money(1_00, "EUR"), "KWD", Decimal("0.3335"), Money(334, "KWD")),  # 0.334 KWD
        # Exact ties round to the even neighbour, not always up.
        (Money(1, "EUR"), "USD", Decimal("0.5"), Money(0, "USD")),
        (Money(3, "EUR"), "USD", Decimal("0.5"), Money(2, "USD")),
        (Money(5, "EUR"), "USD", Decimal("0.5"), Money(2, "USD")),
        # The largest amount the API accepts converts without losing digits.
        (Money(2**53 - 1, "EUR"), "USD", Decimal("1"), Money(2**53 - 1, "USD")),
    ],
)
def test_convert_rounds_half_even_to_the_target_minor_unit(
    amount: Money, to_currency: str, rate: Decimal, expected: Money
) -> None:
    assert convert(amount, to_currency, rate) == expected
