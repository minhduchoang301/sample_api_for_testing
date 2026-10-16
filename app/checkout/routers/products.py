from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.checkout.db import Product, get_db
from app.checkout.money import fmt
from app.checkout.schemas import ProductOut, ProductPage

router = APIRouter(tags=["products"])


@router.get("/products", response_model=ProductPage)
async def list_products(
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> ProductPage:
    total = db.scalar(select(func.count()).select_from(Product)) or 0
    rows = db.scalars(
        select(Product).order_by(Product.sku).offset((page - 1) * limit).limit(limit)
    ).all()
    return ProductPage(
        items=[ProductOut(sku=p.sku, name=p.name, unit_price=fmt(p.unit_price)) for p in rows],
        page=page,
        limit=limit,
        total_items=total,
    )
