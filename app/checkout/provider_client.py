"""Outbound calls from checkout to the payment provider."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import httpx

from app.checkout.config import settings
from app.checkout.db import Order
from app.checkout.money import CURRENCY, fmt

logger = logging.getLogger("paylab.provider_client")

MAX_PROVIDER_WAIT_S = 4.5

Outcome = Literal["accepted", "declined", "provider_error", "provider_timeout"]


@dataclass(frozen=True)
class ChargeResult:
    outcome: Outcome
    charge_id: str | None = None


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=settings.provider_timeout_s)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def create_charge(order: Order, request_id: str | None) -> ChargeResult:
    payload = {
        "order_id": order.id,
        "amount": fmt(order.total),
        "currency": CURRENCY,
        "callback_url": f"{settings.callback_base_url}/webhooks/provider",
    }
    headers = {"X-Request-ID": request_id} if request_id else {}
    wait = min(settings.provider_timeout_s, MAX_PROVIDER_WAIT_S)
    try:
        response = await asyncio.wait_for(
            client().post(f"{settings.provider_url}/charges", json=payload, headers=headers),
            timeout=wait,
        )
    except (TimeoutError, httpx.TimeoutException):
        logger.warning("provider call timed out", extra={"order_id": order.id})
        return ChargeResult("provider_timeout")
    except httpx.HTTPError as exc:
        logger.warning(
            "provider call failed", extra={"order_id": order.id, "error": type(exc).__name__}
        )
        return ChargeResult("provider_error")

    if response.status_code == 202:
        try:
            charge_id = str(response.json()["charge_id"])
        except (ValueError, KeyError, TypeError):
            return ChargeResult("provider_error")
        return ChargeResult("accepted", charge_id)
    if response.status_code == 402:
        return ChargeResult("declined")
    logger.warning(
        "provider returned an error",
        extra={"order_id": order.id, "provider_status": response.status_code},
    )
    return ChargeResult("provider_error")
