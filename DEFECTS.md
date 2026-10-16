# PayLab — Seeded Defects (PRIVATE)

Do not share this file with testers. Each defect is one code path guarded by
`bug_enabled("Bxx")` and switched on with `BUGS=Bxx`. With `BUGS` empty the system
behaves as specified in `SUT_GUIDE.md`. `scripts/verify_defects.py` proves each symptom
with only that ID on, and re-runs every unrelated baseline check alongside it.

| ID | Area | Where |
|----|------|-------|
| B01 | Voucher redemption race | `app/checkout/routers/vouchers.py` `apply_voucher` |
| B02 | Webhook dedupe skipped | `app/checkout/routers/payments.py` `provider_webhook` |
| B03 | Percent rounding | `app/checkout/orders_service.py` `discount_for` |
| B04 | Voucher expiry grace | `app/checkout/routers/vouchers.py` `apply_voucher` |
| B05 | Timeout leaves order pending | `app/checkout/routers/payments.py` `pay_order` |
| B06 | Device state ordering | `app/checkout/routers/devices.py` `ingest_events` |
| B07 | UI total after voucher | `app/web/templates/app.js` `renderTotals` |
| B08 | `total` renamed | `app/checkout/orders_service.py` `order_to_dict` |
| B09 | N+1 on order list | `app/checkout/routers/orders.py` `list_orders` |
| B10 | Refund role check skipped | `app/checkout/routers/refunds.py` `create_refund` |
| B11 | Refund cap not cumulative | `app/checkout/routers/refunds.py` `create_refund` |
| B12 | UI declined shows success | `app/web/templates/app.js` `payResultBanner` |

---

### B01 — Single-use voucher can be redeemed many times under concurrency
- **Defect:** redemption is check-then-act: read `uses`, check against `max_uses`, wait
  ~50 ms, then write `uses + 1` from the stale read. The baseline does a single
  conditional `UPDATE … WHERE uses < max_uses` and checks the row count.
- **Trigger:** create 20 pending orders, fire 20 concurrent
  `POST /orders/{id}/voucher {"code":"ONCE10"}`.
- **Symptom:** many (typically all 20) succeed instead of exactly one; 19 should get 422
  `voucher_exhausted`. `voucher_redemptions_total` rises by the number of successes while
  `GET /vouchers` still shows `uses: 1` (lost update).
- **Layer:** API concurrency / load test (k6, asyncio, `Promise.all`). A sequential
  functional test does not catch it.

### B02 — Duplicate webhook creates a second payment
- **Defect:** the `event_id` dedupe lookup is skipped.
- **Trigger:** provider mode `duplicate_webhook`, pay an order; or POST the same signed
  webhook twice.
- **Symptom:** `GET /orders/{id}/payments` lists two payments for one charge; the second
  delivery answers `{"duplicate": false}`; `webhook_duplicates_total` does not move. The
  order stays `paid`, and since the paid amount doubles, refunds up to twice the total are
  accepted.
- **Layer:** API integration (webhook contract / idempotency) plus metrics assertion.

### B03 — Percent discount rounds down
- **Defect:** percent discounts are quantized with `ROUND_DOWN` instead of `ROUND_HALF_UP`.
- **Trigger:** SAVE15 on one SKU-001 (19.99).
- **Symptom:** discount `2.99`, total `17.00` instead of `3.00` / `16.99`. Only shows when
  the third decimal is ≥ 5.
- **Layer:** API functional, boundary values / data-driven rounding table. Unit test
  if the tester has code access.

### B04 — Voucher still accepted for 1 s after `valid_until`
- **Defect:** the expiry comparison uses `valid_until + 1 s`.
- **Trigger:** create a voucher, set `/__test/clock` to `valid_until` (or up to
  `valid_until + 0.999 s`) and apply it.
- **Symptom:** 200 instead of 422 `voucher_expired`. At `valid_until + 1 s` or later it is
  rejected correctly, and OLD20 (expired a day ago) is still rejected.
