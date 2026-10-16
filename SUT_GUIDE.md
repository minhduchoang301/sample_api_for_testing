# PayLab — System Under Test Guide

PayLab is a small online store with a checkout API, a browser UI and a mock payment
provider. This guide covers what you need to test it: how to run it, the seeded data,
the API rules, the provider's fault modes and the test hooks.

## 1. Running it

Requirements: Docker with Compose v2.

```bash
# from the repository
make up                    # build and start, waits until both services are healthy
make logs                  # follow JSON logs
make down                  # stop and delete the database volume

# or with published images (no source needed)
docker login ghcr.io       # only if the packages are private
PAYLAB_OWNER=<github-owner> docker compose -f compose.consumer.yml up -d --wait
```

| Service  | URL                     | API docs                     |
|----------|-------------------------|------------------------------|
| checkout | http://localhost:8000   | http://localhost:8000/docs   |
| web UI   | http://localhost:8000/  | —                            |
| provider | http://localhost:8001   | http://localhost:8001/docs   |

Both services expose `GET /health` and report healthy in well under 60 s.

### Environment (checkout)

| Variable             | Default                  | Meaning |
|----------------------|--------------------------|---------|
| `BUGS`               | empty                    | Seeded defects to switch on (see §8) |
| `TEST_MODE`          | `1`                      | `1` enables `/__test/*`; `0` makes them 404 |
| `FAKE_NOW`           | empty                    | ISO timestamp; the app clock starts frozen at this instant |
| `JWT_SECRET`         | `paylab-dev-jwt-secret`  | Token signing key |
| `WEBHOOK_SECRET`     | `whsec_paylab_dev`       | Shared with the provider for webhook signatures |
| `PROVIDER_URL`       | `http://provider:8001`   | Provider base URL |
| `PROVIDER_TIMEOUT_S` | `3`                      | Timeout for provider calls (capped at 4.5 s) |

Compose passes `BUGS`, `TEST_MODE`, `FAKE_NOW`, `JWT_SECRET`, `WEBHOOK_SECRET` and
`PROVIDER_TIMEOUT_S` through from your shell.

## 2. Seeded data

Recreated on every `POST /__test/reset`.

**Users**

| Username | Password   | Role     |
|----------|------------|----------|
| alice    | alice123   | customer |
| bob      | bob123     | customer |
| admin    | admin123   | admin    |

**Products:** 30 products, `SKU-001` … `SKU-030`, prices from 7.77 to 249.99.
`SKU-001` is *Wireless Mouse* at 19.99. `GET /products?limit=100` lists them all.

**Vouchers** (times are relative to the app clock at seeding):

| Code   | Kind    | Value | Valid from      | Valid until      | Max uses  |
|--------|---------|-------|-----------------|------------------|-----------|
| SAVE15 | percent | 15    | 1 day ago       | in 365 days      | unlimited |
| FLAT5  | fixed   | 5.00  | 1 day ago       | in 365 days      | unlimited |
| ONCE10 | percent | 10    | 1 day ago       | in 365 days      | 1         |
| OLD20  | percent | 20    | 30 days ago     | 1 day ago        | unlimited |

## 3. API conventions

- **Money** is CAD, serialized as a JSON string with two decimals (`"16.99"`). Requests
  accept a string or number with at most two decimals. Rounding is half-up.
- **Timestamps** are ISO 8601 UTC with milliseconds and `Z` (`2026-10-15T12:00:00.000Z`).
  Input timestamps without an offset are treated as UTC.
- **Errors** are JSON: `{"error": "<code>", "message": "<text>"}`. Validation errors
  (422) add `details`. Unknown request fields are rejected with 422.
- **Auth:** `POST /auth/token` returns a bearer token. Send `Authorization: Bearer <token>`
  on everything except `/health`, `/metrics`, `/auth/token`, `GET /products`, `/docs`,
  `/openapi.json` and the UI files (`/`, `/app.js`, `/static/*`). The provider webhook is
  authenticated by its signature, not a token.
  - Missing, malformed or expired token → **401**. Wrong role → **403**.
  - A customer asking for another customer's order (or its payments/refunds) → **404**.
  - Tokens expire 12 hours after issue, measured on the app clock. If you move the
    clock forward past that, request a new token.
- **Pagination:** `page` ≥ 1, `limit` 1–100 (default 10). Responses are
  `{items, page, limit, total_items}`. Pages past the end return an empty `items`.
- **X-Request-ID:** sent back on every response. If you send one, it is echoed.

## 4. Endpoints

