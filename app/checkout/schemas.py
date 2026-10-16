"""Pydantic request and response models (responses document the API in /docs)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

from app.checkout import clock

PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
MoneyStr = Annotated[str, Field(examples=["16.99"])]
Instant = Annotated[datetime, PlainSerializer(clock.isoformat, return_type=str)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TokenRequest(Strict):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    role: Literal["customer", "admin"]


class ProductOut(BaseModel):
    sku: str
    name: str
    unit_price: MoneyStr


class ProductPage(BaseModel):
    items: list[ProductOut]
    page: int
    limit: int
    total_items: int


class OrderItemIn(Strict):
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(ge=1, le=999)


class OrderCreate(Strict):
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)


class OrderItemOut(BaseModel):
    sku: str
    name: str
    qty: int
    unit_price: MoneyStr
    line_total: MoneyStr


OrderStatus = Literal[
    "pending", "processing", "paid", "payment_failed", "partially_refunded", "refunded"
]


class OrderOut(BaseModel):
    order_id: str
    status: OrderStatus
    items: list[OrderItemOut]
    subtotal: MoneyStr
    discount: MoneyStr
    total: MoneyStr
    voucher_code: str | None
    created_at: Instant
    customer: str


class OrderPage(BaseModel):
    items: list[OrderOut]
    page: int
    limit: int
    total_items: int


class VoucherApply(Strict):
    code: str = Field(min_length=1, max_length=64)


class VoucherCreate(Strict):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["percent", "fixed"]
    value: PositiveMoney
    valid_from: datetime
    valid_until: datetime
    max_uses: int | None = Field(default=None, ge=1)


class VoucherOut(BaseModel):
    code: str
    kind: Literal["percent", "fixed"]
    value: MoneyStr
    valid_from: Instant
    valid_until: Instant
    max_uses: int | None
    uses: int


class PayResponse(BaseModel):
    order_id: str
    status: Literal["processing", "payment_failed"]
    reason: Literal["declined", "provider_error", "provider_timeout"] | None = None
    retryable: bool | None = None


class WebhookIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=64)
    type: Literal["payment.succeeded", "payment.failed"]
    charge_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=40)
    amount: Decimal = Field(ge=0, decimal_places=2)


class WebhookAck(BaseModel):
    received: bool = True
    duplicate: bool = False


class PaymentOut(BaseModel):
    payment_id: str
    order_id: str
    charge_id: str
    amount: MoneyStr
    currency: str
    status: str
    created_at: Instant


class RefundCreate(Strict):
    amount: PositiveMoney


class RefundOut(BaseModel):
    refund_id: str
    order_id: str
    amount: MoneyStr
    created_by: str
    created_at: Instant
    order_status: OrderStatus | None = None


class DeviceEventIn(Strict):
    seq: int = Field(ge=0, le=2**62)
    ts: str = Field(max_length=64)
    type: Literal["plate_read"]
    plate: str = Field(min_length=1, max_length=32)
    camera: str = Field(min_length=1, max_length=64)

    @field_validator("ts")
    @classmethod
    def _iso(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value


class DeviceBatch(Strict):
    events: list[DeviceEventIn] = Field(min_length=1, max_length=500)


class DeviceBatchResult(BaseModel):
    accepted: int
    duplicates: int


class DeviceStateOut(BaseModel):
    device_id: str
    last_seq: int
    last_ts: str
    last_plate: str
    event_count: int


class ClockSet(Strict):
    now: datetime


class ClockOut(BaseModel):
    now: str
    fixed: bool


class Health(BaseModel):
    status: Literal["ok"] = "ok"
