from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.checkout import clock
from app.checkout.bugs import bug_enabled
from app.checkout.db import Payment, Refund, get_db
from app.checkout.errors import ApiError
from app.checkout.money import fmt, from_cents, q
from app.checkout.orders_service import load_order
from app.checkout.schemas import RefundCreate, RefundOut
from app.checkout.security import CurrentUser

router = APIRouter(tags=["refunds"])

Db = Annotated[Session, Depends(get_db)]


def _paid_and_refunded(db: Session, order_id: str) -> tuple[Decimal, Decimal]:
    paid = db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.order_id == order_id, Payment.status == "succeeded"
        )
    )
    refunded = db.scalar(
        select(func.coalesce(func.sum(Refund.amount), 0)).where(Refund.order_id == order_id)
    )
    return from_cents(int(paid or 0)), from_cents(int(refunded or 0))


def _refund_out(r: Refund, order_status: str | None = None) -> RefundOut:
    return RefundOut(
        refund_id=r.id,
        order_id=r.order_id,
        amount=fmt(r.amount),
        created_by=r.created_by,
        created_at=r.created_at,
        order_status=order_status,
    )


@router.post("/orders/{order_id}/refunds", status_code=201, response_model=RefundOut)
async def create_refund(order_id: str, body: RefundCreate, user: CurrentUser, db: Db) -> RefundOut:
    if not user.is_admin and not bug_enabled("B10"):
        raise ApiError(403, "forbidden", "Admin role required")
    order = load_order(db, order_id, user, with_items=False)
    if order.status not in ("paid", "partially_refunded"):
        raise ApiError(
            409, "order_not_refundable", f"Order cannot be refunded while {order.status}"
        )

    amount = q(body.amount)
    paid, already = _paid_and_refunded(db, order.id)
    requested = amount if bug_enabled("B11") else already + amount
    if requested > paid:
        raise ApiError(
            422,
            "refund_exceeds_payment",
            f"Refunds would total {fmt(already + amount)}, more than the {fmt(paid)} paid",
        )

    refund = Refund(
        id=f"ref_{uuid.uuid4().hex[:16]}",
        order_id=order.id,
        amount=amount,
        created_by=user.username,
        created_at=clock.now(),
    )
    db.add(refund)
    order.status = "refunded" if already + amount >= paid else "partially_refunded"
    db.commit()
    return _refund_out(refund, order.status)


@router.get(
    "/orders/{order_id}/refunds",
    response_model=list[RefundOut],
    response_model_exclude_none=True,
)
async def list_refunds(order_id: str, user: CurrentUser, db: Db) -> list[RefundOut]:
    order = load_order(db, order_id, user, with_items=False)
    rows = db.scalars(
        select(Refund).where(Refund.order_id == order.id).order_by(Refund.created_at, Refund.id)
    ).all()
    return [_refund_out(r) for r in rows]
