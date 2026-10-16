from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.checkout import clock, provider_client
from app.checkout.bugs import bug_enabled
from app.checkout.config import settings
from app.checkout.db import IdempotencyRecord, Order, Payment, WebhookEvent, get_db
from app.checkout.errors import ApiError, error_body, not_found
from app.checkout.money import CURRENCY, fmt, q
from app.checkout.observability import PAYMENTS, WEBHOOK_DUPLICATES
from app.checkout.orders_service import load_order
from app.checkout.payments_state import clear_in_flight, mark_in_flight, payment_in_flight
from app.checkout.schemas import PaymentOut, PayResponse, WebhookAck, WebhookIn
from app.checkout.security import CurrentUser

router = APIRouter(tags=["payments"])

Db = Annotated[Session, Depends(get_db)]

_PAYABLE_FROM = ("pending", "payment_failed")


def _mark_failed(db: Session, order_id: str, reason: str, *, retryable: bool) -> None:
    db.execute(
        update(Order)
        .where(Order.id == order_id, Order.status.in_((*_PAYABLE_FROM, "processing")))
        .values(status="payment_failed", failure_reason=reason, failure_retryable=retryable)
    )


@router.post(
    "/orders/{order_id}/pay",
    status_code=202,
    response_model=PayResponse,
    responses={200: {"model": PayResponse, "description": "Payment failed"}},
)
async def pay_order(
    order_id: str,
    request: Request,
    user: CurrentUser,
    db: Db,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    key = (idempotency_key or "").strip()
    if not key or len(key) > 255:
        raise ApiError(400, "idempotency_key_required", "Idempotency-Key header is required")

    order = load_order(db, order_id, user, with_items=False)
    record = db.scalars(
        select(IdempotencyRecord).where(
            IdempotencyRecord.username == user.username, IdempotencyRecord.key == key
        )
    ).one_or_none()
    if record is not None:
        if record.order_id != order.id:
            raise ApiError(
                409, "idempotency_key_reused", "Idempotency-Key was used for another order"
            )
        if record.status_code is None or record.body is None:
            raise ApiError(409, "request_in_progress", "A request with this key is in progress")
        return JSONResponse(record.body, status_code=record.status_code)

    if payment_in_flight(order.id):
        raise ApiError(409, "payment_in_progress", "A payment for this order is in progress")
    payable = order.status == "pending" or (
        order.status == "payment_failed" and order.failure_retryable
    )
    if not payable:
        raise ApiError(409, "order_not_payable", f"Order cannot be paid while {order.status}")

    record = IdempotencyRecord(
        username=user.username, key=key, order_id=order.id, created_at=clock.now()
    )
    db.add(record)
    db.commit()

    mark_in_flight(order.id)
    try:
        result = await provider_client.create_charge(order, request.state.request_id)
    finally:
        clear_in_flight(order.id)

    body: dict[str, Any]
    if result.outcome == "accepted":
        db.execute(
            update(Order)
            .where(Order.id == order.id, Order.status.in_(_PAYABLE_FROM))
            .values(
                status="processing",
                last_charge_id=result.charge_id,
                failure_reason=None,
                failure_retryable=False,
            )
        )
        status_code, body = 202, {"order_id": order.id, "status": "processing"}
        PAYMENTS.labels(status="processing").inc()
    else:
        retryable = result.outcome != "declined"
        if not (result.outcome == "provider_timeout" and bug_enabled("B05")):
            _mark_failed(db, order.id, result.outcome, retryable=retryable)
        reason = "declined" if result.outcome == "declined" else result.outcome
        status_code = 200
        body = {
            "order_id": order.id,
            "status": "payment_failed",
            "reason": reason,
            "retryable": retryable,
        }
        PAYMENTS.labels(status="payment_failed").inc()

    record.status_code = status_code
    record.body = body
    db.commit()
    return JSONResponse(body, status_code=status_code)


@router.post(
    "/webhooks/provider",
    response_model=WebhookAck,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": WebhookIn.model_json_schema()}},
        },
        "parameters": [
            {
                "name": "X-Signature",
                "in": "header",
                "required": True,
                "description": "hex HMAC-SHA256 of the raw body keyed with WEBHOOK_SECRET",
                "schema": {"type": "string"},
            }
        ],
    },
)
async def provider_webhook(request: Request, db: Db) -> JSONResponse:
    raw = await request.body()
    signature = request.headers.get("x-signature", "").strip().lower()
    expected = hmac.new(settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise ApiError(401, "invalid_signature", "Webhook signature verification failed")
    try:
        event = WebhookIn.model_validate_json(raw)
    except ValidationError as exc:
        details = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse(
            error_body("validation_error", "Invalid webhook payload", details=details),
            status_code=422,
        )

    if not bug_enabled("B02") and db.get(WebhookEvent, event.event_id) is not None:
        WEBHOOK_DUPLICATES.inc()
        return JSONResponse({"received": True, "duplicate": True})

    order = db.get(Order, event.order_id)
    if order is None:
        raise not_found("Order")

    db.merge(
        WebhookEvent(
            event_id=event.event_id,
            type=event.type,
            order_id=order.id,
            received_at=clock.now(),
        )
    )
    if event.type == "payment.succeeded":
        db.add(
            Payment(
                id=f"pay_{uuid.uuid4().hex[:16]}",
                order_id=order.id,
                charge_id=event.charge_id,
                event_id=event.event_id,
                amount=q(event.amount),
                currency=CURRENCY,
                status="succeeded",
                created_at=clock.now(),
            )
        )
        if order.status in ("pending", "processing", "payment_failed"):
            order.status = "paid"
            order.failure_reason = None
            order.failure_retryable = False
            order.last_charge_id = event.charge_id
        PAYMENTS.labels(status="paid").inc()
    else:
        if order.status in ("pending", "processing"):
            order.status = "payment_failed"
            order.failure_reason = "declined"
            order.failure_retryable = False
        PAYMENTS.labels(status="payment_failed").inc()
    db.commit()
    return JSONResponse({"received": True, "duplicate": False})


@router.get("/orders/{order_id}/payments", response_model=list[PaymentOut])
async def list_payments(order_id: str, user: CurrentUser, db: Db) -> list[PaymentOut]:
    order = load_order(db, order_id, user, with_items=False)
    rows = db.scalars(
        select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at, Payment.id)
    ).all()
    return [
        PaymentOut(
            payment_id=p.id,
            order_id=p.order_id,
            charge_id=p.charge_id,
            amount=fmt(p.amount),
            currency=p.currency,
            status=p.status,
            created_at=p.created_at,
        )
        for p in rows
    ]
