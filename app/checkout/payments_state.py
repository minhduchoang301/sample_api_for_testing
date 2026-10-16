"""Process-local registry of orders with a provider call in flight."""

from __future__ import annotations

_in_flight: set[str] = set()


def payment_in_flight(order_id: str) -> bool:
    return order_id in _in_flight


def mark_in_flight(order_id: str) -> None:
    _in_flight.add(order_id)


def clear_in_flight(order_id: str | None = None) -> None:
    if order_id is None:
        _in_flight.clear()
    else:
        _in_flight.discard(order_id)
