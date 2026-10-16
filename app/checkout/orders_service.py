"""Order lookup, serialization and pricing helpers shared by the routers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.checkout import clock
from app.checkout.bugs import bug_enabled
from app.checkout.db import Order, OrderItem, Voucher
from app.checkout.errors import not_found
from app.checkout.money import ZERO, fmt, percent_of, q
from app.checkout.security import Principal

FINAL_STATUSES = frozenset({"paid", "payment_failed", "partially_refunded", "refunded"})


def load_order(db: Session, order_id: str, user: Principal, *, with_items: bool = True) -> Order:
    stmt = select(Order).where(Order.id == order_id)
    if with_items:
        stmt = stmt.options(selectinload(Order.items))
    order = db.scalars(stmt).one_or_none()
    if order is None or (not user.is_admin and order.customer != user.username):
        raise not_found("Order")
    return order


def item_to_dict(item: OrderItem) -> dict[str, Any]:
    return {
        "sku": item.sku,
        "name": item.name,
        "qty": item.qty,
        "unit_price": fmt(item.unit_price),
        "line_total": fmt(item.line_total),
    }


def order_to_dict(order: Order, items: list[OrderItem] | None = None) -> dict[str, Any]:
    total_key = "total_amount" if bug_enabled("B08") else "total"
    return {
        "order_id": order.id,
        "status": order.status,
        "items": [item_to_dict(i) for i in (order.items if items is None else items)],
        "subtotal": fmt(order.subtotal),
        "discount": fmt(order.discount),
        total_key: fmt(order.total),
        "voucher_code": order.voucher_code,
        "created_at": clock.isoformat(order.created_at),
        "customer": order.customer,
    }


def discount_for(voucher: Voucher, subtotal: Decimal) -> Decimal:
    if voucher.kind == "percent":
        return percent_of(subtotal, voucher.value, round_down=bug_enabled("B03"))
    return q(voucher.value)


def total_after(subtotal: Decimal, discount: Decimal) -> Decimal:
    return max(ZERO, q(subtotal - discount))
