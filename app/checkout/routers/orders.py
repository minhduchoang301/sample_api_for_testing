from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.checkout import clock
from app.checkout.bugs import bug_enabled
from app.checkout.db import Order, OrderItem, Product, get_db
from app.checkout.errors import ApiError
from app.checkout.money import ZERO, q
from app.checkout.observability import ORDERS_CREATED
from app.checkout.orders_service import load_order, order_to_dict
from app.checkout.schemas import OrderCreate, OrderOut, OrderPage
from app.checkout.security import CurrentUser

router = APIRouter(tags=["orders"])

Db = Annotated[Session, Depends(get_db)]


@router.post("/orders", status_code=201, response_model=OrderOut)
async def create_order(body: OrderCreate, user: CurrentUser, db: Db) -> JSONResponse:
    quantities: dict[str, int] = {}
    for line in body.items:
        quantities[line.sku] = quantities.get(line.sku, 0) + line.qty
    if any(qty > 999 for qty in quantities.values()):
        raise ApiError(422, "validation_error", "Quantity per SKU must not exceed 999")

    products = {
        p.sku: p for p in db.scalars(select(Product).where(Product.sku.in_(quantities))).all()
    }
    unknown = [sku for sku in quantities if sku not in products]
    if unknown:
        raise ApiError(422, "unknown_sku", f"Unknown SKU: {', '.join(unknown)}")

    order = Order(
        id=f"ord_{uuid.uuid4().hex[:16]}",
        customer=user.username,
        status="pending",
        subtotal=ZERO,
        discount=ZERO,
        total=ZERO,
        voucher_code=None,
        created_at=clock.now(),
    )
    subtotal = Decimal(0)
    items: list[OrderItem] = []
    for sku, qty in quantities.items():
        product = products[sku]
        line_total = q(product.unit_price * qty)
        subtotal += line_total
        items.append(
            OrderItem(
                order_id=order.id,
                sku=sku,
                name=product.name,
                qty=qty,
                unit_price=product.unit_price,
                line_total=line_total,
            )
        )
    order.subtotal = q(subtotal)
    order.total = q(subtotal)
    db.add(order)
    db.add_all(items)
    db.commit()
    ORDERS_CREATED.inc()
    return JSONResponse(order_to_dict(order, items), status_code=201)


@router.get("/orders", response_model=OrderPage)
async def list_orders(
    user: CurrentUser,
    db: Db,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> JSONResponse:
    base = select(Order)
    count = select(func.count()).select_from(Order)
    if not user.is_admin:
        base = base.where(Order.customer == user.username)
        count = count.where(Order.customer == user.username)
    total = db.scalar(count) or 0
    window = base.order_by(Order.created_at, Order.id).offset((page - 1) * limit).limit(limit)

    if bug_enabled("B09"):
        orders = db.scalars(window).all()
        payload = []
        for order in orders:
            items = list(
                db.scalars(
                    select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
                ).all()
            )
            time.sleep(0.005)  # noqa: ASYNC251
            payload.append(order_to_dict(order, items))
    else:
        orders = db.scalars(window.options(selectinload(Order.items))).all()
        payload = [order_to_dict(order) for order in orders]

    return JSONResponse({"items": payload, "page": page, "limit": limit, "total_items": total})


@router.get("/orders/{order_id}", response_model=OrderOut)
async def get_order(order_id: str, user: CurrentUser, db: Db) -> JSONResponse:
    return JSONResponse(order_to_dict(load_order(db, order_id, user)))
