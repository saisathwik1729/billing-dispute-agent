"""Decimal helpers. Every monetary value in the system flows through these.

Floats are never used for money: JSON is parsed with parse_float=Decimal and all
values are persisted as strings.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0")


class MoneyError(ValueError):
    pass


def D(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise MoneyError(f"not a number: {value!r}")
    if isinstance(value, float):
        value = repr(value)
    try:
        result = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"not a number: {value!r}") from exc
    if not result.is_finite():
        raise MoneyError(f"not a finite number: {value!r}")
    return result


def money(value) -> Decimal:
    """Round to cents, half-up (commercial rounding)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def m2s(value) -> str:
    """Money to canonical string, e.g. '1234.50'."""
    return str(money(value))


def n2s(value) -> str:
    """Non-money decimal (quantity, rate) to a compact canonical string."""
    d = D(value)
    if d == d.to_integral_value():
        return str(d.quantize(Decimal(1)))
    return format(d.normalize(), "f")
