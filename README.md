# Portfolio Trade Execution Engine

A FastAPI service that takes a first-time portfolio or an explicit rebalance (sell X, buy new Y, adjust Z by ±n) and executes it on the user's broker with one API call. It logs in to the broker, places and tracks every order, copes with broker failures, rate limits and lost responses, and then sends a signed summary of what succeeded, what failed and why.

Five Indian brokers (Zerodha, Upstox, Fyers, AngelOne, Groww) and a simulator sit behind one adapter interface. A browser console at `/ui` runs the whole flow visually: connect, enter or upload a portfolio, execute, view results.

> **Simulator vs live.** The `mock` broker is a full exchange simulator, and everything in the demo runs against it. The five real adapters were built from each broker's published API docs and official SDK source, and pass a shared contract test suite against documented response shapes, but **they have not been verified against live accounts**. Real order placement is **disabled** unless `LIVE_TRADING_ENABLED=true`.

Design document: [`docs/PLAN.md`](docs/PLAN.md) · Progress and decisions log: [`TODO.md`](TODO.md)

---

## Quick start

```bash
cp .env.example .env          # optional: every value has a dev default
docker compose up --build     # API on :8000, Postgres 16
./scripts/demo.sh             # end-to-end walkthrough; exits non-zero if anything is off
```

Then open:
- **Web console:** <http://localhost:8000/ui>. Try the whole flow in the browser; see [Web console](#web-console) below.
- **Swagger UI:** <http://localhost:8000/docs>. The dev API key is `dev-key`.

A full checklist for verifying the project is in [`docs/HOW_TO_VERIFY.md`](docs/HOW_TO_VERIFY.md).

Without Docker (SQLite):

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
DATABASE_URL=sqlite+aiosqlite:///./dev.db .venv/bin/uvicorn --factory app.main:create_app --port 8000
pytest                        # 230 tests, about 15 seconds
```

## Web console

A single-page frontend served by the API at **<http://localhost:8000/ui>**. It is a static HTML file (`frontend/index.html`, vanilla JavaScript, no build step) that only calls the public API, so it exercises the same paths as any other client. It follows the flow in the brief: connect a broker, enter or upload a target portfolio, execute in one click, view the results.

| Step | What you do | What happens |
|---|---|---|
| **1. Connect** | Choose a broker. **Practice account** (the simulator) is selected by default. AngelOne and Groww show login fields; Zerodha, Upstox and Fyers open the broker's login page. | `POST /broker-connections`; current holdings are loaded from the broker. |
| **2. Your trades** | Add Buy/Sell rows (stock + quantity), or **Import from a file** (`RELIANCE,10` to buy, `INFY,-8` to sell). Each row shows how many shares you already hold. | The console turns the rows into the API's instruction types: with no holdings and only buys it sends a first-time portfolio (`INITIAL`); otherwise a `REBALANCE`, where a sell is `sell`, a buy of a held stock is `adjust +n`, and a buy of a new stock is `buy`. |
| **3. Review & place** | Check the plan (sales listed first), then press **Place orders**. | `POST /executions/preview` validates against live holdings without trading; `POST /executions` runs it with an automatically generated `Idempotency-Key`, so pressing again never places the trades twice. |
| **Results** | Watch each order update live. | A summary banner, a progress bar and a plain-language line per order: fill price, the broker's rejection reason, orders still open, or orders needing attention, with a **Check again with broker** action (`POST /executions/{id}/reconcile`). |

**Practice options** (checkboxes in step 1) switch on the simulator's failure scenarios, so you can watch the engine handle them:
- slow fills
- the broker rate-limiting the first orders
- a rejected stock
- a lost broker reply that is recovered without resending

Also available:
- **Recent activity:** past executions on the account.
- **Show technical details:** broker order ids, our order tags and retry counts.
- **Settings:** the access key.
- **Layout:** light and dark themes, and a phone-friendly layout.

All text coming from users or brokers is HTML-escaped before it is shown.

## What it does, and deliberately doesn't

| Does | Doesn't (out of scope) |
|---|---|
| INITIAL: buy every target quantity | Compute rebalance deltas, or convert weights into quantities |
| REBALANCE: explicit `sell`, `buy` (new symbols) and `adjust` (±n) | Strategy logic, F&O, intraday, limit orders, AMO |
| Validate instructions against **live holdings** before trading | Cancel or modify orders (cancelling is a trading decision) |
| Sells first, then buys; poll, reconcile, report | Roll back filled trades (impossible; reported instead) |

Orders are **MARKET, CNC (delivery), NSE, DAY, whole-number quantities**. Brokers that need it get market protection (`market_protection=-1` on Kite and Upstox; AngelOne converts market orders to protected limit orders itself).

---

## Architecture

There is one FastAPI process (one uvicorn worker) and one PostgreSQL database. Executions run as in-process asyncio tasks. **There is no Redis, Celery or queue.** The safety guarantees come from database constraints and committing every state change before the next broker call.

```
 Client ──X-API-Key, Idempotency-Key──▶ API (routers, schemas, one error envelope, request-id)
                                          │
              ┌───────────────────────────┼─────────────────────────────┐
              ▼                           ▼                             ▼
      ConnectionService            ExecutionService              NotificationService
      login flows, Fernet          idempotency, validation,      log + HMAC-signed webhook,
      token vault                  pre-flight, 1-txn insert      retries, separate status
              │                           │ schedules
              │                           ▼
              │                    ExecutionRunner ── phase SELL ─▶ settle ─▶ phase BUY ─▶ finalize ─▶ notify
              │                    (state machine; RecoveryService resumes it on startup)
              ▼                           ▼
        BrokerGateway  ── rate limit · guard timeout · SAFE-only retries · one refresh+retry · kill switch · logs
              ▼
        Adapters: zerodha | upstox | fyers | angelone | groww | mock      (translation only)
              ▼
        Broker REST APIs (httpx, separate connect/read timeouts)          PostgreSQL (SQLAlchemy async)
