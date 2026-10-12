from dataclasses import dataclass
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