Full schemas are in `/docs`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET    | `/health` | — | `{"status":"ok"}` |
| GET    | `/metrics` | — | Prometheus text |
| POST   | `/auth/token` | — | `{username, password}` |
| GET    | `/products` | — | sorted by `sku` |
| POST   | `/orders` | user | `{items:[{sku, qty}]}` → 201 |
| GET    | `/orders` | user | own orders; admin sees all |
| GET    | `/orders/{order_id}` | user | |
| POST   | `/orders/{order_id}/voucher` | user | `{code}` |
| POST   | `/orders/{order_id}/pay` | user | needs `Idempotency-Key` header |
| GET    | `/orders/{order_id}/payments` | user | |
| POST   | `/orders/{order_id}/refunds` | admin | `{amount}` → 201 |
| GET    | `/orders/{order_id}/refunds` | user | |
| POST   | `/vouchers` | admin | create a voucher → 201 |
| GET    | `/vouchers` | admin | list vouchers with `uses` |
| POST   | `/webhooks/provider` | signature | called by the provider |
| POST   | `/devices/{device_id}/events` | user | camera event batches |
| GET    | `/devices/{device_id}/state` | user | |
| POST   | `/__test/reset` | admin | test mode only |
| GET/POST/DELETE | `/__test/clock` | admin | test mode only |

### Orders

- `qty` must be 1–999 and `sku` must exist, otherwise 422 (`unknown_sku` for an
  unknown SKU). Repeated SKUs in one request are merged into one line.
- `unit_price` always comes from the catalog. `line_total = unit_price × qty`.
- Status flow: `pending → processing → paid | payment_failed`;
  `paid → partially_refunded → refunded` (or straight to `refunded`).
- `GET /orders` is sorted by `created_at`, then `order_id`.

### Vouchers

- `POST /vouchers` body: `{code, kind: "percent"|"fixed", value, valid_from,
  valid_until, max_uses?}`. Codes are stored upper-case; letters, digits, `_` and `-`.
  Percent values must be ≤ 100; `valid_until` must be after `valid_from`. A duplicate
  code → 409 `voucher_code_taken`.
- Applying: codes are case-insensitive. Only on a `pending` order with no payment in
  flight (409 `order_not_pending`), one per order (409 `voucher_already_applied`).
- A voucher is valid when `valid_from <= now < valid_until`.
- Percent discount = `subtotal × value / 100`, rounded half-up. Fixed discount = `value`.
  `total = max(0, subtotal − discount)`. The `discount` field shows the voucher's full
  value even when it is larger than the subtotal.
- Rejections are 422 with `error` set to one of `voucher_not_found`, `voucher_expired`,
  `voucher_not_yet_valid`, `voucher_exhausted`. Checks run in that order
  (not found → not yet valid → expired → exhausted).

### Payments

`POST /orders/{id}/pay` with header `Idempotency-Key: <any string up to 255 chars>`.
Checkout charges the order total through the provider.

| Provider result | Order status | Response |
|-----------------|--------------|----------|
| 202 accepted | `processing` (then `paid` by webhook) | **202** `{order_id, status:"processing"}` |
| 402 declined | `payment_failed` | **200** `{order_id, status:"payment_failed", reason:"declined", retryable:false}` |
| 5xx / unreachable | `payment_failed` | **200** `{…, reason:"provider_error", retryable:true}` |
| timeout | `payment_failed` | **200** `{…, reason:"provider_timeout", retryable:true}` |

- The response always arrives within 5 s.
- Missing header → 400 `idempotency_key_required`.
- Keys are scoped to the user. The same key on the same order returns the stored status
  code and body and never charges again. The same key on a different order → 409
  `idempotency_key_reused`. A second request with the same key while the first is
  still running → 409 `request_in_progress`.
- An order can be paid when it is `pending`, or `payment_failed` with a retryable
  failure (use a new key). Anything else → 409 `order_not_payable`. A second pay while
  a provider call is in flight → 409 `payment_in_progress`.

### Webhooks

`POST /webhooks/provider`, header `X-Signature` = lowercase hex
HMAC-SHA256 of the **raw request body** keyed with `WEBHOOK_SECRET`
(default `whsec_paylab_dev`). Bad or missing signature → 401.

```json
{"event_id":"evt_1","type":"payment.succeeded","charge_id":"ch_1","order_id":"ord_…","amount":"16.99"}
```

```bash
body='{"event_id":"evt_1","type":"payment.succeeded","charge_id":"ch_1","order_id":"ORDER","amount":"16.99"}'
sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac whsec_paylab_dev -hex | sed 's/^.* //')
curl -X POST localhost:8000/webhooks/provider -H "X-Signature: $sig" -H 'Content-Type: application/json' -d "$body"
```

- `payment.succeeded` records a payment (amount from the event) and moves a
  `pending`, `processing` or `payment_failed` order to `paid`.
- `payment.failed` moves a `pending` or `processing` order to `payment_failed`
  (not retryable).
