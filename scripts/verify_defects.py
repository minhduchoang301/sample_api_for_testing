"""Private verification for the PayLab SUT.

For each run (baseline, then B01..B12, then a config run) this script restarts the
stack with BUGS overridden, resets state before every check, and proves:

* baseline: every specified behaviour holds with BUGS empty;
* Bxx: the defect's symptom appears with only that ID on, and every baseline check
  that the defect is not expected to break still passes.

Usage: uv run python scripts/verify_defects.py [--only baseline,B03] [--skip-build]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import itertools
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Locator, Page, sync_playwright
from playwright.sync_api import expect as expect_pw

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = os.environ.get("CHECKOUT_URL", "http://localhost:8000")
PROVIDER = os.environ.get("PROVIDER_URL", "http://localhost:8001")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "whsec_paylab_dev")
BUG_IDS = [f"B{n:02d}" for n in range(1, 13)]
ID_PATTERN = re.compile(r"\bB(0[1-9]|1[0-2])\b")
PASSWORDS = {"alice": "alice123", "bob": "bob123", "admin": "admin123"}

PERF_CLIENTS = 50
PERF_ORDERS = 200
PERF_LIMIT = 50
PERF_BASELINE_P95_S = 0.300
PERF_BUGGY_P95_S = 1.0


class CheckFailed(AssertionError):
    pass


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def expect_eq(actual: Any, expected: Any, what: str) -> None:
    if actual != expected:
        raise CheckFailed(f"{what}: expected {expected!r}, got {actual!r}")


# --------------------------------------------------------------------------- stack


def compose(*args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{result.stderr[-3000:]}")
    return result.stdout


def start_stack(bugs: str, *, test_mode: str = "1", fake_now: str = "") -> float:
    env = {"BUGS": bugs, "TEST_MODE": test_mode, "FAKE_NOW": fake_now}
    started = time.perf_counter()
    compose("up", "-d", "--wait", "--force-recreate", "checkout", env=env)
    return time.perf_counter() - started


def checkout_logs() -> list[dict[str, Any]]:
    out = compose("logs", "--no-log-prefix", "--no-color", "checkout")
    lines: list[dict[str, Any]] = []
    for raw in out.splitlines():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            lines.append(parsed)
    return lines


def checkout_raw_logs() -> str:
    return compose("logs", "--no-log-prefix", "--no-color", "checkout")


# --------------------------------------------------------------------------- API


class Api:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=CHECKOUT, timeout=30)
        self.provider = httpx.Client(base_url=PROVIDER, timeout=30)
        self._tokens: dict[str, str] = {}

    def login(self, user: str) -> str:
        r = self.http.post("/auth/token", json={"username": user, "password": PASSWORDS[user]})
        expect_eq(r.status_code, 200, f"login {user}")
        token = str(r.json()["access_token"])
        self._tokens[user] = token
        return token

    def token(self, user: str) -> str:
        return self._tokens.get(user) or self.login(user)

    def headers(self, user: str | None, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = dict(extra or {})
        if user:
            h["Authorization"] = f"Bearer {self.token(user)}"
        return h

    def req(
        self,
        method: str,
        path: str,
        user: str | None = None,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {"headers": self.headers(user, headers)}
        if json_body is not None:
            kwargs["json"] = json_body
        if timeout is not None:
            kwargs["timeout"] = timeout
        r = self.http.request(method, path, **kwargs)
        if r.status_code == 401 and user and "expired" in r.text:
            self.login(user)
            kwargs["headers"] = self.headers(user, headers)
            r = self.http.request(method, path, **kwargs)
        return r

    # convenience
    def reset(self) -> None:
        self.provider.post("/admin/reset").raise_for_status()
        r = self.req("DELETE", "/__test/clock", "admin")
        expect_eq(r.status_code, 200, "clear clock")
        r = self.req("POST", "/__test/reset", "admin")
        expect_eq(r.status_code, 200, "reset")

    def set_clock(self, instant: datetime) -> None:
        r = self.req("POST", "/__test/clock", "admin", json_body={"now": instant.isoformat()})
        expect_eq(r.status_code, 200, "set clock")

    def fault(self, mode: str, delay_ms: int = 2000) -> None:
        r = self.provider.post("/admin/faults", json={"mode": mode, "delay_ms": delay_ms})
        r.raise_for_status()

    def create_order(self, user: str, items: list[tuple[str, int]]) -> dict[str, Any]:
        r = self.req(
            "POST",
            "/orders",
            user,
            json_body={"items": [{"sku": s, "qty": q} for s, q in items]},
        )
        expect_eq(r.status_code, 201, f"create order: {r.text}")
        return dict(r.json())

    def get_order(self, user: str, order_id: str) -> dict[str, Any]:
        r = self.req("GET", f"/orders/{order_id}", user)
        expect_eq(r.status_code, 200, f"get order {order_id}")
        return dict(r.json())

    def apply_voucher(self, user: str, order_id: str, code: str) -> httpx.Response:
        return self.req("POST", f"/orders/{order_id}/voucher", user, json_body={"code": code})

    def pay(self, user: str, order_id: str, key: str | None = None) -> httpx.Response:
        headers = {"Idempotency-Key": key or uuid.uuid4().hex}
        return self.req("POST", f"/orders/{order_id}/pay", user, headers=headers, timeout=15)

    def wait_status(
        self, user: str, order_id: str, wanted: set[str], timeout: float = 5
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            order = self.get_order(user, order_id)
            if order["status"] in wanted:
                return order
            if time.monotonic() > deadline:
                raise CheckFailed(f"order {order_id} stuck in {order['status']}, wanted {wanted}")
            time.sleep(0.1)

    def paid_order(self, user: str = "alice", sku: str = "SKU-001") -> dict[str, Any]:
        order = self.create_order(user, [(sku, 1)])
        r = self.pay(user, order["order_id"])
        expect_eq(r.status_code, 202, "pay ok")
        return self.wait_status(user, order["order_id"], {"paid"})

    def metrics(self) -> dict[str, float]:
        r = self.http.get("/metrics")
        expect_eq(r.status_code, 200, "metrics")
        values: dict[str, float] = {}
        for line in r.text.splitlines():
            if line and not line.startswith("#"):
                name, _, value = line.rpartition(" ")
                values[name] = float(value)
        return values


def total_of(order: dict[str, Any]) -> str:
    """Order total, tolerant of the field name; strict shape is checked separately."""
    return str(order["total"] if "total" in order else order["total_amount"])


def sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def post_webhook(api: Api, payload: dict[str, Any], signature: str | None = None) -> httpx.Response:
    body = json.dumps(payload).encode()
    return api.http.post(
        "/webhooks/provider",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": signature or sign(body)},
    )


# --------------------------------------------------------------------------- registry


@dataclass
class Ctx:
    run: str
    bugs: frozenset[str]
    api: Api
    perf_seconds: float
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.notes.append(text)


CheckFn = Callable[[Ctx], None]


@dataclass(frozen=True)
class Check:
    name: str
    fn: CheckFn
    affected_by: frozenset[str]


BASELINE: list[Check] = []
SYMPTOMS: dict[str, Check] = {}


def baseline(name: str, affected_by: tuple[str, ...] = ()) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        BASELINE.append(Check(name, fn, frozenset(affected_by)))
        return fn

    return register


def symptom(bug_id: str, name: str) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        SYMPTOMS[bug_id] = Check(name, fn, frozenset({bug_id}))
        return fn

    return register


# --------------------------------------------------------------------------- baseline checks

EXPECTED_PATHS = {
    "/health",
    "/metrics",
    "/auth/token",
    "/products",
    "/orders",
    "/orders/{order_id}",
    "/vouchers",
    "/orders/{order_id}/voucher",
    "/orders/{order_id}/pay",
    "/webhooks/provider",
    "/orders/{order_id}/payments",
    "/orders/{order_id}/refunds",
    "/devices/{device_id}/events",
    "/devices/{device_id}/state",
    "/__test/reset",
    "/__test/clock",
}


@baseline("health and /docs list every endpoint")
def check_health_docs(ctx: Ctx) -> None:
    api = ctx.api
    expect_eq(api.http.get("/health").json(), {"status": "ok"}, "checkout health")
    expect_eq(api.provider.get("/health").json(), {"status": "ok"}, "provider health")
    expect_eq(api.http.get("/docs").status_code, 200, "/docs")
    paths = set(api.http.get("/openapi.json").json()["paths"])
    missing = EXPECTED_PATHS - paths
    expect(not missing, f"/docs is missing {sorted(missing)}")
    provider_paths = set(api.provider.get("/openapi.json").json()["paths"])
    expect(
        {"/charges", "/charges/{charge_id}", "/admin/faults", "/admin/reset"} <= provider_paths,
        "provider docs incomplete",
    )


@baseline("auth: tokens, 401, 403 and cross-customer 404")
def check_auth(ctx: Ctx) -> None:
    api = ctx.api
    for user, role in (("alice", "customer"), ("bob", "customer"), ("admin", "admin")):
        r = api.http.post("/auth/token", json={"username": user, "password": PASSWORDS[user]})
        expect_eq(r.status_code, 200, f"token {user}")
        body = r.json()
        expect_eq((body["token_type"], body["role"]), ("bearer", role), f"token body {user}")
    bad = api.http.post("/auth/token", json={"username": "alice", "password": "nope"})
    expect_eq(bad.status_code, 401, "wrong password")
    expect_eq(api.http.get("/orders").status_code, 401, "missing token")
    r = api.http.get("/orders", headers={"Authorization": "Bearer not-a-jwt"})
    expect_eq(r.status_code, 401, "invalid token")
    expect_eq(api.http.get("/products").status_code, 200, "products public")
    expect_eq(api.http.get("/metrics").status_code, 200, "metrics public")
    r = api.req(
        "POST",
        "/vouchers",
        "alice",
        json_body={
            "code": "NOPE1",
            "kind": "fixed",
            "value": "1.00",
            "valid_from": "2020-01-01T00:00:00Z",
            "valid_until": "2040-01-01T00:00:00Z",
        },
    )
    expect_eq(r.status_code, 403, "customer creating voucher")
    order = api.create_order("alice", [("SKU-002", 1)])
    expect_eq(api.req("GET", f"/orders/{order['order_id']}", "bob").status_code, 404, "bob->alice")
    expect_eq(api.req("GET", f"/orders/{order['order_id']}", "admin").status_code, 200, "admin")
    expect_eq(
        api.req("GET", f"/orders/{order['order_id']}/payments", "bob").status_code,
        404,
        "bob->alice payments",
    )


@baseline("products: 30 seeded, sorted by sku, paginated")
def check_products(ctx: Ctx) -> None:
    api = ctx.api
    first = api.http.get("/products").json()
    expect_eq((first["page"], first["limit"], first["total_items"]), (1, 10, 30), "defaults")
    expect_eq(len(first["items"]), 10, "page size")
    seen: list[str] = []
    for page in range(1, 6):
        body = api.http.get("/products", params={"page": page, "limit": 7}).json()
        seen.extend(p["sku"] for p in body["items"])
    expect_eq(len(seen), 30, "products across pages")
    expect_eq(seen, sorted(set(seen)), "sorted and unique")
    item = first["items"][0]
    expect(isinstance(item["unit_price"], str), "unit_price is a string")
    expect_eq(api.http.get("/products", params={"limit": 0}).status_code, 422, "limit=0")


@baseline("orders: create, prices from catalog, 422s, exact shape", affected_by=("B08",))
def check_order_create(ctx: Ctx) -> None:
    api = ctx.api
    order = api.create_order("alice", [("SKU-001", 2), ("SKU-002", 1)])
    expect_eq(
        set(order),
        {
            "order_id",
            "status",
            "items",
            "subtotal",
            "discount",
            "total",
            "voucher_code",
            "created_at",
            "customer",
        },
        "order keys",
    )
    expect_eq(order["status"], "pending", "status")
    expect_eq(order["items"][0]["unit_price"], "19.99", "unit price from catalog")
    expect_eq(order["items"][0]["line_total"], "39.98", "line total")
    money = (order["subtotal"], order["discount"], order["total"])
    expect_eq(money, ("49.47", "0.00", "49.47"), "subtotal/discount/total")
    expect_eq((order["voucher_code"], order["customer"]), (None, "alice"), "voucher/customer")
    r = api.req("POST", "/orders", "alice", json_body={"items": [{"sku": "SKU-001", "qty": 0}]})
    expect_eq(r.status_code, 422, "qty 0")
    r = api.req("POST", "/orders", "alice", json_body={"items": [{"sku": "NOPE", "qty": 1}]})
    expect_eq(r.status_code, 422, "unknown sku")
    r = api.req(
        "POST", "/orders", "alice", json_body={"items": [{"sku": "SKU-001", "unit_price": "0.01"}]}
    )
    expect_eq(r.status_code, 422, "client cannot set price")
    fetched = api.get_order("alice", order["order_id"])
    expect_eq(fetched, order, "GET matches POST")


@baseline("orders: list scoped per customer, sorted, every order once across pages")
def check_order_list(ctx: Ctx) -> None:
    api = ctx.api
    frozen = datetime.now(UTC).replace(microsecond=0)
    api.set_clock(frozen)
    for _ in range(6):
        api.create_order("alice", [("SKU-003", 1)])
    api.req("DELETE", "/__test/clock", "admin")
    for _ in range(9):
        api.create_order("alice", [("SKU-004", 1)])
    for _ in range(4):
        api.create_order("bob", [("SKU-005", 1)])

    def walk(user: str, limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            r = api.req("GET", f"/orders?page={page}&limit={limit}", user)
            expect_eq(r.status_code, 200, "list orders")
            items = r.json()["items"]
            if not items:
                return out
            out.extend(items)
            page += 1

    alice = walk("alice", 4)
    expect_eq(len(alice), 15, "alice order count")
    expect(all(o["customer"] == "alice" for o in alice), "alice sees only her orders")
    ids = [o["order_id"] for o in alice]
    expect_eq(len(set(ids)), 15, "no duplicates across pages")
    keys = [(o["created_at"], o["order_id"]) for o in alice]
    expect_eq(keys, sorted(keys), "sorted by created_at then order_id")
    every = walk("admin", 7)
    expect_eq(len({o["order_id"] for o in every}), 19, "admin sees all orders once")
    r = api.req("GET", "/orders?limit=4", "alice").json()
    expect_eq(r["total_items"], 15, "total_items")


@baseline("vouchers: percent discount rounds half-up", affected_by=("B03",))
def check_voucher_percent(ctx: Ctx) -> None:
    api = ctx.api
    order = api.create_order("alice", [("SKU-001", 1)])
    r = api.apply_voucher("alice", order["order_id"], "SAVE15")
    expect_eq(r.status_code, 200, "apply SAVE15")
    body = r.json()
    expect_eq((body["discount"], total_of(body)), ("3.00", "16.99"), "15% of 19.99")
    expect_eq(body["voucher_code"], "SAVE15", "voucher_code")


@baseline("vouchers: fixed discount, floor at zero, error codes")
def check_voucher_fixed_and_errors(ctx: Ctx) -> None:
    api = ctx.api
    order = api.create_order("alice", [("SKU-002", 1)])
    r = api.apply_voucher("alice", order["order_id"], "FLAT5")
    expect_eq(r.status_code, 200, "FLAT5")
    expect_eq((r.json()["discount"], total_of(r.json())), ("5.00", "4.49"), "fixed")
    again = api.apply_voucher("alice", order["order_id"], "SAVE15")
    expect_eq(again.status_code, 409, "second voucher")

    r = api.req(
        "POST",
        "/vouchers",
        "admin",
        json_body={
            "code": "BIG10",
            "kind": "fixed",
            "value": "10.00",
            "valid_from": "2020-01-01T00:00:00Z",
            "valid_until": "2099-01-01T00:00:00Z",
        },
    )
    expect_eq(r.status_code, 201, "create BIG10")
    cheap = api.create_order("alice", [("SKU-020", 1)])
    r = api.apply_voucher("alice", cheap["order_id"], "BIG10")
    expect_eq((r.json()["discount"], total_of(r.json())), ("10.00", "0.00"), "floored at 0")

    fresh = api.create_order("alice", [("SKU-003", 1)])
    for code, error in (("NOSUCH", "voucher_not_found"), ("OLD20", "voucher_expired")):
        r = api.apply_voucher("alice", fresh["order_id"], code)
        expect_eq((r.status_code, r.json().get("error")), (422, error), code)

    now = datetime.now(UTC)
    r = api.req(
        "POST",
        "/vouchers",
        "admin",
        json_body={
            "code": "SOON",
            "kind": "percent",
            "value": "10",
            "valid_from": (now + timedelta(days=2)).isoformat(),
            "valid_until": (now + timedelta(days=9)).isoformat(),
        },
    )
    expect_eq(r.status_code, 201, "admin creates voucher")
    r = api.apply_voucher("alice", fresh["order_id"], "SOON")
    expect_eq((r.status_code, r.json().get("error")), (422, "voucher_not_yet_valid"), "SOON")

    r = api.apply_voucher("alice", fresh["order_id"], "ONCE10")
    expect_eq(r.status_code, 200, "ONCE10 first use")
    other = api.create_order("bob", [("SKU-003", 1)])
    r = api.apply_voucher("bob", other["order_id"], "ONCE10")
    expect_eq((r.status_code, r.json().get("error")), (422, "voucher_exhausted"), "ONCE10 reuse")

    paid = api.paid_order()
    r = api.apply_voucher("alice", paid["order_id"], "SAVE15")
    expect_eq(r.status_code, 409, "voucher on paid order")


@baseline("vouchers: validity window valid_from <= now < valid_until", affected_by=("B04",))
def check_voucher_window(ctx: Ctx) -> None:
    api = ctx.api
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=10)
    end = start + timedelta(minutes=30)
    r = api.req(
        "POST",
        "/vouchers",
        "admin",
        json_body={
            "code": "WINDOW",
            "kind": "fixed",
            "value": "1.00",
            "valid_from": start.isoformat(),
            "valid_until": end.isoformat(),
        },
    )
    expect_eq(r.status_code, 201, "create WINDOW")
    cases = [
        (start - timedelta(milliseconds=1), 422, "voucher_not_yet_valid"),
        (start, 200, None),
        (end - timedelta(milliseconds=1), 200, None),
        (end, 422, "voucher_expired"),
        (end + timedelta(milliseconds=500), 422, "voucher_expired"),
    ]
    for instant, status, error in cases:
        api.set_clock(instant)
        order = api.create_order("alice", [("SKU-002", 1)])
        r = api.apply_voucher("alice", order["order_id"], "WINDOW")
        expect_eq((r.status_code, r.json().get("error")), (status, error), f"at {instant}")


async def _concurrent_once10(api: Api) -> list[int]:
    orders = [api.create_order("alice", [("SKU-001", 1)])["order_id"] for _ in range(20)]
    headers = api.headers("alice")
    async with httpx.AsyncClient(base_url=CHECKOUT, timeout=30) as client:
        responses = await asyncio.gather(
            *[
                client.post(f"/orders/{oid}/voucher", json={"code": "ONCE10"}, headers=headers)
                for oid in orders
            ]
        )
    return [r.status_code for r in responses]


@baseline("vouchers: max_uses atomic under 20 concurrent requests", affected_by=("B01",))
def check_voucher_concurrency(ctx: Ctx) -> None:
    before = ctx.api.metrics().get("voucher_redemptions_total", 0)
    codes = asyncio.run(_concurrent_once10(ctx.api))
    expect_eq(codes.count(200), 1, "ONCE10 successes")
    expect_eq(codes.count(422), 19, "ONCE10 rejections")
    after = ctx.api.metrics().get("voucher_redemptions_total", 0)
    expect_eq(after - before, 1, "voucher_redemptions_total delta")
    uses = {v["code"]: v["uses"] for v in ctx.api.req("GET", "/vouchers", "admin").json()}
    expect_eq(uses["ONCE10"], 1, "ONCE10 uses")


@baseline("payments: happy path, idempotency, missing key")
def check_payment_ok(ctx: Ctx) -> None:
    api = ctx.api
    order = api.create_order("alice", [("SKU-003", 1)])
    oid = order["order_id"]
    r = api.req("POST", f"/orders/{oid}/pay", "alice")
    expect_eq(r.status_code, 400, "missing Idempotency-Key")
    m0 = api.metrics()
    first = api.pay("alice", oid, key="key-1")
    processing = {"order_id": oid, "status": "processing"}
    expect_eq((first.status_code, first.json()), (202, processing), "first pay")
    paid = api.wait_status("alice", oid, {"paid"})
    replay = api.pay("alice", oid, key="key-1")
    expect_eq((replay.status_code, replay.json()), (first.status_code, first.json()), "replay")
    time.sleep(0.4)
    payments = api.req("GET", f"/orders/{oid}/payments", "alice").json()
    expect_eq(len(payments), 1, "one payment")
    expect_eq(payments[0]["amount"], total_of(paid), "payment amount")
    expect_eq(payments[0]["currency"], "CAD", "currency")
    m1 = api.metrics()
    delta = m1.get('payments_total{status="processing"}', 0) - m0.get(
        'payments_total{status="processing"}', 0
    )
    expect_eq(delta, 1, "only one charge attempt")
    charge = api.provider.get(f"/charges/{payments[0]['charge_id']}")
    expect_eq(charge.status_code, 200, "provider has charge")
    expect_eq(api.pay("alice", oid).status_code, 409, "paying a paid order")


@baseline("payments: decline is final")
def check_payment_decline(ctx: Ctx) -> None:
    api = ctx.api
    api.fault("decline")
    oid = api.create_order("alice", [("SKU-003", 1)])["order_id"]
    r = api.pay("alice", oid)
    expect_eq(r.status_code, 200, "declined status code")
    expect_eq(
        r.json(),
        {"order_id": oid, "status": "payment_failed", "reason": "declined", "retryable": False},
        "declined body",
    )
    expect_eq(api.get_order("alice", oid)["status"], "payment_failed", "order status")
    api.fault("ok")
    expect_eq(api.pay("alice", oid).status_code, 409, "declined order cannot be re-paid")


@baseline("payments: provider 500 is retryable")
def check_payment_error(ctx: Ctx) -> None:
    api = ctx.api
    api.fault("error_500")
    oid = api.create_order("alice", [("SKU-003", 1)])["order_id"]
    r = api.pay("alice", oid)
    expect_eq(r.status_code, 200, "500 status code")
    expect_eq(
        r.json(),
        {
            "order_id": oid,
            "status": "payment_failed",
            "reason": "provider_error",
            "retryable": True,
        },
        "500 body",
    )
    expect_eq(api.get_order("alice", oid)["status"], "payment_failed", "order status")
    api.fault("ok")
    expect_eq(api.pay("alice", oid).status_code, 202, "retry with new key")
    api.wait_status("alice", oid, {"paid"})


@baseline("payments: provider timeout within 5 s, order payment_failed", affected_by=("B05",))
def check_payment_timeout(ctx: Ctx) -> None:
    api = ctx.api
    api.fault("timeout")
    oid = api.create_order("alice", [("SKU-003", 1)])["order_id"]
    started = time.perf_counter()
    r = api.pay("alice", oid)
    elapsed = time.perf_counter() - started
    expect(elapsed < 5, f"timeout response took {elapsed:.2f}s")
    expect_eq(
        r.json(),
        {
            "order_id": oid,
            "status": "payment_failed",
            "reason": "provider_timeout",
            "retryable": True,
        },
        "timeout body",
    )
    expect_eq(api.get_order("alice", oid)["status"], "payment_failed", "order status")
    api.fault("ok")
    expect_eq(api.pay("alice", oid).status_code, 202, "retry after timeout")
    api.wait_status("alice", oid, {"paid"})


@baseline("webhooks: duplicate delivery is deduplicated", affected_by=("B02",))
def check_webhook_duplicate(ctx: Ctx) -> None:
    api = ctx.api
    api.fault("duplicate_webhook")
    before = api.metrics().get("webhook_duplicates_total", 0)
    oid = api.create_order("alice", [("SKU-003", 1)])["order_id"]
    expect_eq(api.pay("alice", oid).status_code, 202, "pay")
    api.wait_status("alice", oid, {"paid"})
    time.sleep(0.8)
    payments = api.req("GET", f"/orders/{oid}/payments", "alice").json()
    expect_eq(len(payments), 1, "payments after duplicate webhook")
    after = api.metrics().get("webhook_duplicates_total", 0)
    expect_eq(after - before, 1, "webhook_duplicates_total delta")

    other = api.create_order("alice", [("SKU-004", 1)])["order_id"]
    payload = {
        "event_id": "evt_manual_1",
        "type": "payment.succeeded",
        "charge_id": "ch_manual",
        "order_id": other,
        "amount": "89.00",
    }
    expect_eq(post_webhook(api, payload).json().get("duplicate"), False, "first delivery")
    expect_eq(post_webhook(api, payload).json(), {"received": True, "duplicate": True}, "replay")


@baseline("webhooks: signature verified, failure event, delayed delivery")
def check_webhook_signature(ctx: Ctx) -> None:
    api = ctx.api
    oid = api.create_order("alice", [("SKU-004", 1)])["order_id"]
    payload = {
        "event_id": "evt_sig",
        "type": "payment.succeeded",
        "charge_id": "ch_sig",
        "order_id": oid,
        "amount": "89.00",
    }
    expect_eq(post_webhook(api, payload, signature="00" * 32).status_code, 401, "bad signature")
    r = api.http.post("/webhooks/provider", json=payload)
    expect_eq(r.status_code, 401, "missing signature")
    expect_eq(api.get_order("alice", oid)["status"], "pending", "unchanged")
    fail_payload = {**payload, "event_id": "evt_fail", "type": "payment.failed"}
    expect_eq(post_webhook(api, fail_payload).status_code, 200, "payment.failed")
    expect_eq(api.get_order("alice", oid)["status"], "payment_failed", "failed via webhook")

    api.fault("delayed_webhook", delay_ms=1500)
    oid2 = api.create_order("alice", [("SKU-004", 1)])["order_id"]
    expect_eq(api.pay("alice", oid2).status_code, 202, "pay delayed")
    time.sleep(0.6)
    expect_eq(api.get_order("alice", oid2)["status"], "processing", "still processing")
    api.wait_status("alice", oid2, {"paid"}, timeout=5)


@baseline("refunds: partial then full, cumulative cap", affected_by=("B11",))
def check_refund_flow(ctx: Ctx) -> None:
    api = ctx.api
    order = api.paid_order(sku="SKU-001")
    oid = order["order_id"]
    path = f"/orders/{oid}/refunds"
    unpaid = api.create_order("alice", [("SKU-001", 1)])["order_id"]
    r = api.req("POST", f"/orders/{unpaid}/refunds", "admin", json_body={"amount": "1.00"})
    expect_eq(r.status_code, 409, "refund unpaid order")
    r = api.req("POST", path, "admin", json_body={"amount": "20.00"})
    expect_eq((r.status_code, r.json().get("error")), (422, "refund_exceeds_payment"), "> paid")
    r = api.req("POST", path, "admin", json_body={"amount": "10.00"})
    expect_eq((r.status_code, r.json()["order_status"]), (201, "partially_refunded"), "partial")
    expect_eq(api.get_order("alice", oid)["status"], "partially_refunded", "status partial")
    r = api.req("POST", path, "admin", json_body={"amount": "10.00"})
    expect_eq((r.status_code, r.json().get("error")), (422, "refund_exceeds_payment"), "cumulative")
    r = api.req("POST", path, "admin", json_body={"amount": "9.99"})
    expect_eq((r.status_code, r.json()["order_status"]), (201, "refunded"), "full")
    refunds = api.req("GET", path, "alice").json()
    expect_eq([x["amount"] for x in refunds], ["10.00", "9.99"], "refund list")
    r = api.req("POST", path, "admin", json_body={"amount": "0.01"})
    expect_eq(r.status_code, 409, "refund a refunded order")
    r = api.req("POST", path, "admin", json_body={"amount": "-1"})
    expect_eq(r.status_code, 422, "negative amount")


@baseline("refunds: admin only", affected_by=("B10",))
def check_refund_admin_only(ctx: Ctx) -> None:
    api = ctx.api
    oid = api.paid_order()["order_id"]
    r = api.req("POST", f"/orders/{oid}/refunds", "alice", json_body={"amount": "1.00"})
    expect_eq(r.status_code, 403, "customer refund")
    expect_eq(api.req("GET", f"/orders/{oid}/refunds", "alice").json(), [], "no refunds")


def _device_events(seqs: list[int]) -> dict[str, Any]:
    return {
        "events": [
            {
                "seq": s,
                "ts": f"2026-10-15T10:00:{s:02d}Z",
                "type": "plate_read",
                "plate": f"PLATE{s}",
                "camera": "gate-1",
            }
            for s in seqs
        ]
    }


@baseline("devices: dedupe and highest-seq state", affected_by=("B06",))
def check_devices(ctx: Ctx) -> None:
    api = ctx.api
    path = "/devices/cam-7/events"
    r = api.req("POST", path, "alice", json_body=_device_events([5, 6]))
    expect_eq(r.json(), {"accepted": 2, "duplicates": 0}, "first batch")
    r = api.req("POST", path, "alice", json_body=_device_events([1, 2, 3, 3]))
    expect_eq(r.json(), {"accepted": 3, "duplicates": 1}, "out-of-order batch")
    r = api.req("POST", path, "alice", json_body=_device_events([6, 2]))
    expect_eq(r.json(), {"accepted": 0, "duplicates": 2}, "replayed batch")
    state = api.req("GET", "/devices/cam-7/state", "alice").json()
    expect_eq(
        state,
        {
            "device_id": "cam-7",
            "last_seq": 6,
            "last_ts": "2026-10-15T10:00:06Z",
            "last_plate": "PLATE6",
            "event_count": 5,
        },
        "device state",
    )
    expect_eq(api.req("GET", "/devices/nope/state", "alice").status_code, 404, "unknown device")
    expect_eq(api.http.post(path, json=_device_events([9])).status_code, 401, "auth required")


@baseline("observability: request id, JSON access log, metrics")
def check_observability(ctx: Ctx) -> None:
    api = ctx.api
    oid = api.create_order("alice", [("SKU-001", 1)])["order_id"]
    rid = f"verify-{uuid.uuid4().hex[:12]}"
    r = api.req("GET", f"/orders/{oid}", "alice", headers={"X-Request-ID": rid})
    expect_eq(r.headers.get("x-request-id"), rid, "echoed X-Request-ID")
    generated = api.http.get("/health").headers.get("x-request-id")
    expect(bool(generated), "generated X-Request-ID")
    time.sleep(0.3)
    lines = [ln for ln in checkout_logs() if ln.get("request_id") == rid]
    expect_eq(len(lines), 1, "exactly one access log line")
    line = lines[0]
    for key, value in (("method", "GET"), ("path", f"/orders/{oid}"), ("status", 200)):
        expect_eq(line.get(key), value, f"log {key}")
    expect_eq(line.get("order_id"), oid, "log order_id")
    expect(isinstance(line.get("duration_ms"), (int, float)), "log duration_ms")
    text = api.http.get("/metrics").text
    for name in (
        "orders_created_total",
        "payments_total",
        "voucher_redemptions_total",
        "webhook_duplicates_total",
        'http_request_duration_seconds_bucket{le="0.005",method="GET",route="/orders/{order_id}"}',
    ):
        expect(name in text, f"metric {name} missing")


@baseline("test hooks: clock and reset")
def check_test_hooks(ctx: Ctx) -> None:
    api = ctx.api
    instant = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    api.set_clock(instant)
    clock = api.req("GET", "/__test/clock", "admin").json()
    expect_eq(clock["fixed"], True, "clock fixed")
    order = api.create_order("alice", [("SKU-001", 1)])
    expect_eq(order["created_at"], instant.isoformat().replace("+00:00", ".000Z"), "created_at")
    expect_eq(api.req("DELETE", "/__test/clock", "admin").json()["fixed"], False, "clock real")
    expect_eq(api.req("POST", "/__test/reset", "alice").status_code, 403, "reset needs admin")
    api.req("POST", "/__test/reset", "admin")
    expect_eq(api.req("GET", "/orders", "admin").json()["total_items"], 0, "reset wiped orders")


@baseline("no defect identifiers in UI, responses or logs")
def check_no_leaks(ctx: Ctx) -> None:
    api = ctx.api
    oid = api.create_order("alice", [("SKU-001", 1)])["order_id"]
    bodies = {
        "/": api.http.get("/").text,
        "/app.js": api.http.get("/app.js").text,
        "/docs": api.http.get("/docs").text,
        "/openapi.json": api.http.get("/openapi.json").text,
        "order": api.req("GET", f"/orders/{oid}", "alice").text,
        "401": api.http.get("/orders").text,
        "422": api.apply_voucher("alice", oid, "NOSUCH").text,
    }
    for name, body in bodies.items():
        expect(not ID_PATTERN.search(body), f"defect id found in {name}")
    expect("BUGS" not in bodies["/app.js"] and "bug(" not in bodies["/app.js"], "flag in app.js")
    expect(not ID_PATTERN.search(checkout_raw_logs()), "defect id found in logs")


async def _load(token: str, seconds: float) -> tuple[list[float], int]:
    latencies: list[float] = []
    errors = 0
    deadline = time.perf_counter() + seconds
    limits = httpx.Limits(max_connections=PERF_CLIENTS, max_keepalive_connections=PERF_CLIENTS)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=CHECKOUT, timeout=120, limits=limits) as client:

        async def worker() -> None:
            nonlocal errors
            while time.perf_counter() < deadline:
                started = time.perf_counter()
                r = await client.get(f"/orders?limit={PERF_LIMIT}", headers=headers)
                latencies.append(time.perf_counter() - started)
                if r.status_code != 200:
                    errors += 1

        await asyncio.gather(*[worker() for _ in range(PERF_CLIENTS)])
    return latencies, errors


def _orders_p95(ctx: Ctx, seconds: float) -> float:
    api = ctx.api
    skus = [f"SKU-{n:03d}" for n in range(1, 31)]
    for i in range(PERF_ORDERS):
        api.create_order("alice", [(skus[i % 30], 1), (skus[(i * 7) % 30], 2)])
    latencies, errors = asyncio.run(_load(api.token("alice"), seconds))
    expect_eq(errors, 0, "errors under load")
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    ctx.note(
        f"GET /orders?limit={PERF_LIMIT}, {PERF_CLIENTS} clients, {seconds:.0f}s: "
        f"{len(latencies)} requests, p50={latencies[len(latencies) // 2] * 1000:.0f}ms "
        f"p95={p95 * 1000:.0f}ms"
    )
    return p95


@baseline("performance: GET /orders p95 under 300 ms with 50 clients", affected_by=("B09",))
def check_perf(ctx: Ctx) -> None:
    p95 = _orders_p95(ctx, ctx.perf_seconds)
    expect(p95 < PERF_BASELINE_P95_S, f"p95 {p95 * 1000:.0f}ms >= 300ms")


# --------------------------------------------------------------------------- UI (Playwright)


@contextmanager
def browser_page() -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(base_url=CHECKOUT)
            page.set_default_timeout(10_000)
            yield page
        finally:
            browser.close()


def ui_login_and_checkout(page: Page, user: str = "alice") -> str:
    page.goto("/#/login")
    page.get_by_label("Username").fill(user)
    page.get_by_label("Password").fill(PASSWORDS[user])
    page.get_by_role("button", name="Sign in").click()
    expect_pw(page.get_by_role("heading", name="Products")).to_be_visible()
    page.get_by_role("button", name="Add Wireless Mouse to cart").click()
    expect_pw(page.get_by_test_id("cart-badge")).to_have_text("1")
    page.get_by_role("link", name=re.compile("^Cart")).click()
    page.get_by_role("button", name="Proceed to checkout").click()
    page.wait_for_url(re.compile(r"#/checkout/ord_"))
    expect_pw(page.get_by_test_id("cart-badge")).to_have_text("0")
    return page.url.rsplit("/", 1)[-1]


def ui_voucher_total(ctx: Ctx) -> tuple[str, str]:
    """Apply FLAT5 to a 19.99 order; return (UI total text, API total)."""
    with browser_page() as page:
        oid = ui_login_and_checkout(page)
        expect_pw(page.get_by_test_id("order-total")).to_have_text("$19.99")
        page.get_by_label("Voucher code").fill("FLAT5")
        page.get_by_role("button", name="Apply voucher").click()
        applied = page.get_by_role("status").filter(has_text="Voucher FLAT5 applied")
        expect_pw(applied).to_be_visible()
        shown = page.get_by_test_id("order-total").inner_text()
        expect_pw(page.get_by_label("Voucher code")).to_be_disabled()
        expect_pw(page.get_by_role("button", name="Apply voucher")).to_be_disabled()
    return shown, total_of(ctx.api.get_order("alice", oid))


def ui_pay(page: Page) -> Locator:
    page.get_by_role("button", name="Pay now").click()
    return page.get_by_test_id("status-banner")


@baseline("UI: checkout shows discounted total after a voucher", affected_by=("B07", "B08"))
def check_ui_voucher(ctx: Ctx) -> None:
    shown, api_total = ui_voucher_total(ctx)
    expect_eq(api_total, "14.99", "API total")
    expect_eq(shown, "$14.99", "UI total")


@baseline("UI: declined payment shows an error alert", affected_by=("B12",))
def check_ui_declined(ctx: Ctx) -> None:
    ctx.api.fault("decline")
    with browser_page() as page:
        ui_login_and_checkout(page)
        banner = ui_pay(page)
        expect_pw(banner).to_contain_text("declined")
        expect_pw(banner).to_have_attribute("role", "alert")
        pay_button = page.get_by_role("button", name=re.compile("Pay now|Try payment"))
        expect_pw(pay_button).to_be_disabled()


@baseline("UI: successful payment polls every 500 ms until paid", affected_by=("B08",))
def check_ui_happy(ctx: Ctx) -> None:
    ctx.api.fault("delayed_webhook", delay_ms=1600)
    with browser_page() as page:
        oid = ui_login_and_checkout(page)
        polls: list[float] = []
        page.on(
            "request",
            lambda req: (
                polls.append(time.monotonic())
                if req.method == "GET" and req.url.endswith(f"/orders/{oid}")
                else None
            ),
        )
        banner = ui_pay(page)
        expect_pw(banner).to_contain_text("processing")
        expect_pw(banner).to_contain_text("Payment successful", timeout=8000)
        expect_pw(banner).to_have_attribute("role", "status")
        expect(len(polls) >= 3, f"expected polling, saw {len(polls)} GETs")
        gaps = [b - a for a, b in itertools.pairwise(polls)]
        expect(all(0.4 <= g <= 1.0 for g in gaps), f"poll gaps {gaps}")
        page.get_by_role("link", name="Order history").click()
        expect_pw(page.get_by_role("link", name=oid)).to_be_visible()
        expect_pw(page.get_by_role("row").filter(has_text=oid)).to_contain_text("Paid")


# --------------------------------------------------------------------------- symptoms


@symptom("B01", "ONCE10 redeemed more than once under 20 concurrent applies")
def symptom_b01(ctx: Ctx) -> None:
    codes = asyncio.run(_concurrent_once10(ctx.api))
    ctx.note(f"{codes.count(200)} of 20 concurrent ONCE10 applies succeeded")
    expect(codes.count(200) > 1, f"only {codes.count(200)} success")


@symptom("B02", "duplicate webhook creates a second payment, metric not incremented")
def symptom_b02(ctx: Ctx) -> None:
    api = ctx.api
    api.fault("duplicate_webhook")
    before = api.metrics().get("webhook_duplicates_total", 0)
    oid = api.create_order("alice", [("SKU-003", 1)])["order_id"]
    api.pay("alice", oid)
    api.wait_status("alice", oid, {"paid"})
    time.sleep(0.8)
    payments = api.req("GET", f"/orders/{oid}/payments", "alice").json()
    ctx.note(f"{len(payments)} payments recorded for one charge")
    expect_eq(len(payments), 2, "payments")
    expect_eq(api.metrics().get("webhook_duplicates_total", 0) - before, 0, "duplicates metric")


@symptom("B03", "15% of 19.99 gives 2.99")
def symptom_b03(ctx: Ctx) -> None:
    oid = ctx.api.create_order("alice", [("SKU-001", 1)])["order_id"]
    body = ctx.api.apply_voucher("alice", oid, "SAVE15").json()
    ctx.note(f"discount={body['discount']} total={total_of(body)}")
    expect_eq((body["discount"], total_of(body)), ("2.99", "17.00"), "rounded down")


@symptom("B04", "voucher accepted 0.5 s after valid_until, rejected after 1.5 s")
def symptom_b04(ctx: Ctx) -> None:
    api = ctx.api
    end = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=30)
    api.req(
        "POST",
        "/vouchers",
        "admin",
        json_body={
            "code": "EDGE",
            "kind": "fixed",
            "value": "1.00",
            "valid_from": (end - timedelta(hours=1)).isoformat(),
            "valid_until": end.isoformat(),
        },
    )
    results = []
    for offset in (0.5, 1.5):
        api.set_clock(end + timedelta(seconds=offset))
        oid = api.create_order("alice", [("SKU-002", 1)])["order_id"]
        results.append(api.apply_voucher("alice", oid, "EDGE").status_code)
    ctx.note(f"+0.5s -> {results[0]}, +1.5s -> {results[1]}")
    expect_eq(results, [200, 422], "statuses after valid_until")


@symptom("B05", "provider timeout leaves the order pending; response unchanged")
def symptom_b05(ctx: Ctx) -> None:
    api = ctx.api
    api.fault("timeout")
    oid = api.create_order("alice", [("SKU-003", 1)])["order_id"]
    started = time.perf_counter()
    r = api.pay("alice", oid)
    elapsed = time.perf_counter() - started
    expect(elapsed < 5, "response within 5s")
    expect_eq(
        (r.json()["status"], r.json()["reason"], r.json()["retryable"]),
        ("payment_failed", "provider_timeout", True),
        "response unchanged",
    )
    status = api.get_order("alice", oid)["status"]
    ctx.note(f"response says payment_failed, order is {status}")
    expect_eq(status, "pending", "order status")


@symptom("B06", "device state follows the last received batch, not the highest seq")
def symptom_b06(ctx: Ctx) -> None:
    api = ctx.api
    api.req("POST", "/devices/cam-9/events", "alice", json_body=_device_events([5, 6]))
    api.req("POST", "/devices/cam-9/events", "alice", json_body=_device_events([1, 2, 3]))
    state = api.req("GET", "/devices/cam-9/state", "alice").json()
    ctx.note(f"last_seq={state['last_seq']} event_count={state['event_count']}")
    expect_eq((state["last_seq"], state["event_count"]), (3, 5), "state")


@symptom("B07", "checkout page shows the subtotal as total after a voucher")
def symptom_b07(ctx: Ctx) -> None:
    shown, api_total = ui_voucher_total(ctx)
    ctx.note(f"UI total {shown}, API total {api_total}")
    expect_eq((shown, api_total), ("$19.99", "14.99"), "UI vs API")


@symptom("B08", "order responses use total_amount instead of total")
def symptom_b08(ctx: Ctx) -> None:
    api = ctx.api
    order = api.create_order("alice", [("SKU-001", 1)])
    oid = order["order_id"]
    shapes = [
        order,
        api.get_order("alice", oid),
        api.req("GET", "/orders", "alice").json()["items"][0],
        api.apply_voucher("alice", oid, "FLAT5").json(),
    ]
    for shape in shapes:
        expect("total" not in shape and "total_amount" in shape, f"shape {sorted(shape)}")
    ctx.note("POST/GET/list/voucher responses all carry total_amount")


@symptom("B09", "GET /orders p95 over 1 s with 50 clients")
def symptom_b09(ctx: Ctx) -> None:
    p95 = _orders_p95(ctx, ctx.perf_seconds)
    expect(p95 > PERF_BUGGY_P95_S, f"p95 {p95 * 1000:.0f}ms <= 1000ms")


@symptom("B10", "customer can refund their own order")
def symptom_b10(ctx: Ctx) -> None:
    oid = ctx.api.paid_order()["order_id"]
    r = ctx.api.req("POST", f"/orders/{oid}/refunds", "alice", json_body={"amount": "1.00"})
    ctx.note(f"alice refund -> {r.status_code}")
    expect_eq(r.status_code, 201, "customer refund")


@symptom("B11", "cumulative refunds exceed the paid amount")
def symptom_b11(ctx: Ctx) -> None:
    api = ctx.api
    oid = api.paid_order(sku="SKU-001")["order_id"]
    path = f"/orders/{oid}/refunds"
    codes = [
        api.req("POST", path, "admin", json_body={"amount": "15.00"}).status_code,
        api.req("POST", path, "admin", json_body={"amount": "15.00"}).status_code,
    ]
    total = sum(Decimal(r["amount"]) for r in api.req("GET", path, "admin").json())
    ctx.note(f"refunded {total} on a 19.99 payment, statuses {codes}")
    expect_eq(codes, [201, 201], "both refunds accepted")
    single = api.req("POST", path, "admin", json_body={"amount": "25.00"})
    expect_eq(single.status_code, 409, "order already marked refunded")


@symptom("B12", "declined payment shows the success banner")
def symptom_b12(ctx: Ctx) -> None:
    ctx.api.fault("decline")
    with browser_page() as page:
        oid = ui_login_and_checkout(page)
        banner = ui_pay(page)
        expect_pw(banner).to_contain_text("Payment successful")
        ctx.note(f"banner: {banner.inner_text()!r}")
    expect_eq(ctx.api.get_order("alice", oid)["status"], "payment_failed", "API status")


# --------------------------------------------------------------------------- config run


def config_run(api: Api) -> list[tuple[str, bool, str]]:
    """TEST_MODE=0, FAKE_NOW and unknown BUGS entries."""
    fake = "2030-01-01T00:00:00Z"
    results: list[tuple[str, bool, str]] = []

    def record(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            results.append((name, True, ""))
        except Exception as exc:
            results.append((name, False, str(exc)))

    start_stack("B99,nonsense", test_mode="0", fake_now=fake)
    api._tokens.clear()

    def unknown_warning() -> None:
        warnings = [
            ln
            for ln in checkout_logs()
            if ln.get("level") == "warning" and "unknown" in str(ln.get("message", ""))
        ]
        expect(bool(warnings), "no warning for unknown BUGS entries")

    def hooks_hidden() -> None:
        for method, path in (("POST", "/__test/reset"), ("POST", "/__test/clock")):
            r = api.req(method, path, "admin", json_body={"now": fake})
            expect_eq(r.status_code, 404, f"{method} {path} with TEST_MODE=0")

    def fake_now() -> None:
        order = api.create_order("alice", [("SKU-001", 1)])
        expect_eq(order["created_at"], "2030-01-01T00:00:00.000Z", "created_at under FAKE_NOW")
        expect_eq(order["total"], "19.99", "unknown entries switch nothing on")

    record("unknown BUGS entries ignored with a warning", unknown_warning)
    record("test hooks return 404 when TEST_MODE=0", hooks_hidden)
    record("FAKE_NOW honoured at startup", fake_now)
    return results


# --------------------------------------------------------------------------- runner


@dataclass
class RunResult:
    run: str
    startup_s: float
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def run_one(run: str, api: Api, args: argparse.Namespace) -> RunResult:
    bugs = "" if run == "baseline" else run
    startup = start_stack(bugs)
    api._tokens.clear()
    result = RunResult(run=run, startup_s=startup)
    long_perf = run in ("baseline", "B09")
    ctx = Ctx(
        run=run,
        bugs=frozenset({run}) if run != "baseline" else frozenset(),
        api=api,
        perf_seconds=args.perf_seconds if long_perf else args.perf_seconds_other,
    )
    plan: list[tuple[str, Check]] = []
    if run in SYMPTOMS:
        plan.append(("symptom", SYMPTOMS[run]))
    for check in BASELINE:
        if run in check.affected_by:
            result.skipped.append(check.name)
        else:
            plan.append(("baseline", check))
    for kind, check in plan:
        label = f"[{kind}] {check.name}"
        started = time.perf_counter()
        try:
            api.reset()
            check.fn(ctx)
            result.passed.append(label)
            status = "PASS"
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            if not isinstance(exc, CheckFailed):
                detail += "\n" + traceback.format_exc(limit=3)
            result.failed.append((label, detail))
            status = "FAIL"
        print(f"  {status} {label} ({time.perf_counter() - started:.1f}s)", flush=True)
        if status == "FAIL":
            print(f"       {result.failed[-1][1].splitlines()[0]}", flush=True)
    result.notes = ctx.notes
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="comma-separated runs, e.g. baseline,B03")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--no-config-run", action="store_true")
    parser.add_argument("--perf-seconds", type=float, default=30.0)
    parser.add_argument("--perf-seconds-other", type=float, default=10.0)
    args = parser.parse_args()

    runs = ["baseline", *BUG_IDS]
    if args.only:
        runs = [r.strip() for r in args.only.split(",") if r.strip()]
    if not args.skip_build:
        print("building images…", flush=True)
        compose("build")
        compose("up", "-d", "--wait", env={"BUGS": ""})

    api = Api()
    results: list[RunResult] = []
    suite_started = time.perf_counter()
    for run in runs:
        print(f"\n=== {run} (BUGS={'' if run == 'baseline' else run}) ===", flush=True)
        results.append(run_one(run, api, args))

    config_results: list[tuple[str, bool, str]] = []
    if not args.no_config_run and not args.only:
        print("\n=== config (BUGS=B99,nonsense TEST_MODE=0 FAKE_NOW=2030-01-01) ===", flush=True)
        config_results = config_run(api)
        for name, ok, detail in config_results:
            print(f"  {'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)
        start_stack("")

    print("\n" + "=" * 78)
    print(f"{'run':<10}{'result':<8}{'symptom':<9}{'baseline':<12}{'skipped':<9}{'startup':<8}")
    print("-" * 78)
    for r in results:
        symptom_ok = (
            "-"
            if r.run == "baseline"
            else ("yes" if any(p.startswith("[symptom]") for p in r.passed) else "NO")
        )
        base_total = len([p for p in r.passed if p.startswith("[baseline]")]) + len(
            [f for f, _ in r.failed if f.startswith("[baseline]")]
        )
        base_pass = len([p for p in r.passed if p.startswith("[baseline]")])
        print(
            f"{r.run:<10}{'PASS' if r.ok else 'FAIL':<8}{symptom_ok:<9}"
            f"{f'{base_pass}/{base_total}':<12}{len(r.skipped):<9}{r.startup_s:>5.1f}s"
        )
    if config_results:
        config_ok = sum(1 for _, passed, _ in config_results if passed)
        print(
            f"{'config':<10}{'PASS' if config_ok == len(config_results) else 'FAIL':<8}"
            f"{'-':<9}{f'{config_ok}/{len(config_results)}':<12}"
        )
    print("-" * 78)
    defects = [r for r in results if r.run != "baseline"]
    print(f"defects verified: {sum(r.ok for r in defects)}/{len(defects)}")
    print(f"baseline: {'PASS' if all(r.ok for r in results if r.run == 'baseline') else 'FAIL'}")
    print(f"total time: {time.perf_counter() - suite_started:.0f}s")
    print("\nobservations:")
    for r in results:
        for note in r.notes:
            print(f"  {r.run}: {note}")
    for r in results:
        for label, detail in r.failed:
            print(f"\nFAILED {r.run} {label}\n{detail}")
    all_ok = all(r.ok for r in results) and all(ok for _, ok, _ in config_results)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