```

| Component | Responsibility | Must not |
|---|---|---|
| `app/api` | HTTP: auth header, idempotency header, status codes, envelope | Contain trading logic |
| `app/execution/validator.py`, `planner.py` | Pure functions: payload → instructions → ordered legs | Do I/O |
| `app/execution/service.py` | Idempotency, pre-flight checks against the broker, one-transaction persist, schedule | Place orders |
| `app/execution/runner.py` | Order state machine: submit, reconcile, poll, finalize | Know broker details |
| `app/brokers/gateway.py` | Every cross-cutting broker concern, once | Retry an ambiguous submission |
| `app/brokers/<name>/adapter.py` | Translate payloads, statuses and errors | Retry, sleep, touch the DB |
| `app/notifications` | Summary + signed webhook | Change the execution outcome |

**Why one process and no queue?** Order batches are I/O-bound and small. Durability comes from the database: every order row reaches `SUBMITTING` before the broker call, and startup recovery resumes from what the database says. A task queue would add a broker and a worker without adding correctness at this scale. Scaling out needs a shared rate limiter (Redis), which is listed under future work.

---

## Execution flow

**Synchronous part of `POST /executions`** (under a second against the mock):

1. `X-API-Key` → owner. `Idempotency-Key` is **required**.
2. A known key with the same body replays the execution (`200`, `Idempotent-Replayed: true`). A known key with a different body → `422 IDEMPOTENCY_KEY_REUSED`.
3. Structural validation (below) → `422` listing every problem, tied to fields.
4. Connection must be `ACTIVE`, the kill switch is checked, and the session is validated (refreshed if the broker supports it). Every symbol is resolved and **live holdings are fetched and checked**.
5. The execution and all its orders are inserted in **one transaction**, guarded by unique constraints.
6. The runner is scheduled. Response: `202` with the planned orders and `status_url`.

**Asynchronous part (`ExecutionRunner`):** phase SELL (submit concurrently through the rate limiter, then poll the order book until every sell settles or the 60s window passes) → phase BUY (same) → finalize → notify.

### Order state machine

```
PENDING ──commit SUBMITTING──▶ place_order()
   ack ─────────────────────▶ SUBMITTED ──poll──▶ OPEN / PARTIALLY_FILLED ──▶ FILLED | REJECTED | CANCELLED
   not placed, retries used up ─▶ FAILED        (auth expired, broker down, kill switch)
   OrderRejected ───────────────▶ REJECTED      (broker's reason kept)
   AmbiguousSubmission ─▶ RECONCILING ──tag found──▶ SUBMITTED (adopts the broker order id)
                                      └─not found──▶ UNKNOWN   (never resent; execution NEEDS_REVIEW)
Restart: PENDING → SKIPPED · SUBMITTING/RECONCILING → reconcile · SUBMITTED/OPEN → resume polling
```

Every transition is a conditional `UPDATE … WHERE status = <last known>` committed before the next broker call, and each one is appended to `order_events` as an audit trail.

**Execution status:** `COMPLETED` (all filled) · `PARTIALLY_COMPLETED` (some filled, or some still open) · `FAILED` (nothing filled) · `NEEDS_REVIEW` (any order `UNKNOWN`).

---

## Rebalance logic

The caller sends the instructions; the engine never computes deltas, so **the categories are the contract**. A category that contradicts the account's actual holdings means the caller's view is stale, and the engine refuses to trade on a stale view.

```json
{"mode": "REBALANCE", "connection_id": "…",
 "sell":   [{"symbol": "INFY", "quantity": 8}],
 "buy":    [{"symbol": "HDFCBANK", "quantity": 4}],
 "adjust": [{"symbol": "TCS", "delta": -2}, {"symbol": "RELIANCE", "delta": 5}]}
```

| Rule | Error code |
|---|---|
| Quantity is a positive integer; `8.0`, `"8"` and `0` are rejected; `delta ≠ 0` | `VALIDATION_ERROR` |
| A symbol appears at most once per list and in at most one list | `DUPLICATE_SYMBOL`, `CONFLICTING_INSTRUCTIONS` |
| Something to do | `EMPTY_PORTFOLIO`, `EMPTY_INSTRUCTIONS` |
| Per-order and per-execution caps (fat-finger) | `QUANTITY_LIMIT_EXCEEDED`, `TOO_MANY_ORDERS` |
| Symbol resolves at the broker | `INVALID_SYMBOL` |
| INITIAL / `buy`: the symbol must **not** already be held (buying the full quantity would overshoot) | `ALREADY_HELD` |
| `sell` and `adjust −n`: held, and no more than held | `NOT_HELD`, `INSUFFICIENT_HOLDINGS` |
| `adjust +n`: already held (new positions go in `buy`) | `NOT_HELD` |

`adjust +n` becomes a BUY and `−n` a SELL. **All sells run in phase 1 and all buys in phase 2**, so sale proceeds are available for the buys. If a sell fails, the buys still run; the broker's margin check stops any buy that can't be funded, and the report says so. `POST /executions/preview` runs every check, including the live holdings check, and returns the plan without trading.

---

## Broker adapters

```python
class BrokerAdapter(ABC):
    name: ClassVar[str]; capabilities: ClassVar[BrokerCapabilities]   # auth flow, refresh, tag limit, rate limits
    def login_url(state) -> str | None
    async def complete_login(params) -> BrokerSession
    async def refresh_session() -> BrokerSession
    async def validate_session() -> None
    async def get_holdings() -> list[Holding]
    async def list_orders() -> list[OrderSnapshot]      # batch polling + reconciliation by tag
    async def get_order(broker_order_id) -> OrderSnapshot
    def resolve_symbol(symbol, exchange="NSE") -> BrokerInstrument
    async def place_order(req: OrderRequest) -> PlaceOrderAck   # MUST send client_order_id as the broker tag
```

Every broker failure is mapped to one canonical error, and **the key property of each error is whether the order might have been placed**:

| Error | Placed? | Gateway behaviour |
|---|---|---|
| `BrokerUnavailable` (connect refused, connect timeout, DNS) | no | retry with backoff |
| `BrokerRateLimited` (429, or AngelOne's 403 without a token error) | no | retry, honouring `Retry-After` |
| `ReauthRequired` | no | refresh once if supported, else mark the connection `EXPIRED`; later calls fail fast |
| `OrderRejected` / `InvalidInstrument` | decided | final; reason kept |
| `AmbiguousSubmission` (read timeout, dropped connection, any 5xx or unparseable reply to a write, a success reply with no id, guard timeout, an unexpected adapter exception) | **maybe** | **never retried**; reconcile by tag |

| Broker | Login | Refresh | Tag field (limit) | Notes |
|---|---|---|---|---|
| Zerodha (Kite v3) | redirect, `request_token` + sha256 checksum | — (re-login daily, ~06:00 IST) | `tag` (20) | `market_protection=-1`; 502 `NetworkException` on a write is ambiguous |
| Upstox | OAuth2 code, `state` echoed | — (expires 03:30 IST) | `tag` (40) | v3 place endpoint, `instrument_token = NSE_EQ\|<ISIN>`, `slice=false` |
| Fyers v3 | auth code + `appIdHash` | needs the user's PIN, which we don't store → none | `orderTag` | status codes 1/2/4/5/6/7 |
| AngelOne | client code + PIN + TOTP (used once) | `generateTokens` | `ordertag` (<20) | **403 also means rate limited**; `status` may be the string `"false"`; reads limited to 1/s |
| Groww | user API key + TOTP or secret | — | `order_reference_id` (8–20) | paginated order list |

Our `client_order_id` is `KX` plus 16 base32 characters (18 alphanumeric). It fits every limit above, and the contract suite asserts that it does.

**Instrument identifiers.** Upstox needs ISINs and AngelOne needs NSE exchange tokens; the other three take the trading symbol. A wrong identifier would trade the wrong stock. `app/brokers/instruments/nse_equity.json` (2,670 NSE equities) is generated by `python scripts/refresh_instruments.py` from Upstox's official instrument master, with every token cross-checked against AngelOne's master; the script refuses to write if they disagree. With live trading on, entries that aren't verified are refused. The first refresh caught a wrong ISIN in the original hand-curated seed (KOTAKBANK, changed by a stock split), which is why hand-written identifiers are never trusted.

**Adding a 6th broker (e.g. Dhan):** (1) create `app/brokers/dhan/adapter.py` subclassing `BrokerAdapter`; (2) decorate it with `@register_broker("dhan")`, and the registry discovers it automatically; (3) add `DHAN_*` settings; (4) add a `BrokerSpec` to `tests/adapters/fixtures.py`. Passing the contract suite is the definition of done. The engine, API and database don't change.

## Library evaluation: why our own thin adapters

| Option | Verdict |
|---|---|
| **OpenAlgo** | Actively maintained and covers all five brokers, but it's a self-hosted Flask *platform* with its own UI, database and browser login, and it's AGPL-3.0. We would be running and securing a second service, and the license needs legal review before embedding or modifying it. Documented as a possible `OpenAlgoAdapter`. |
| **Official SDKs** (`kiteconnect`, `upstox-python-sdk`, `fyers-apiv3`, `smartapi-python`, `growwapi`) | The most trustworthy source for payloads and auth, and we used their source as the reference. But they are synchronous, each has its own exception types, and they **hide timeout semantics**. "Was it sent?" is the most important question this system asks. |
| **Own adapters on httpx** ✔ | Async, one transport, explicit connect-versus-read timeouts, uniform error mapping, easy to test with respx. Wrapping an SDK inside one adapter is still possible if a broker's auth is painful; the interface stays the same. |

---

## Idempotency and failure recovery

| ID | Created by | Purpose |
|---|---|---|
| `Idempotency-Key` | client, one per execution intent | retries return the same execution; unique per `(owner, key)` plus a body hash |
| `execution_id` | server | handle for status, logs, notifications |
| `client_order_id` | server, stored **before** sending | sent as the broker tag; the **reconciliation key** |
| `broker_order_id` | broker | status polling |

Layered protection:
1. **Request level:** a unique constraint on `(owner_id, idempotency_key)`. Concurrent identical requests produce one insert, and the losers replay the winner.
2. **Account level:** a partial unique index allows only one `ACCEPTED`/`RUNNING` execution per connection. A double-click with a fresh key gets `409 EXECUTION_IN_PROGRESS`.
3. **Order level:** `PENDING → SUBMITTING` is a conditional update, so two runners can never send the same order.

**The hard case: we sent the order and the response was lost.** Indian broker APIs don't deduplicate by client tag, so resending is never safe. The order row is already `SUBMITTING` with its tag. On an ambiguous outcome it moves to `RECONCILING`, and the engine searches the order book for the tag a few times to allow for broker lag. If it's found, the engine adopts the broker order id and keeps tracking it. If not, the order becomes `UNKNOWN`, the execution `NEEDS_REVIEW`, and the notification says so. `POST /executions/{id}/reconcile` re-checks later. Once the order has been absent from the book for `RECONCILE_CONFIRM_AFTER_S` (120s), it becomes `FAILED(NOT_PLACED_CONFIRMED)` and is safe to re-run.

> **A missed trade can be re-run. A duplicate trade costs real money. When in doubt, stop and surface it.**

**Crash recovery:** on startup every `ACCEPTED`/`RUNNING` execution is resumed in recovery mode. `PENDING` orders become `SKIPPED` (prices may have moved, so they are never auto-sent), `SUBMITTING` orders are reconciled, live orders resume polling, and the execution is finalized and notified. An unexpected internal error during a run triggers a fail-safe instead (`PENDING → SKIPPED`, uncertain orders → `UNKNOWN`), so an account is never left locked in `RUNNING`.

## Failure handling

| Situation | Result |
|---|---|
| Invalid credentials / bad OAuth state | `401 INVALID_BROKER_CREDENTIALS` / `400 INVALID_STATE`; nothing stored |
| Token expired before the run | refreshed if supported, else `409 BROKER_REAUTH_REQUIRED`, connection `EXPIRED`, nothing created |
| Token expires mid-run | one refresh attempt; then the current order `FAILED(AUTH_EXPIRED)` and the rest fail fast **without being sent** |
| Stored token can't be decrypted (key rotated) | treated as expired → reconnect; never a 500 |
| Broker down (pre-flight / mid-run) | `503`, nothing created / `FAILED(BROKER_UNAVAILABLE)` after bounded retries |
| 429 / rate limit | token bucket per account + backoff honouring `Retry-After` |
| Rejected (funds, RMS, DDPI, circuit) | `REJECTED` with the broker's message |
| Partial fill / still open after the window | reported as `PARTIALLY_FILLED` / `OPEN`; **never auto-cancelled** (DAY orders expire) |
| Response lost after sending | reconciled by tag, or `UNKNOWN` → `NEEDS_REVIEW` |
| Server crash | startup recovery (above) |
| Webhook down | 3 attempts with backoff; `notification.status=FAILED`; execution status unchanged; `POST /executions/{id}/notify` resends |
| Market closed, circuit limits | passed through from the broker (not enforced by the engine) |

---

## API

All errors share one envelope: `{"error": {"code", "message", "details": [...]}}`. Every route except `/health` and the OAuth callback requires `X-API-Key`.

| Method & path | Purpose |
|---|---|
| `GET /health` | liveness + DB |
| `GET /brokers` | supported brokers, auth flow, live-enabled flag |
| `POST /broker-connections` | `{broker, credentials}`: `201 ACTIVE` (credential brokers, mock) or `200 PENDING_LOGIN` + `login_url` (redirect brokers) |
| `GET /broker-connections/{broker}/callback` | OAuth redirect target (single-use `state`) |
| `GET /broker-connections[/{id}]`, `DELETE /broker-connections/{id}` | status (never tokens) / revoke and wipe tokens |
| `GET /broker-connections/{id}/holdings` | normalized holdings |
| `POST /executions/preview` | validate + plan, no trading |
| `POST /executions` | execute (`Idempotency-Key` required) → `202` |
| `GET /executions[/{id}]` | status, orders, summary, notification |
| `POST /executions/{id}/reconcile` | re-check `UNKNOWN`/open orders against the broker |
| `POST /executions/{id}/notify` | resend the notification |
| `POST/GET /dev/webhook-sink` | dev-only webhook receiver for the demo |

```bash
curl -s -X POST localhost:8000/broker-connections -H 'X-API-Key: dev-key' -H 'Content-Type: application/json' \
  -d '{"broker":"mock","credentials":{"client_id":"DEMO1","initial_holdings":{"INFY":8},
       "scenario":{"symbols":{"ITC":"TIMEOUT_AFTER_PLACE"},"rate_limit_first_n":2}}}'

