"""Mock payment provider with switchable fault modes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("paylab.provider")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "whsec_paylab_dev")
DEFAULT_DELAY_MS = 2000
OK_WEBHOOK_DELAY_S = 0.1
DUPLICATE_GAP_S = 0.2
TIMEOUT_SLEEP_S = 10.0

Mode = Literal["ok", "decline", "timeout", "error_500", "duplicate_webhook", "delayed_webhook"]


class ChargeIn(BaseModel):
    order_id: str
    amount: Decimal = Field(ge=0, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    callback_url: str


class FaultsIn(BaseModel):
    mode: Mode
    delay_ms: int = Field(default=DEFAULT_DELAY_MS, ge=0, le=60_000)


class FaultsOut(BaseModel):
    mode: Mode
    delay_ms: int


class ChargeOut(BaseModel):
    charge_id: str
    order_id: str
    amount: str
    currency: str
    status: str
    mode: Mode
    webhooks_sent: int
    created_at: str


class _State:
    def __init__(self) -> None:
        self.mode: Mode = "ok"
        self.delay_ms = DEFAULT_DELAY_MS
        self.charges: dict[str, ChargeOut] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def reset(self) -> None:
        self.mode = "ok"
        self.delay_ms = DEFAULT_DELAY_MS
        self.charges.clear()


state = _State()
app = FastAPI(
    title="PayLab Mock Payment Provider",
    version="1.0.0",
    description="Accepts charges and calls back with signed webhooks. Behaviour is set via "
    "POST /admin/faults.",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def _deliver(callback_url: str, body: bytes, charge: ChargeOut) -> None:
    headers = {"Content-Type": "application/json", "X-Signature": sign(body)}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(callback_url, content=body, headers=headers)
        charge.webhooks_sent += 1
        logger.info(
            "webhook delivered",
            extra={"charge_id": charge.charge_id, "callback_status": response.status_code},
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "webhook delivery failed",
            extra={"charge_id": charge.charge_id, "error": type(exc).__name__},
        )


async def _send_webhooks(charge: ChargeOut, callback_url: str, delay_s: float, copies: int) -> None:
    event = {
        "event_id": f"evt_{uuid.uuid4().hex[:20]}",
        "type": "payment.succeeded",
        "charge_id": charge.charge_id,
        "order_id": charge.order_id,
        "amount": charge.amount,
    }
    body = json.dumps(event, separators=(",", ":")).encode()
    await asyncio.sleep(delay_s)
    for copy in range(copies):
        if copy:
            await asyncio.sleep(DUPLICATE_GAP_S)
        await _deliver(callback_url, body, charge)
    charge.status = "succeeded"


def _schedule(coro: Any) -> None:
    task: asyncio.Task[None] = asyncio.create_task(coro)
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/charges", status_code=202, tags=["charges"])
async def create_charge(body: ChargeIn) -> JSONResponse:
    mode = state.mode
    logger.info("charge requested", extra={"order_id": body.order_id, "mode": mode})
    if mode == "timeout":
        await asyncio.sleep(TIMEOUT_SLEEP_S)
        return JSONResponse({"error": "gateway_timeout"}, status_code=504)
    if mode == "error_500":
        return JSONResponse({"error": "internal_error"}, status_code=500)
    if mode == "decline":
        return JSONResponse({"error": "card_declined"}, status_code=402)

    charge = ChargeOut(
        charge_id=f"ch_{uuid.uuid4().hex[:20]}",
        order_id=body.order_id,
        amount=str(body.amount.quantize(Decimal("0.01"))),
        currency=body.currency.upper(),
        status="pending",
        mode=mode,
        webhooks_sent=0,
        created_at=_now(),
    )
    state.charges[charge.charge_id] = charge
    delay = state.delay_ms / 1000 if mode == "delayed_webhook" else OK_WEBHOOK_DELAY_S
    copies = 2 if mode == "duplicate_webhook" else 1
    _schedule(_send_webhooks(charge, body.callback_url, delay, copies))
    return JSONResponse({"charge_id": charge.charge_id}, status_code=202)


@app.get("/charges/{charge_id}", response_model=ChargeOut, tags=["charges"])
async def get_charge(charge_id: str) -> ChargeOut:
    charge = state.charges.get(charge_id)
    if charge is None:
        raise HTTPException(status_code=404, detail="charge not found")
    return charge


@app.get("/admin/faults", response_model=FaultsOut, tags=["admin"])
async def get_faults() -> FaultsOut:
    return FaultsOut(mode=state.mode, delay_ms=state.delay_ms)


@app.post("/admin/faults", response_model=FaultsOut, tags=["admin"])
async def set_faults(body: FaultsIn) -> FaultsOut:
    state.mode = body.mode
    state.delay_ms = body.delay_ms
    logger.info("fault mode changed", extra={"mode": body.mode, "delay_ms": body.delay_ms})
    return FaultsOut(mode=state.mode, delay_ms=state.delay_ms)


@app.post("/admin/reset", response_model=FaultsOut, tags=["admin"])
async def reset() -> FaultsOut:
    state.reset()
    return FaultsOut(mode=state.mode, delay_ms=state.delay_ms)
