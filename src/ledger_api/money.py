from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final

# ISO 4217 code -> minor-unit exponent: EUR 12.34 is 1234 minor units, JPY has none, KWD has 3.
CURRENCY_EXPONENTS: Final[dict[str, int]] = {
    "EUR": 2,
    "USD": 2,
    "GBP": 2,
    "JPY": 0,
    "KWD": 3,
}


class UnknownCurrency(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        # bool is a subclass of int and float would lose precision, so require exactly int.
        if type(self.amount_minor) is not int:
            raise TypeError(f"amount_minor must be int, got {type(self.amount_minor).__name__}")
        if self.currency not in CURRENCY_EXPONENTS:
            raise UnknownCurrency(self.currency)


def convert(amount: Money, to_currency: str, rate: Decimal) -> Money:
    """Converts at `rate` (to_currency per unit of amount.currency), rounding half-even."""
    if to_currency not in CURRENCY_EXPONENTS:
        raise UnknownCurrency(to_currency)
    shift = CURRENCY_EXPONENTS[to_currency] - CURRENCY_EXPONENTS[amount.currency]
    # 60 digits keep the product exact; ties then go to the even neighbour, avoiding bias.
    with localcontext(prec=60):
        exact = Decimal(amount.amount_minor) * rate * Decimal(10) ** shift
        return Money(int(exact.to_integral_value(rounding=ROUND_HALF_EVEN)), to_currency)