- A repeated `event_id` → 200 `{"received":true,"duplicate":true}` with no state change;
  `webhook_duplicates_total` goes up by one.
- Unknown `order_id` → 404.

### Refunds

`POST /orders/{id}/refunds` (admin) `{amount}`, amount > 0 with ≤ 2 decimals.
- Order must be `paid` or `partially_refunded`, else 409 `order_not_refundable`.
- The sum of all refunds may not exceed the sum of payments: 422 `refund_exceeds_payment`.
- Order becomes `refunded` when refunds equal the amount paid, otherwise
  `partially_refunded`. The 201 response includes `order_status`.

### Devices (licence-plate cameras)

`POST /devices/{device_id}/events` with
`{"events":[{"seq":1,"ts":"2026-10-15T10:00:00Z","type":"plate_read","plate":"ABC123","camera":"gate-1"}]}`
(1–500 events per batch, `seq` ≥ 0) → `{accepted, duplicates}`. An already-stored
`(device_id, seq)`, including a repeat inside the same batch, counts as a duplicate.
`GET /devices/{device_id}/state` → `{device_id, last_seq, last_ts, last_plate, event_count}`
for the highest `seq` received; `event_count` counts unique events. Unknown device → 404.

## 5. Mock payment provider (port 8001)

| Mode | Behaviour on `POST /charges` |
|------|------------------------------|
| `ok` (default) | 202 `{charge_id}`, then a signed `payment.succeeded` webhook ~100 ms later |
| `decline` | 402 `{"error":"card_declined"}`, no webhook |
| `timeout` | sleeps 10 s, then 504 (checkout gives up first) |
| `error_500` | 500 |
| `duplicate_webhook` | like `ok`, but sends the identical webhook twice, 200 ms apart |
| `delayed_webhook` | like `ok`, but waits `delay_ms` before the webhook |

```bash
curl -X POST localhost:8001/admin/faults -H 'Content-Type: application/json' \
     -d '{"mode":"delayed_webhook","delay_ms":3000}'
curl localhost:8001/admin/faults            # current mode
curl -X POST localhost:8001/admin/reset     # back to ok, forget charges
curl localhost:8001/charges/<charge_id>     # inspect a charge (status, webhooks_sent)
```

`delay_ms` defaults to 2000 (0–60000). The mode is global and returns to `ok` on
restart or reset.

## 6. Test hooks (TEST_MODE=1, admin token)

| Call | Effect |
|------|--------|
| `POST /__test/reset` | wipe and reseed the database (vouchers relative to the current app clock) |
| `POST /__test/clock {"now":"2026-12-31T23:59:59Z"}` | freeze the app clock at that instant |
| `GET /__test/clock` | `{now, fixed}` |
| `DELETE /__test/clock` | back to real time |

The clock stays frozen until changed; it does not tick. Reset does not change the clock,
the provider mode or the Prometheus counters (counters only go up; compare deltas).
With `TEST_MODE=0` these paths return 404.

## 7. Observability

- One JSON log line per request on stdout (`docker compose logs checkout`):
  `request_id, method, path, status, duration_ms` and `order_id` when the path has one.
- `GET /metrics`: `orders_created_total`, `payments_total{status}` (status is
  `processing`, `paid` or `payment_failed`), `voucher_redemptions_total`,
  `webhook_duplicates_total`, `http_request_duration_seconds{method,route}`
  (route is the template, e.g. `/orders/{order_id}`).

## 8. Seeded defects

The build contains twelve seeded defects with IDs **B01 to B12**. All are off by default.
Switch any combination on with the `BUGS` variable and restart checkout:

```bash
BUGS=B03 make up
BUGS=B01,B07 docker compose up -d --wait --force-recreate checkout
```

IDs are case-insensitive; unknown entries are ignored and logged as a warning. Finding
out what each one does is the exercise.

## 9. Web UI

Open http://localhost:8000/. It is a hash-routed single page app that calls the API with
`fetch`, so you can intercept or mock requests.

| Route | Page |
|-------|------|
| `#/login` | sign in |
| `#/products?page=N` | catalogue with pagination, *Add to cart* |
| `#/cart` | cart (kept in `localStorage`), *Proceed to checkout* creates the order |
| `#/checkout/{order_id}` | items, voucher box, subtotal/discount/total, *Pay now* |
| `#/orders?page=N` | order history |
| `#/orders/{order_id}` | order detail with payments and refunds |
| `#/admin/refunds/{order_id?}` | admin refund screen |

After *Pay now* the status banner shows the result; while the order is `processing` the
page polls `GET /orders/{id}` every 500 ms until a final status. Only three elements have
`data-testid`: `order-total`, `status-banner` and `cart-badge`. Everything else is
reachable by role and accessible name (labels, buttons, headings, landmarks). Banners
use `role="status"` for info and success and `role="alert"` for errors.
