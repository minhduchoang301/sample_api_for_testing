"""JSON logging, Prometheus metrics and the request-tracking ASGI middleware."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from prometheus_client import Counter, Histogram, disable_created_metrics
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.common.jsonlog import logging_config as logging_config

disable_created_metrics()  # type: ignore[no-untyped-call]

ORDERS_CREATED = Counter("orders_created_total", "Orders created")
PAYMENTS = Counter("payments_total", "Payment outcomes by order status", ["status"])
VOUCHER_REDEMPTIONS = Counter("voucher_redemptions_total", "Vouchers applied to orders")
WEBHOOK_DUPLICATES = Counter(
    "webhook_duplicates_total", "Provider webhooks ignored because the event was already processed"
)
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

access_logger = logging.getLogger("paylab.access")


def _route_template(app: Any, scope: Scope) -> str | None:
    route = scope.get("route")
    if route is not None and hasattr(route, "path"):
        return str(route.path)
    for candidate in getattr(getattr(app, "router", None), "routes", []):
        match, _ = candidate.matches(scope)
        if match == Match.FULL:
            return str(getattr(candidate, "path", None) or "")
    return None


class RequestContextMiddleware:
    """Assigns X-Request-ID, records duration metrics and writes one access log line."""

    def __init__(self, app: ASGIApp, root_app: Any = None) -> None:
        self.app = app
        self.root_app = root_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                incoming = value.decode("latin-1").strip()
                break
        request_id = incoming[:128] if incoming else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - started
            route = _route_template(self.root_app, scope) or "unmatched"
            method = scope.get("method", "")
            REQUEST_DURATION.labels(method=method, route=route).observe(duration)
            line: dict[str, Any] = {
                "request_id": request_id,
                "method": method,
                "path": scope.get("path", ""),
                "status": status_code,
                "duration_ms": round(duration * 1000, 2),
            }
            order_id = (scope.get("path_params") or {}).get("order_id")
            if order_id:
                line["order_id"] = order_id
            access_logger.info("request", extra=line)