- **Layer:** API functional, boundary-value analysis with the controllable clock.

### B05 — Provider timeout leaves the order `pending`
- **Defect:** on a timeout the order status update is skipped. The HTTP response is
  unchanged (`payment_failed`, `provider_timeout`, `retryable: true`).
- **Trigger:** provider mode `timeout`, pay.
- **Symptom:** response says `payment_failed`, but `GET /orders/{id}` shows `pending`. The
  order is still payable and voucher-able.
- **Layer:** API integration / resilience test that checks state after the response,
  not only the response.

### B06 — Device state follows arrival order
- **Defect:** state is overwritten by every accepted event instead of only by a higher
  `seq`.
- **Trigger:** POST batch `seq 5,6`, then batch `seq 1,2,3`.
- **Symptom:** state shows `last_seq: 3` (and its ts/plate) instead of 6. `event_count` and
  duplicate counting stay correct. In-order delivery hides it.
- **Layer:** API functional, out-of-order / event-ordering test.

### B07 — Checkout UI shows the subtotal as the total after a voucher
- **Defect:** `renderTotals` in the served script uses
  `order.voucher_code ? order.subtotal : order.total`.
- **Trigger:** UI checkout of SKU-001, apply FLAT5.
- **Symptom:** `data-testid="order-total"` shows `$19.99`; the API (and the order detail
  page) say `14.99`. Subtotal and discount lines are right.
- **Layer:** UI end-to-end (Playwright/Cypress) asserting the rendered total against the
  API. API tests pass.

### B08 — `total` renamed to `total_amount`
- **Defect:** every order response (create, get, list items, apply voucher) uses the key
  `total_amount`.
- **Trigger:** any order response.
- **Symptom:** `total` missing; schema/contract validation fails. The UI shows `$undefined`
  for totals as a knock-on effect.
- **Layer:** API contract test (JSON schema / OpenAPI response validation, Pact).

### B09 — N+1 queries and blocking work on `GET /orders`
- **Defect:** items are loaded with one query per order plus a blocking 5 ms sleep per
  order on the event loop.
- **Trigger:** ~200 orders, 50 concurrent clients on `GET /orders?limit=50` for 30 s.
- **Symptom:** p95 well over 1 s (seconds; throughput collapses to a few req/s) versus a
  baseline p95 of ~150 ms on the verification machine. Single requests are ~250 ms, so a
  functional test barely notices. Other endpoints also stall while it runs.
- **Layer:** performance / load test (k6, Locust) with a p95 threshold.

### B10 — Customers can create refunds
- **Defect:** the admin role check in `POST /orders/{id}/refunds` is skipped.
- **Trigger:** alice pays an order, then refunds it with her own token.
- **Symptom:** 201 instead of 403. Ownership still applies (bob gets 404 on alice's order).
- **Layer:** API security / authorization matrix test.

### B11 — Refund cap checks only the single refund
- **Defect:** the check compares the new refund amount with the paid amount instead of
  `already refunded + new amount`.
- **Trigger:** pay 19.99, refund 15.00, then refund 15.00 again.
- **Symptom:** the second refund is accepted (30.00 refunded on 19.99 paid). A single
  over-sized refund (20.00) is still rejected, so a one-step test misses it.
- **Layer:** API functional, multi-step / stateful scenario.

### B12 — Declined payment shows the success banner
- **Defect:** `payResultBanner` only treats *retryable* failures as errors, so a decline
  (`retryable: false`) falls through to the success message.
- **Trigger:** provider mode `decline`, click *Pay now* in the UI.
- **Symptom:** `data-testid="status-banner"` reads "Payment successful. Thank you for your
  order!" with `role="status"`, while the API says `payment_failed` and the button is
  disabled. Provider errors and timeouts still show an error.
- **Layer:** UI end-to-end, negative path (or UI test with a mocked `/pay` response).