curl -s -X POST localhost:8000/executions -H 'X-API-Key: dev-key' -H 'Idempotency-Key: rebal-001' \
  -H 'Content-Type: application/json' \
  -d '{"mode":"REBALANCE","connection_id":"<id>","callback_url":"http://localhost:8000/dev/webhook-sink",
       "sell":[{"symbol":"INFY","quantity":8}],"buy":[{"symbol":"ITC","quantity":20}]}'
```

## Notifications

The summary is always logged (`execution.summary`). When `callback_url` is set, it is also POSTed:

```json
{"event": "execution.finished", "event_id": "evt_…", "execution_id": "…", "status": "PARTIALLY_COMPLETED",
 "summary": {"total": 5, "filled": 4, "partially_filled": 0, "open": 0, "rejected": 1, "failed": 0, "unknown": 0, "skipped": 0},
 "orders": [{"symbol": "HDFCBANK", "side": "BUY", "quantity": 4, "filled_quantity": 0, "status": "REJECTED",
             "broker_order_id": "…", "error_code": "ORDER_REJECTED", "error_message": "RMS: …"}]}
```

Headers are `X-Event-Id` and `X-Signature: sha256=<HMAC-SHA256(body, WEBHOOK_SECRET)>`. Delivery is at-least-once, so consumers should dedupe on `X-Event-Id`. The id is derived from the outcome: a retry or resend of the same outcome reuses it, and an outcome changed by reconcile gets a new one. Outside dev, `callback_url` must be https and must not resolve to a private or reserved address.

## Configuration

See [`.env.example`](.env.example). The most important settings:

| Variable | Default | Meaning |
|---|---|---|
| `API_KEYS` | `dev-key:demo-owner` | `key:owner` pairs |
| `TOKEN_ENCRYPTION_KEY` | random per process in dev | Fernet key for broker tokens; **required** outside dev |
| `LIVE_TRADING_ENABLED` | `false` | kill switch for real brokers |
| `WEBHOOK_SECRET` | `dev-webhook-secret` | HMAC key |
| `MAX_QTY_PER_ORDER` / `MAX_ORDERS_PER_EXECUTION` | 100000 / 50 | fat-finger caps |
| `ORDER_MONITOR_TIMEOUT_S` / `POLL_INTERVAL_S` | 60 / 1 | per-phase monitoring window |
| `RECONCILE_ATTEMPTS` / `RECONCILE_DELAY_S` / `RECONCILE_CONFIRM_AFTER_S` | 3 / 3 / 120 | ambiguous-outcome handling |
| `<BROKER>_*` | empty | the platform's broker app credentials |

## Docker

`docker compose up` starts `api` (python:3.12-slim, non-root, healthcheck, **one uvicorn worker on purpose**) and `db` (postgres:16-alpine with a healthcheck and a named volume). Tables are created at startup. Redis and Celery are deliberately absent (see Architecture).

## Testing

```bash
pytest                                  # everything, SQLite
TEST_DATABASE_URL=postgresql+asyncpg://… pytest tests/engine tests/api
```

| Suite | What it proves |
|---|---|
| `tests/unit` | validator rules, planner ordering, tag format, status aggregation, **gateway retry classification** (ambiguous is never retried, a refresh happens once even under concurrency, the kill switch holds) |
| `tests/adapters` | **contract suite run against every real adapter**: payloads including the tag, 429/connect/read-timeout/5xx/garbage/rejection/auth mapping, status and holdings mapping, tag limits; plus login checksums and broker quirks |
| `tests/engine` | end-to-end on the mock: happy path, sells before buys (from the exchange's event log), lost response → **exactly one broker order**, never-placed → `UNKNOWN` with **zero resends**, 429s, partial fills, mid-run expiry, broker down, idempotency races, crash recovery, webhooks; plus a full live-mode rebalance through the real Zerodha adapter against mocked Kite endpoints |
| `tests/api` | auth, envelope, owner scoping, tokens never returned and encrypted at rest |

**Mock scenarios**, set per connection (`credentials.scenario`):
- Per symbol: `SUCCESS`, `REJECTED`, `REJECTED_ON_PLACE`, `PARTIAL_FILL`, `PENDING`, `CANCELLED`, `TIMEOUT_AFTER_PLACE`, `TIMEOUT_NOT_PLACED`, `BROKER_DOWN`, `INVALID_SYMBOL`.
- Per account: `fill_delay_s`, `rate_limit_first_n`, `retry_after_s`, `fail_reads_first_n`, `session_expired`, `expire_session_after_orders`, `supports_refresh`, `login`, `funds`, `latency_ms`.

## Security and observability

- **Tokens:** broker tokens are Fernet-encrypted at rest, decrypted only to build a gateway, and never returned by any endpoint. PINs, TOTPs and secrets are used once for login and never stored.
- **Scoping and safety:** every query is scoped to the API key's owner. The kill switch, caps, single-use OAuth `state` and HMAC-signed webhooks are all on by default.
- **Logs:** structured JSON logs carry `request_id`, `owner_id`, `execution_id`, `connection_id`, `broker` and the order ids. A processor redacts token, secret, PIN, TOTP, authorization and API-key fields, and httpx request logging is silenced.
- **Example trail:** filtering on an `execution_id` gives `order.submit.ambiguous` → `order.reconcile.found` → `order.status.changed FILLED`, which shows the order was placed exactly once.
- **Deployment:** SEBI requires API orders to come from a whitelisted static IP, so production needs a fixed egress IP. HTTPS terminates at the load balancer.

## Assumptions

- MARKET / CNC / NSE / DAY only.
- Sells run before buys, and buys still run after a failed sell.
- Strict holdings validation.
- The `Idempotency-Key` is mandatory; a request rejected by validation creates nothing, so its key can be reused.
- One active execution per broker account.
- No automatic resend after an ambiguous outcome, and no auto-cancel.
- Market hours are enforced by the broker, not the engine.
- Selling CNC holdings needs DDPI/eDIS authorisation at the broker; rejections for this are passed through.
- "Upstox / Indiabulls" in the brief → Upstox.

## Trade-offs

| Choice | Why |
|---|---|
| Async (202 + poll + webhook) over sync | Order monitoring outlasts HTTP timeouts, and long requests invite client retries (and duplicates) |
| In-process tasks + DB state over a queue | Same correctness with fewer moving parts; recovery comes from the DB |
| Best effort per order over an "atomic" batch | Fills can't be rolled back; compensating trades are a human decision |
| Fail closed on stale holdings | A rebalance built on the wrong holdings is worse than no rebalance |
| `UNKNOWN` over auto-retry | A duplicate costs money; a missed order can be re-run |

## Limitations and future work

- **No live verification:** the five real adapters have not been run against live accounts. Unverified details are flagged in each adapter's docstring (for example, Fyers' order-book tag echo format).
- **Instruments:** the map was generated on 2026-10-01; ISINs change with corporate actions, so schedule `scripts/refresh_instruments.py` daily.
- **Scaling:** to scale horizontally, move to a shared rate limiter (Redis) and a work queue, or use row leasing.
- **Reconciliation:** a scheduled end-of-day job comparing the order book with the database for all accounts.
- **Schema migrations:** Alembic instead of `create_all`.
- **Broader trading:** limit orders, AMO, cancel/modify, and a `sell_failure_policy`.
- **Live status:** postbacks or websockets instead of polling.
- **More brokers:** an OpenAlgo adapter.
- **Webhook SSRF:** checks resolve DNS at request time; DNS rebinding isn't covered.
