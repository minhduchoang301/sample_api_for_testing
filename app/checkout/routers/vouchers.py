from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.checkout import clock
from app.checkout.bugs import bug_enabled
from app.checkout.db import Voucher, get_db
from app.checkout.errors import ApiError
from app.checkout.money import fmt, q
from app.checkout.observability import VOUCHER_REDEMPTIONS
from app.checkout.orders_service import discount_for, load_order, order_to_dict, total_after
from app.checkout.payments_state import payment_in_flight
from app.checkout.schemas import OrderOut, VoucherApply, VoucherCreate, VoucherOut
from app.checkout.security import AdminUser, CurrentUser

router = APIRouter(tags=["vouchers"])

Db = Annotated[Session, Depends(get_db)]


def _voucher_out(v: Voucher) -> VoucherOut:
    return VoucherOut(
        code=v.code,
        kind=v.kind,
        value=fmt(v.value),
        valid_from=v.valid_from,
        valid_until=v.valid_until,
        max_uses=v.max_uses,
        uses=v.uses,
    )


@router.post("/vouchers", status_code=201, response_model=VoucherOut)
async def create_voucher(body: VoucherCreate, _: AdminUser, db: Db) -> VoucherOut:
    code = body.code.upper()
    valid_from = clock.parse_instant(body.valid_from.isoformat())
    valid_until = clock.parse_instant(body.valid_until.isoformat())
    if valid_until <= valid_from:
        raise ApiError(422, "validation_error", "valid_until must be after valid_from")
    if body.kind == "percent" and body.value > 100:
        raise ApiError(422, "validation_error", "Percent value must be at most 100")
    if db.scalars(select(Voucher).where(Voucher.code == code)).one_or_none() is not None:
        raise ApiError(409, "voucher_code_taken", f"Voucher {code} already exists")
    voucher = Voucher(
        code=code,
        kind=body.kind,
        value=q(body.value),
        valid_from=valid_from,
        valid_until=valid_until,
        max_uses=body.max_uses,
        uses=0,
        created_at=clock.now(),
    )
    db.add(voucher)
    db.commit()
    return _voucher_out(voucher)


@router.get("/vouchers", response_model=list[VoucherOut])
async def list_vouchers(_: AdminUser, db: Db) -> list[VoucherOut]:
    return [_voucher_out(v) for v in db.scalars(select(Voucher).order_by(Voucher.code)).all()]


@router.post("/orders/{order_id}/voucher", response_model=OrderOut)
async def apply_voucher(
    order_id: str, body: VoucherApply, user: CurrentUser, db: Db
) -> JSONResponse:
    order = load_order(db, order_id, user)
    if order.status != "pending" or payment_in_flight(order.id):
        raise ApiError(409, "order_not_pending", "Vouchers can only be applied to pending orders")
    if order.voucher_code is not None:
        raise ApiError(409, "voucher_already_applied", "This order already has a voucher")

    code = body.code.strip().upper()
    voucher = db.scalars(select(Voucher).where(Voucher.code == code)).one_or_none()
    if voucher is None:
        raise ApiError(422, "voucher_not_found", f"Voucher {code} does not exist")

    now = clock.now()
    valid_until = voucher.valid_until
    if bug_enabled("B04"):
        valid_until += timedelta(seconds=1)
    if now < voucher.valid_from:
        raise ApiError(422, "voucher_not_yet_valid", f"Voucher {code} is not valid yet")
    if now >= valid_until:
        raise ApiError(422, "voucher_expired", f"Voucher {code} has expired")

    exhausted = ApiError(422, "voucher_exhausted", f"Voucher {code} has no uses left")
    if bug_enabled("B01"):
        if voucher.max_uses is not None and voucher.uses >= voucher.max_uses:
            raise exhausted
        await asyncio.sleep(0.05)
        voucher.uses = voucher.uses + 1
    else:
        result = db.execute(
            update(Voucher)
            .where(
                Voucher.id == voucher.id,
                or_(Voucher.max_uses.is_(None), Voucher.uses < Voucher.max_uses),
            )
            .values(uses=Voucher.uses + 1)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            db.rollback()
            raise exhausted

    discount = discount_for(voucher, order.subtotal)
    order.voucher_code = voucher.code
    order.discount = discount
    order.total = total_after(order.subtotal, discount)
    db.commit()
    VOUCHER_REDEMPTIONS.inc()
    return JSONResponse(order_to_dict(order))
