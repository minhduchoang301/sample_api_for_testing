"""Money helpers: CAD amounts as Decimals with two places."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

CURRENCY = "CAD"
CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def q(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def percent_of(amount: Decimal, percent: Decimal, *, round_down: bool = False) -> Decimal:
    raw = amount * percent / Decimal(100)
    return raw.quantize(CENT, rounding=ROUND_DOWN if round_down else ROUND_HALF_UP)


def fmt(value: Decimal) -> str:
    return str(q(value))


def to_cents(value: Decimal) -> int:
    return int(q(value) * 100)


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)
