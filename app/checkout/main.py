"""Checkout service application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from app.checkout import errors, provider_client
from app.checkout.config import settings
from app.checkout.observability import RequestContextMiddleware
from app.checkout.routers import (
    auth,
    devices,
    orders,
    payments,
    products,
    refunds,
    testhooks,
    ui,
    vouchers,
)
from app.checkout.schemas import Health
from app.checkout.seed import init_db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db(wipe=False)
    yield
    await provider_client.close()


def create_app() -> FastAPI:
    application = FastAPI(
        title="PayLab Checkout",
        version="1.0.0",
        description=(
            "Checkout API for the PayLab store. Amounts are CAD strings with two decimals. "
            "Authenticate with POST /auth/token and send `Authorization: Bearer <token>`."
        ),
        lifespan=lifespan,
    )
    errors.install(application)

    @application.get("/health", response_model=Health, tags=["ops"])
    async def health() -> Health:
        return Health()

    @application.get("/metrics", tags=["ops"], response_class=Response)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    for module in (auth, products, orders, vouchers, payments, refunds, devices):
        application.include_router(module.router)
    if settings.test_mode:
        application.include_router(testhooks.router)
    application.include_router(ui.router)
    application.mount("/static", StaticFiles(directory=ui.WEB_DIR / "static"), name="static")
    application.add_middleware(RequestContextMiddleware, root_app=application)
    return application


app = create_app()
