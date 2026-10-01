# Portfolio Trade Execution Engine — Design Plan

This is the design the implementation follows. Progress against it is tracked in [`TODO.md`](../TODO.md).
Where the implementation deliberately deviates, the deviation is noted in `TODO.md` under "Decisions log".

## Facts checked before the design

- **OpenAlgo** (the main open-source option for connecting to many Indian brokers) is actively maintained and supports all 5 brokers we need. But it's an AGPL-3.0 licensed, self-hosted Flask app with its own UI and database, not a library we can import.
- **Groww** now has an official trading API and Python SDK (`growwapi`). It accepts a client-supplied `order_reference_id`.
- **SEBI's retail-algo rules** are now in force. API orders need a whitelisted static IP, anything above 10 orders per second needs strategy registration, and brokers are adding "market protection" requirements to API market orders.
- **Zerodha already limits API orders to 10 per second.**

These points shape Steps 5, 8 and 14.

---

# STEP 1 — Requirement understanding

**In my own words:** Build a FastAPI service that receives a list of trade instructions and places them with the user's broker from one API call. The instructions are either a first-time portfolio (buy everything) or an explicit rebalance (sell X, buy new Y, adjust Z by ±n). The service must log in to the broker, place and track each order, cope with broker failures and rate limits, and then send the caller a summary of what succeeded, what failed and why. Brokers sit behind one common interface (adapter pattern), so adding one is cheap.

| Category | Items |
|---|---|
| **Explicitly required** | ≥5 Indian brokers behind one adapter interface • broker authentication • first-time BUY flow • executing explicit SELL / BUY_NEW / ADJUST instructions with validation • a post-execution notification (succeeded, failed, reasons), which may be mocked • Python + FastAPI • Dockerfile + docker-compose.yml • error handling for broker API failures, rate limits and failed trades • public repo with a README covering setup, Docker, architecture, rebalance logic and library justification |
| **Optional / bonus** | Basic frontend (upload portfolio → connect broker → execute → view results) |
| **Not required** | Calculating rebalance deltas • converting weights to quantities or fetching prices • strategy logic • F&O, intraday, derivatives • modifying orders • user signup and management • P&L or reporting • market-data streaming • horizontal scaling |
| **Assumptions needed** | Order type, exchange, product, sequencing, idempotency semantics, sync vs async, partial-fill semantics, how the "consumer" is identified (see Step 2) |
| **Must be mocked** | Live order placement (no funded accounts, API subscriptions or static IP) • broker login for the demo • webhook receiver. The real adapters get built and unit-tested against documented response formats, but the README will say plainly that they weren't verified against live accounts. |

One subtle point: "single click" means one request triggers the whole batch. It does **not** mean the trades are atomic. Once an order fills it can't be rolled back, so the engine is best-effort per order and reports honestly what happened.

---

# STEP 2 — Ambiguities and assumptions (highest architectural impact first)

| # | Ambiguity | Assumption to document | Why it matters |
|---|---|---|---|
| 1 | Sync or async execution? | **Async.** `POST /executions` validates synchronously and returns `202` with an `execution_id`. Orders then run in a background task. The client polls `GET /executions/{id}` and/or gets a webhook. | Watching order status can take tens of seconds, and holding the HTTP connection open encourages client retries, which create duplicates. |
| 2 | Duplicate requests, retries, double clicks | An **`Idempotency-Key` header is mandatory**. The same key returns the same execution. A different key while an execution is still running on the same broker account gets `409`. | This is the core safety property. |
| 3 | Order sent, then network timeout before the response | **Never blindly resend.** Every order carries our own `client_order_id` as the broker order tag. On an ambiguous outcome, look it up in the broker's order book. If it can't be confirmed, mark it `UNKNOWN` and flag for review. | A duplicate buy costs real money. A missed buy can simply be re-run. |
| 4 | Order in which to run SELL and BUY | **Two phases:** all sells, wait until each reaches a final state, then all buys. ADJUST +n becomes a BUY and −n becomes a SELL. | Sale proceeds pay for the buys. |
| 5 | Sell fails during a rebalance: still buy? | **Continue with buys.** The broker's margin check naturally stops buys without funds, and the report flags it clearly. A `sell_failure_policy` field is a possible later addition. | Keeps instructions independent, as the assignment frames them. |
| 6 | Order parameters | **MARKET, CNC (delivery), NSE, DAY validity, whole-number quantity, cash equity only.** Where a broker requires market protection on API market orders, the adapter adds it. | Keeps the canonical order model small. |
| 7 | "First-time portfolio" when holdings already exist | Ignore holdings unrelated to the request. **Reject** if a target symbol is already held, because buying the target quantity would overshoot the target. | Fail closed. |
| 8 | Rebalance categories don't match actual holdings (SELL more than held, BUY_NEW of a symbol already held, ADJUST on a symbol not held) | **Reject with 422.** The engine doesn't compute deltas, so the categories are the contract. A mismatch means the caller's view of holdings is out of date, which is dangerous for a rebalance. | Validation needs a holdings check before execution. |
| 9 | Auth expiry | Tokens are **typically daily** (details in Step 4). **Validate the session before placing anything.** Refresh if the broker supports it; otherwise mark the connection `EXPIRED` and return `BROKER_REAUTH_REQUIRED`. | Avoids failing halfway through a batch. |
| 10 | Partial fills or orders still open | Monitor each order for a bounded time (60s by default). Report `PARTIALLY_FILLED` / `OPEN` with the filled quantity. **Never cancel automatically.** A DAY order expires at the end of the session. | Cancelling is itself a trading decision. |
| 11 | Notification fails after trades succeed | Notification is **separate from the execution result**. Retry with backoff, store `notification_status`, and provide a manual resend endpoint. | Trades are the source of truth, not the notification. |
| 12 | Rate limits | A **token bucket per broker account** (configured per broker, default ≤10 orders/s in line with SEBI and Zerodha), limited concurrency, and backoff on 429 honouring `Retry-After`. | — |
| 13 | Server crashes mid-execution | Recover on startup. **Reconcile** orders that were in flight. **Do not auto-submit** orders never sent (prices may have moved); mark them `SKIPPED` and let the user re-run. | — |
| 14 | Who is "the consumer"? | An API client (e.g. Kalpi's backend) identified by `X-API-Key` and mapped to an `owner_id`. Optional `callback_url` per request. No user management. | Keeps auth out of scope while scoping data by owner. |
| 15 | Symbol format | Canonical symbol = **NSE trading symbol** (`RELIANCE`). Each adapter converts it to the broker's format (Fyers `NSE:RELIANCE-EQ`, Upstox instrument key, AngelOne symbol token). | Stops broker-specific formats leaking into the core. |
| 16 | Market hours | **Not enforced** by the engine. The broker is the source of truth and rejects out-of-hours orders (after-market orders are out of scope). Documented. | — |
| 17 | Selling CNC holdings in India requires DDPI/POA or CDSL eDIS/TPIN authorisation | Documented. Broker rejections of this kind are passed through with a clear reason. | A real cause of failed sells. |
| 18 | "Upstox / Indiabulls" | Choose **Upstox** (clean, documented v2 REST API). | — |

---

# STEP 3 — Architecture

A **single FastAPI process** (one uvicorn worker), a PostgreSQL database, and an in-process background runner. There is no queue, no Redis and no Celery. Correctness guarantees (idempotency, one active execution per account, crash recovery) come from **database constraints and write-before-send state changes**, not from extra infrastructure.

```
   Client (Swagger / curl / tiny static UI)
        |  X-API-Key, Idempotency-Key
        v
+----------------------------- FastAPI app (1 process) -------------------------------+
|  API layer: routers + Pydantic schemas + error envelope + request-id middleware      |
|     /brokers   /broker-connections   /executions   /health   (/dev/webhook-sink)     |
|        |                   |                                                         |
|        v                   v                                                         |
|  ConnectionService    ExecutionService                                               |
|  (login, token vault) (idempotency, validation, plan, persist)--> Validator/Planner   |
|        |                   | schedules                                               |
|        |                   v                                                         |
|        |             ExecutionRunner (asyncio task per execution)                    |
|        |               phase SELL -> await final states -> phase BUY -> finalize      |
|        |               per order: submit -> [reconcile] -> poll -> persist            |
|        |                   |                                  |                      |
|        v                   v                                  v                      |
|   BrokerGateway (wraps one adapter per connection)       NotificationService         |
|     rate limiter | retry(safe errors only) | timeouts |    LogNotifier, WebhookNotifier|
|     auth refresh | structured logging | error mapping     (HMAC, retries)            |
|        |                                                      |                      |
|        v                                                      |                      |
|   BrokerRegistry -> Adapter: Zerodha|Fyers|AngelOne|Upstox|Groww|Mock                 |
|                                                                                      |
|   Repositories (SQLAlchemy async) ------------------------------------> PostgreSQL   |
|   RecoveryService (on startup: resume/reconcile RUNNING executions)                  |
+-----------------------|--------------------------------------------|----------------+
                        v httpx                                      v HTTPS POST
                 Broker REST APIs                            Consumer webhook
```

**Component responsibilities**

| Component | Responsibility | Must NOT |
|---|---|---|
| API layer | HTTP concerns: auth header, idempotency header, schema validation, status codes, error envelope | Contain trading logic |
| ConnectionService | Start and complete broker login, encrypt and store tokens, check or refresh sessions, mark connections `EXPIRED` | Expose tokens in responses |
| ExecutionService | Idempotency lookup, pre-flight validation (symbols, session, holdings), build the order plan, create execution and orders in **one transaction**, schedule the runner | Call brokers to place orders |
| Validator / Planner | Pure functions. Payload → validated instructions → ordered legs (phase, seq, side, qty) | Do I/O (holdings are passed in) |
| ExecutionRunner | The order state machine: submit, reconcile, poll, finalize, notify. Persists every transition | Know broker-specific details |
| BrokerGateway | Cross-cutting broker concerns: rate limit, timeouts, retry of **safe** errors, one token refresh plus retry, structured call logs | Retry ambiguous order submissions |
| Broker adapters | Translation only: auth exchange, canonical ↔ broker payloads, status mapping, error mapping, symbol resolution | Retry, sleep, touch the DB |
| NotificationService | Build the summary, deliver it (log + webhook with HMAC signature and retries), record delivery status | Change the execution outcome |
| Repositories | Data access, the unique-constraint handling behind idempotency | — |
| RecoveryService | On startup, reconcile executions that were running | Auto-send orders that were never sent |
| Config (`pydantic-settings`) | Environment settings, broker app keys, encryption key, `LIVE_TRADING_ENABLED` kill switch, limits | — |

---

# STEP 4 — Broker adapter design

**Key idea:** adapters are **thin translators**. Everything cross-cutting (retries, rate limits, timeouts, refresh, logging) lives once in the `BrokerGateway`. So a new broker only adds mapping code, and broker logic can't leak upward.

**Canonical types** (the only types the engine sees):
- `OrderRequest`: symbol, exchange, side, quantity, order_type=MARKET, product=CNC, validity=DAY, `client_order_id`
- `PlaceOrderAck`: broker_order_id
- `OrderSnapshot`: broker_order_id, client_order_id (tag), `status: OrderState` (OPEN, FILLED, PARTIALLY_FILLED, REJECTED, CANCELLED), filled_qty, pending_qty, avg_price, status_message
- `Holding`: symbol, quantity (settled + T1 sellable), avg_price
- `BrokerSession`: access_token, refresh_token?, expires_at, broker_user_id

**Canonical errors** (adapters must map every broker error to one of these). Each has a `placement` meaning:

| Error | Meaning | Gateway behaviour |
|---|---|---|
| `ReauthRequired` / `InvalidCredentials` | Token expired or bad credentials | Refresh once if supported, else fail fast. The order was **not placed**. |
| `BrokerRateLimited(retry_after)` | 429 | Back off and retry. **Not placed.** |
| `BrokerUnavailable` | Connection refused, DNS failure, 503 before the request was sent | Retry with backoff. **Not placed.** |
| `OrderRejected(reason)` / `InvalidInstrument` | Broker rejected the order | No retry. Permanent. |
| `AmbiguousSubmission` | Read timeout after sending, 5xx after sending, response we can't parse | **Do not retry.** Reconcile. |
| `BrokerProtocolError` | Unexpected response shape on a read | Retry reads. Fail after N attempts. |

Using httpx with separate **connect** and **read** timeouts is what makes "definitely not sent" versus "ambiguous" distinguishable. It's one reason to prefer direct REST calls (Step 5).

**Interface contract (method signatures only, no implementation):**

```text
class BrokerAdapter(ABC):
    name: ClassVar[str]                        # "zerodha"
    capabilities: ClassVar[BrokerCapabilities] # auth_flow=REDIRECT|CREDENTIALS, supports_refresh,
                                               # supports_order_tag, max_tag_len, rate_limits
    # --- auth ---
    def login_url(state) -> str | None                  # REDIRECT brokers only
    async def complete_login(params: dict) -> BrokerSession   # code/request_token or creds+TOTP
    async def refresh_session(session) -> BrokerSession      # raises ReauthRequired if unsupported
    async def validate_session() -> None                      # cheap call (profile/funds)
    # --- reads ---
    async def get_holdings() -> list[Holding]
    async def get_order(broker_order_id) -> OrderSnapshot
    async def list_orders() -> list[OrderSnapshot]           # today's order book; used for tag reconciliation
    def resolve_symbol(symbol, exchange) -> BrokerInstrument  # raises InvalidInstrument
    # --- writes ---
    async def place_order(req: OrderRequest) -> PlaceOrderAck
    async def cancel_order(broker_order_id) -> None           # interface only; not used by the engine in the MVP
```

**Common vs broker-specific**

| In the common interface | Stays broker-specific (inside the adapter) |
|---|---|
| The methods above, canonical models, error taxonomy, capability flags | Login URLs and checksums (Zerodha `sha256(api_key+request_token+secret)`, Fyers `appIdHash`), AngelOne TOTP login, Groww key/secret or TOTP |
| `OrderState` enum (partial fills worked out from quantities, not broker strings) | Status strings (Zerodha `OPEN PENDING`, `TRIGGER PENDING`…) and mapping them to `OrderState` |
| `client_order_id` as the tag | Tag field name (`tag`, `orderTag`, `ordertag`, `order_reference_id`) and length limits |
| — | Symbol formats and instrument lookup (Upstox instrument key via ISIN, AngelOne `symboltoken`), market-protection parameters, form vs JSON bodies |

**Token lifetimes** (to confirm during implementation). This is why `refresh_session` is capability-gated rather than universal:

| Broker | Login flow | Refresh |
|---|---|---|
| Zerodha | OAuth-style redirect → `request_token` → access token (valid until about 6 AM the next day) | No refresh token for regular apps → re-login |
| Upstox | OAuth2 code → access token (daily expiry) | Re-login |
| Fyers | Auth code → access token + refresh token | Refresh with PIN |
| AngelOne | Client code + PIN + TOTP → JWT + refresh token | `generateTokens` |
| Groww | API key+secret or TOTP → access token | Re-login |
| Mock | Credentials plus a scenario config | Configurable (to test expiry) |

**Adding a 6th broker (e.g. Dhan)** — no changes to the engine, API or DB:
1. `app/brokers/dhan/adapter.py`: subclass `BrokerAdapter`, declare capabilities, implement the mappers.
2. Register it with the `@register_broker("dhan")` decorator, which the registry discovers automatically.
3. Add `DHAN_*` settings to config and `.env.example`.
4. Add fixture JSON files and add `"dhan"` to the parametrized **adapter contract test suite**. Passing that suite is the definition of done.

---

# STEP 5 — Build our own integrations or use a library?

**Options considered**

| Option | Assessment |
|---|---|
| **B1. OpenAlgo** (`marketcalls/openalgo`) | Active (commits within hours), 30+ brokers including all 5 of ours, normalized REST API. **But:** it's a *platform*, not a library: a Flask app with its own DB and UI, where broker login happens in *its* browser UI. It's built around a trader self-hosting their own instance, so our engine would become a client of a second service we'd have to run and secure. **AGPL-3.0**, which a startup needs to review legally before modifying or embedding it. It doesn't fit a multi-user backend engine. |
| **B2. Brokers' official SDKs** (`kiteconnect`, `fyers-apiv3`, `smartapi-python`, `upstox-python-sdk`, `growwapi`) | Maintained by the brokers themselves, so the most trustworthy source of auth and payload details. **But:** they're synchronous (would need `to_thread`), each has its own exception types, some pin conflicting dependencies, and they **hide the timeout semantics**. Hidden timeouts are a problem when "sent or not?" is the most important question in this system. |
| **A. Our own thin REST adapters on httpx** | Async, one transport, explicit connect vs read timeouts, easy to test with `respx` fixtures, no dependency conflicts. More code per broker (~150–200 lines), but only 6 methods each. |

**Recommendation: A, with our own interface either way.** Write thin httpx adapters, using the official SDKs and docs as the reference for payloads and checksums. If a broker's auth is painful to reimplement, wrapping its official SDK *inside that one adapter* is fine, because the interface doesn't change. Mention OpenAlgo in the README as the evaluated alternative, and as a possible `OpenAlgoAdapter` later for breadth (it plugs in like any 6th broker).

**Being honest about the lack of credentials:**
- Registered adapters: `zerodha`, `fyers`, `angelone`, `upstox`, `groww` (real, **unverified live**) and `mock` (full exchange simulator).
- `LIVE_TRADING_ENABLED=false` by default. Real adapters refuse `place_order` unless it's explicitly enabled. Read-only calls still work if someone supplies credentials.
- Real adapters are covered by **fixture-based contract tests** built from documented response formats. The README states: "built against published API docs; not verified against live accounts."
- The demo uses `mock`, labelled as a simulator.

---

# STEP 6 — Execution flow

**Synchronous part (inside `POST /executions`, typically under 1–2s):**
1. Authenticate `X-API-Key` → `owner_id`. Require `Idempotency-Key` → hash the canonical request body.
2. Look up `(owner_id, idempotency_key)`:
   - found with the same hash → return the existing execution (`200`, `Idempotent-Replayed: true`)
   - found with a different hash → `422 IDEMPOTENCY_KEY_REUSED`
3. Pydantic validation: shape, quantity > 0, duplicates, conflicts, empty lists.
4. Load the connection (scoped to the owner). If it isn't `ACTIVE` → `409 BROKER_REAUTH_REQUIRED`.
5. Pre-flight through the gateway: `validate_session` (refresh if possible), `resolve_symbol` for every leg, `get_holdings` → holdings checks (Step 2 #7 and #8) → `422` with per-leg details.
6. Planner builds the legs: `phase` (SELL=1, BUY=2), `seq`, side, qty, `client_order_id`.
7. **One transaction:** insert the execution (`ACCEPTED`) and all orders (`PENDING`).
   - unique-key race → reload and replay the existing execution
   - active-execution index conflict → `409 EXECUTION_IN_PROGRESS`
8. Schedule the runner task, tracked in a registry so shutdown is clean. Return **`202`** with `execution_id`, the planned orders and a status URL.

**Asynchronous part (ExecutionRunner):**

9. Execution → `RUNNING`.
10. **Phase SELL:** submit orders concurrently (semaphore ~5, rate limiter), then poll each until it reaches a final state or the monitoring timeout.
11. **Phase BUY:** same.
12. **Finalize:** compute the summary and final status, then commit.
13. **Notify:** log + webhook with retries; save `notification_status`.

**Per-order state machine** (every transition is committed *before* the next external call):

```
PENDING --(commit SUBMITTING)--> place_order()
   ack ------------------------> SUBMITTED(broker_order_id) --poll--> OPEN/PARTIALLY_FILLED
                                                                   \-> FILLED | REJECTED | CANCELLED
   not-placed error (429/connect/auth) --retry<=N--> ... else FAILED(reason)
   OrderRejected ----------------------------------> REJECTED(reason)
   AmbiguousSubmission --> RECONCILING --found tag--> SUBMITTED (adopt broker_order_id)
                                       --not found after grace--> UNKNOWN (never auto-resent)
Crash/restart: PENDING -> SKIPPED ; SUBMITTING -> RECONCILING ; SUBMITTED/OPEN -> resume polling
```

**Execution final status:**
- `COMPLETED`: all orders filled
- `PARTIALLY_COMPLETED`: some filled, or some still open
- `FAILED`: nothing filled
- `NEEDS_REVIEW`: any order `UNKNOWN`

**Sync vs async:** synchronous is simpler to demo but ties correctness to HTTP timeouts, and a 60s request invites client retries. **Recommend async (202 + polling + webhook)** using a plain asyncio task, with durability coming from the database rather than a task queue.

---

# STEP 7 — Edge cases

**I = implement in the MVP, D = document only**

| Area | Case | Handling | |
|---|---|---|---|
| Auth | Invalid credentials | `complete_login` → `InvalidCredentials` → `401`-style error code on the connection endpoint; nothing stored | I |
| | Expired token | Pre-flight `validate_session` → refresh or `409 BROKER_REAUTH_REQUIRED`; connection → `EXPIRED` | I |
| | Refresh fails | Same as expired; the execution is never created | I |
| | Token expires mid-run | Gateway refreshes once. If it fails, the order becomes `FAILED(AUTH_EXPIRED)` (not placed, safe to re-run); remaining orders fail the same way | I |
| | Broker down | `BrokerUnavailable` → backoff retries → `FAILED(BROKER_UNAVAILABLE)`. In pre-flight → `503` and no execution created | I |
| Input | Invalid symbol | Regex at the schema level plus `resolve_symbol` in pre-flight → 422 naming the leg | I |
| | Quantity 0 / negative / non-integer | Pydantic `conint(gt=0)`; ADJUST `delta != 0` | I |
| | Duplicate symbols in a list | 422 (never merged silently) | I |
| | Empty portfolio | 422 | I |
| | Invalid action / mode | Discriminated union on `mode` → 422 | I |
| | Same symbol in SELL and BUY / ADJUST | 422 `CONFLICTING_INSTRUCTIONS` | I |
| | SELL more than held / BUY_NEW already held / ADJUST on something not held | 422 after the holdings check | I |
| | Unsupported broker | 422 listing the supported brokers | I |
| | Malformed JSON | 422 in the standard envelope | I |
| | Fat-finger (huge quantity or too many legs) | Configurable caps (`MAX_QTY_PER_ORDER`, `MAX_ORDERS_PER_EXECUTION`) | I |
| Execution | Timeout after sending | `AmbiguousSubmission` → reconcile by tag | I |
| | Network failure before sending | Not placed → retry | I |
| | Rate limit | Token bucket + 429 backoff honouring `Retry-After` | I |
| | Rejected (funds, circuit limit, no DDPI) | `REJECTED` with the broker's message | I |
| | Partial fill | `PARTIALLY_FILLED` with filled quantity; not cancelled | I |
| | Pending past the monitoring window | Left `OPEN`, reported; the DAY order expires at the broker | I |
| | Cancelled by broker or exchange | `CANCELLED` with reason | I |
| | Mixed success and failure | `PARTIALLY_COMPLETED`; no rollback (documented as impossible) | I |
| | Unexpected response on `place_order` | Treated as ambiguous → reconcile | I |
| | Unexpected response on reads | Retry, then mark the order `UNKNOWN` | I |
| | Duplicate request / double click | Idempotency key + one-active-execution index | I |
| | Server crash mid-run | Recovery on startup (Step 6) | I |
| | Market closed | Broker rejects; passed through | D |
| | Stock at upper/lower circuit | Market order stays open or gets rejected; reported | D |
| Consistency | Knowing what was already sent | Order row reaches `SUBMITTING` before the call; `broker_order_id` is saved after | I |
| | DB says FAILED but the broker actually filled it | Only possible if we mislabel an ambiguous result, which the design forbids (ambiguous → `UNKNOWN`, not `FAILED`). `POST /executions/{id}/reconcile` re-checks against the broker; a daily order-book reconciliation job is future work | I (endpoint) / D (job) |
| | Trade succeeded, notification failed | `notification_status=FAILED`; execution status unchanged; resend endpoint | I |
| Notification | Webhook down or timing out | 3 attempts with exponential backoff, 5s timeout each | I |
| | Delivered twice | Stable `X-Event-Id` header for the consumer to dedupe; documented as at-least-once | I |
| | Malicious `callback_url` (SSRF) | https only outside dev; block private IP ranges | D (basic check: I) |

---

# STEP 8 — Idempotency and failure recovery

**Four identifiers, each with one job:**

| ID | Created by | Scope | Purpose |
|---|---|---|---|
| `Idempotency-Key` | Client (the UI generates one per "execution intent", reused on retry) | Request | Retries return the same execution. Unique on `(owner_id, idempotency_key)` plus `request_hash` |
| `execution_id` | Server (UUID) | Batch | Handle for status, logs and notifications |
| `client_order_id` | Server, stored **before** sending; ≤20 alphanumeric chars to fit the strictest broker tag limit (e.g. `KX` + 16 base32 chars) | One order leg | Sent to the broker as the order tag; the **reconciliation key** |
| `broker_order_id` | Broker | One order | Status polling; unique per broker |

**Layered protection:**
1. **Request level:** the unique constraint in Postgres is the lock, so no Redis lock is needed. Concurrent identical requests produce one successful insert; the loser reads and replays the winner's result.
2. **Account level:** a partial unique index on `executions(connection_id) WHERE status IN ('ACCEPTED','RUNNING')`. This blocks a second execution with a *different* key (a double click in a client that generates a new key per click).
3. **Order level:** the runner only calls `place_order` for orders in `PENDING`. A move to `SUBMITTING` is a conditional update (`WHERE status='PENDING'`), so two runners can never send the same order.

**Retry rule:** retry automatically **only** when we know the order wasn't placed (connection refused, 429, auth rejected before placement, 503 from a gateway before sending). Anything ambiguous goes to reconciliation.

**The hard case: we sent the order and it timed out.** Most Indian broker APIs **don't deduplicate by client tag**, so we can't simply resend with the same key the way Stripe allows. What we do instead:
1. The order row is already saved as `SUBMITTING` with `client_order_id` (write-before-send).
2. On `AmbiguousSubmission` → `RECONCILING`. Call `list_orders()` and look for `tag == client_order_id`, checking 3 times over about 5–10s to allow for broker lag.
3. **Found** → adopt `broker_order_id` and continue polling. No duplicate.
4. **Not found** → `UNKNOWN`. The execution becomes `NEEDS_REVIEW`, and the notification says so explicitly. **No automatic resend.** `POST /executions/{id}/reconcile` re-checks later; once it's confirmed absent, the order is marked `FAILED(NOT_PLACED_CONFIRMED)` and the user can re-run just that leg as a new execution.
5. If a broker lacks tag support (capability flag), fall back to matching on `(symbol, side, qty, placed_after=submitted_at)`. More than one match → `UNKNOWN`.

The principle for the README: **a missed trade can be re-run; a duplicate trade costs real money. When in doubt, stop and surface it.**

**Crash recovery:** on startup, `RecoveryService` scans executions in `RUNNING` state:
- `SUBMITTING` → reconcile
- `SUBMITTED` / `OPEN` → resume polling
- `PENDING` → `SKIPPED("server restarted before submission")`
- then finalize and notify

---

# STEP 9 — Data model (3 tables, plus 1 optional)

**`broker_connections`**
- `id` UUID PK, `owner_id`, `broker` (string, so new brokers need no migration), `broker_user_id`
- `status` (`ACTIVE` | `EXPIRED` | `REVOKED`)
- `access_token_enc`, `refresh_token_enc` (Fernet-encrypted), `token_expires_at`
- `config_enc` (e.g. the mock scenario)
- `created_at`, `updated_at`
- Unique `(owner_id, broker, broker_user_id)`

**`executions`**
- `id` UUID PK, `owner_id`, `connection_id` FK
- `idempotency_key`, `request_hash`, `mode` (`INITIAL` | `REBALANCE`), `request_payload` JSON
- `status` (`ACCEPTED` | `RUNNING` | `COMPLETED` | `PARTIALLY_COMPLETED` | `FAILED` | `NEEDS_REVIEW`)
- `summary` JSON (counts, lists)
- `callback_url`, `notification_status` (`PENDING` | `SENT` | `FAILED` | `SKIPPED`), `notification_attempts`, `notified_at`
- `error_code`, `created_at`, `started_at`, `finished_at`
- Unique `(owner_id, idempotency_key)`; partial unique `(connection_id)` for active statuses

**`orders`**
- `id` UUID PK, `execution_id` FK, `seq`, `phase` (1 SELL / 2 BUY)
- `instruction_type` (`INITIAL_BUY` | `SELL` | `BUY_NEW` | `ADJUST`)
- `symbol`, `exchange`, `side`, `quantity`, `order_type`, `product`
- `client_order_id` (unique), `broker_order_id` (nullable)
- `status` (from Step 6), `filled_quantity`, `average_price`, `attempts`
- `error_code`, `error_message`, `broker_status_raw`
- `submitted_at`, `completed_at`, `created_at`, `updated_at`
- Unique `(execution_id, seq)`

**`order_events`** *(SHOULD)*: `id`, `order_id`, `from_status`, `to_status`, `detail` JSON, `created_at`. An append-only audit trail, cheap and very useful for a trading post-mortem.

No `users` table (owners come from API keys in config) and no `notifications` table (the fields on `executions` are enough).

---

# STEP 10 — API design

All errors use one envelope: `{"error": {"code": "...", "message": "...", "details": [...]}}`. Every route except `/health` requires `X-API-Key`.

| Method & URL | Purpose | Request | Response | Key errors |
|---|---|---|---|---|
| `GET /health` | Liveness + DB check | — | `{status, db}` | 503 |
| `GET /brokers` | Supported brokers and capabilities | — | `[{name, auth_flow, supports_refresh, live_enabled}]` | — |
| `POST /broker-connections` | Start or complete a connection | `{broker, credentials?: {...}}` (mock: `{client_id, scenario}`) | `201 {connection_id, status:"ACTIVE"}` or `200 {status:"PENDING_LOGIN", login_url}` | 422 unsupported broker, 401 `INVALID_BROKER_CREDENTIALS`, 503 broker down |
| `GET /broker-connections/{broker}/callback` | OAuth redirect target (`code` / `request_token`, `state`) | query | connection status | 400 bad state |
| `GET /broker-connections/{id}` | Status (never tokens) | — | `{id, broker, status, expires_at}` | 404 |
| `GET /broker-connections/{id}/holdings` | Normalized holdings (handy for the demo) | — | `[{symbol, quantity, avg_price}]` | 409 reauth |
| `DELETE /broker-connections/{id}` *(SHOULD)* | Disconnect and wipe tokens | — | 204 | 404 |
| `POST /executions/preview` *(SHOULD)* | Validate and plan without trading | same body as below | `{valid, orders:[...], errors:[...]}` | 422 |
| `POST /executions` | Execute | Header `Idempotency-Key`. Body `{connection_id, mode:"INITIAL", target:[{symbol,quantity}], callback_url?}` or `{connection_id, mode:"REBALANCE", sell:[{symbol,quantity}], buy:[{symbol,quantity}], adjust:[{symbol,delta}], callback_url?}` | `202 {execution_id, status:"ACCEPTED", orders:[{seq,phase,side,symbol,quantity,client_order_id,status}], status_url}`; replay → `200` + `Idempotent-Replayed: true` | 400 missing key, 404 connection, 409 `EXECUTION_IN_PROGRESS` / `BROKER_REAUTH_REQUIRED`, 422 validation / `IDEMPOTENCY_KEY_REUSED` |
| `GET /executions/{id}` | Full status | — | execution + orders (status, filled qty, broker IDs, errors) + summary + notification status | 404 |
| `GET /executions` *(SHOULD)* | List (filters: `connection_id`, `status`) | — | paginated list | — |
| `POST /executions/{id}/reconcile` *(SHOULD)* | Re-check `UNKNOWN` / `OPEN` orders against the broker | — | updated execution | 409 still running |
| `POST /executions/{id}/notify` *(SHOULD)* | Resend the notification | — | `{notification_status}` | 409 not finished |
| `POST /dev/webhook-sink`, `GET /dev/webhook-sink` | Dev-only receiver that records webhooks, for the demo | any | stored events | disabled outside dev |

**Webhook payload:** `{event:"execution.finished", event_id, execution_id, status, summary:{total, filled, partially_filled, rejected, failed, unknown, skipped}, orders:[{symbol, side, quantity, filled_quantity, status, broker_order_id, error_code, error_message}]}`. Headers: `X-Event-Id`, `X-Signature: sha256=HMAC(body)`.

---

# STEP 11 — Project structure

```
kalpi-execution-engine/
├── app/
│   ├── main.py                 # app factory, lifespan (DB init, recovery, runner registry)
│   ├── core/                   # config (pydantic-settings), logging (structlog), security
│   │                           #   (API key dep, Fernet vault), errors (envelope + handlers)
│   ├── api/                    # routers: brokers.py, connections.py, executions.py, dev.py; deps.py
│   ├── schemas/                # Pydantic request/response models (API contract only)
│   ├── domain/                 # canonical models + enums (OrderRequest, OrderSnapshot, OrderState…),
│   │                           #   broker error taxonomy — no FastAPI, no SQLAlchemy imports
│   ├── execution/              # validator.py, planner.py (pure), service.py (ExecutionService),
│   │                           #   runner.py (state machine), reconciler.py, recovery.py
│   ├── brokers/
│   │   ├── base.py             # BrokerAdapter ABC, capabilities
│   │   ├── registry.py         # @register_broker, factory
│   │   ├── gateway.py          # rate limit / retry / timeout / refresh / logging wrapper
│   │   ├── ratelimit.py
│   │   ├── instruments/        # small static symbol maps (ISIN, Angel tokens) for supported symbols
│   │   ├── zerodha/ fyers/ angelone/ upstox/ groww/   # adapter.py + mappers.py each
│   │   └── mock/               # simulator: in-memory holdings + order book + scenarios
│   ├── connections/            # ConnectionService
│   ├── notifications/          # base.py, log.py, webhook.py, service.py
│   └── db/                     # SQLAlchemy models, session, repositories
├── tests/ unit/ adapters/ (fixtures/*.json + contract suite) engine/ api/ integration/
├── frontend/index.html         # optional single-file UI served by FastAPI at /ui
├── scripts/demo.sh             # curl walkthrough, doubles as the smoke test
├── Dockerfile  docker-compose.yml  .env.example  pyproject.toml  README.md
```

The rule behind this layout: `domain/` and the pure `execution/validator` and `planner` import nothing from FastAPI, SQLAlchemy or the brokers. That keeps the core testable and stops broker details from leaking in.

---

# STEP 12 — Testing strategy

**Tools:** pytest, pytest-asyncio, `httpx.AsyncClient` with `ASGITransport` for API tests, `respx` for mocking broker HTTP, SQLite in-memory for fast tests, and an **injectable clock/sleeper** so polling and backoff tests run instantly.

| Layer | What we test |
|---|---|
| Unit | Validator (parametrized invalid payloads), planner (INITIAL → buys; REBALANCE → sells in phase 1, ADJUST split by sign), `client_order_id` format and length, execution-status aggregation, error classification |
| **Adapter contract suite** (parametrized over every adapter) | Using respx + fixture JSON copied from the docs: login exchange builds the right checksum and body; `place_order` sends the right payload **including the tag**; 429 → `BrokerRateLimited`; `ConnectTimeout` → `BrokerUnavailable`; `ReadTimeout` → `AmbiguousSubmission`; 401/403 → `ReauthRequired`; status mapping table; holdings mapping |
| Engine (with MockBroker) | Scenarios: `SUCCESS`, `REJECTED`, `TIMEOUT_AFTER_PLACE` (reconcile finds it → filled; **assert the mock order book has exactly 1 order**), `TIMEOUT_NOT_PLACED` (→ `UNKNOWN`, **0 resends**), `RATE_LIMITED` (N×429 then success; check attempts), `PARTIAL_FILL`, `PENDING` (monitoring timeout → `OPEN`), `AUTH_EXPIRED` mid-run, `BROKER_DOWN`, mixed → `PARTIALLY_COMPLETED`, sells finish before buys start |
| Idempotency | Same key replayed → same `execution_id` and the broker saw each order once; same key with a different body → 422; `asyncio.gather` of 5 identical requests → 1 execution; different key while running → 409 |
| Recovery | Seed the DB with `SUBMITTING`, `SUBMITTED` and `PENDING` orders, run recovery → reconciled, resumed, skipped |
| Notifications | Webhook 500 ×3 → `FAILED` with the execution status unchanged; HMAC header verifies; stable `event_id` |
| API | Status codes, error envelope, missing idempotency key, API-key auth, tokens never appear in any response |
| Integration | `docker compose up` + `scripts/demo.sh` against Postgres (catches differences between SQLite and Postgres, e.g. the partial index) |

**MockBroker design:** it keeps per-connection in-memory holdings and an order book (so holdings checks and tag reconciliation behave realistically). The scenario is set at connection time: `{"default": "SUCCESS", "symbols": {"TCS": "REJECTED", "INFY": "PARTIAL_FILL", "ITC": "TIMEOUT_AFTER_PLACE"}, "rate_limit_first_n": 2}`, deterministic and seeded. The demo is scriptable with no code changes.

---

# STEP 13 — Docker

**Dockerfile:** `python:3.12-slim`, dependencies installed in their own layer (cache-friendly), non-root user, `HEALTHCHECK` on `/health`, `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`.

The **single worker is deliberate:** the rate limiter and runner live in memory. Scaling out would need Redis or a queue, which is documented as future work.

**docker-compose.yml:**
- `api`: build `.`, `env_file: .env`, port 8000, `depends_on: db (service_healthy)`
- `db`: `postgres:16-alpine`, `pg_isready` healthcheck, named volume

| Component | Needed? | Reason |
|---|---|---|
| FastAPI | Yes | Required |
| PostgreSQL | **Yes** | Idempotency and crash recovery need durable state and real unique constraints. SQLite is used for tests only. |
| Redis | **No** | Idempotency comes from DB constraints; rate limits are per-process with one worker. Needed only to scale horizontally. |
| Celery / worker | **No** | An asyncio task is enough for I/O-bound order batches; durability comes from DB state plus startup recovery. |
| Nginx / Kafka / anything else | **No** | — |

Tables are created with `create_all` at startup in the MVP; Alembic is a SHOULD.

---

# STEP 14 — Security (minimum for this scope)

- **Broker app secrets** (api_key and secret for Zerodha, Upstox, etc.) live only in environment variables; `.env.example` holds placeholders, `.env` is git-ignored.
- **User tokens** are Fernet-encrypted at rest (`TOKEN_ENCRYPTION_KEY` env), decrypted only inside the gateway, and **never returned by any endpoint**. The frontend only ever sees `connection_id`.
- **Login credentials** (TOTP, PIN, password for credential-based brokers) are used once for the exchange and **never stored**.
- **API auth:** `X-API-Key` → owner, and every query is scoped by `owner_id`.
- **Safety rails:** `LIVE_TRADING_ENABLED=false` by default, caps on quantity and number of orders, OAuth `state` parameter checked on callback.
- **Logs:** a structlog processor redacts `token`, `secret`, `password`, `pin`, `totp`, `authorization`, `api_key` keys, and the httpx logger is configured not to log headers.
- **Webhooks:** HMAC-signed; `callback_url` must be https outside dev, with private IP ranges blocked (basic SSRF protection).
- **HTTPS** is assumed to terminate at a load balancer or ingress (documented).
- **CORS** is limited to the UI origin.
- **Deployment note:** SEBI requires API orders to come from the whitelisted static IP, so production needs a fixed egress IP (NAT gateway).

---

# STEP 15 — Observability

JSON logs through structlog, with context variables so every line automatically carries `request_id`, `owner_id`, `execution_id`, `connection_id`, `broker`, and `order_id` / `client_order_id` / `broker_order_id` when relevant.

**Events** (fields: `symbol`, `side`, `quantity`, `status`, `error_code`, `attempt`, `latency_ms`, `timestamp`):
- `execution.accepted`, `execution.replayed`, `execution.rejected_validation`
- `order.submit.started`, `order.submit.ack`, `order.submit.ambiguous`, `order.reconcile.found` / `not_found`, `order.status.changed`
- `broker.call` (method, status, latency), `broker.rate_limited`, `broker.auth.refreshed`, `broker.auth.expired`
- `execution.finished`, `notification.sent` / `failed`, `recovery.resumed`

**Never logged:** access or refresh tokens, API secrets, PIN/TOTP/password, `Authorization` headers, full raw broker profile responses (these contain PAN, email, phone), webhook secrets.

**Debugging example:** a user reports "INFY didn't buy." Filter logs by `execution_id` → `order.submit.ambiguous` (read timeout, 10.0s) → `order.reconcile.found broker_order_id=…` → `order.status.changed REJECTED error_code=INSUFFICIENT_FUNDS`. In three lines we know it was placed exactly once and the broker rejected it for funds. The `order_events` table gives the same trail from the DB.

---

# STEP 16 — Scope for 24 hours

**MUST HAVE**
- Config, logging, DB models, compose skeleton
- Schemas + validator + planner (pure, unit-tested)
- Adapter interface, error taxonomy, registry, gateway (rate limit, safe retry, refresh), MockBroker with scenarios
- Runner (two phases, state machine, reconciliation, polling, finalize), idempotency + active-execution guard, startup recovery
- Connection + execution endpoints
- Notifications (log + signed webhook with retries + dev sink)
- 5 real adapters (Zerodha, Fyers, AngelOne, Upstox, Groww) limited to the interface methods, with contract tests
- Engine / idempotency / API tests
- Dockerfile + compose, README, `demo.sh`

**SHOULD HAVE**
- `/executions/preview`, `/reconcile`, `/notify` resend, `GET /executions` list
- `order_events` audit table, Alembic, GitHub Actions running pytest

**NICE TO HAVE**
- Single-file HTML UI (≤1.5h, vanilla JS, served at `/ui`)
- `OpenAlgoAdapter`
- SSE live status, Prometheus metrics, `sell_failure_policy`

**Timeline**

| Hours | Work |
|---|---|
| 0–1 | Repo skeleton, settings, structlog, SQLAlchemy models, compose with Postgres |
| 1–3 | Schemas, validator, planner + unit tests |
| 3–5.5 | Domain models, error taxonomy, adapter ABC, registry, gateway, MockBroker + tests |
| 5.5–9.5 | ExecutionService (idempotency, pre-flight, transactional plan), runner, reconciler, recovery, endpoints + engine and idempotency tests. **End-to-end demo working on the mock by ~hour 10.** |
| 9.5–10.5 | Notifications + dev webhook sink + tests |
| 10.5–16 | Real adapters, ~1h each + contract fixtures (Zerodha → Upstox → Fyers → AngelOne → Groww) |
| 16–17 | Docker polish, `demo.sh` smoke test on Postgres |
| 17–19 | README + diagrams |
| 19–21 | SHOULD items; the UI only if everything else is green |
| 21–24 | Buffer: full test run, clean clone → `docker compose up` test, record demo |

---

# STEP 17 — Demo (about 4 minutes)

1. **(0:00)** `docker compose up`, open `/docs`, and show `GET /brokers`: 5 real adapters + mock, with the live-trading switch off.
2. **(0:30)** `POST /broker-connections` with the `mock` broker and scenario `{default: SUCCESS}`; empty holdings.
3. **(1:00)** `POST /executions` INITIAL (RELIANCE 10, TCS 5, INFY 8) → 202 → `GET` shows 3 filled → `GET /dev/webhook-sink` shows the signed summary.
4. **(1:45)** **Replay the same request with the same `Idempotency-Key`** → 200, same `execution_id`, `Idempotent-Replayed: true`, and holdings still show 10/5/8.
5. **(2:15)** Reconnect with scenario `{HDFCBANK: REJECTED, ITC: TIMEOUT_AFTER_PLACE, rate_limit_first_n: 2}`. Submit a REBALANCE: sell INFY 8, buy HDFCBANK 4 and ITC 20, adjust TCS −2 and RELIANCE +5.
6. **(3:00)** The result shows sells ran before buys, the HDFCBANK rejection with reason, ITC **reconciled after the timeout with exactly one broker order**, 429 retries visible in logs → `PARTIALLY_COMPLETED` plus the webhook.
7. **(3:30)** Send an invalid payload (duplicate symbol, SELL more than held) → 422 with per-leg errors; a second execution while one is running → 409.
8. **(3:50)** `pytest -q` summary; point to the README trade-offs section.

---

# STEP 18 — README structure

1. Title + one-paragraph summary + the **"Simulator vs live"** disclaimer
2. Quick start (3 commands) + demo script
3. Problem statement (brief)
4. Architecture: diagram, component table, why a single process with no queue
5. Execution flow: lifecycle, order state machine, two-phase SELL → BUY
6. Rebalance logic: payload semantics, validation rules, ADJUST mapping, what the engine deliberately doesn't do (delta calculation)
7. Broker adapter design: interface, canonical errors, capability flags, **adding a 6th broker in 4 steps**
8. Library evaluation: OpenAlgo vs official SDKs vs own httpx adapters, and why
9. Idempotency and failure recovery: the ambiguous-timeout case, reconciliation, crash recovery
10. Failure handling matrix (condensed edge-case table)
11. API reference (table + curl examples; Swagger link)
12. Notifications: payload, HMAC verification, retry semantics
13. Configuration (env var table)
14. Docker (commands, services, why no Redis/Celery)
15. Testing (how to run; the mock scenarios; contract suite)
16. Security and observability
17. Assumptions
18. Trade-offs
19. Limitations and future improvements (live verification, instrument-master sync, horizontal scaling with Redis, scheduled reconciliation job, AMO, limit orders, OpenAlgo adapter)

---

# STEP 19 — Engineering trade-offs

| Trade-off | Recommendation | Why |
|---|---|---|
| Sync vs async | **Async: 202 + polling + webhook** | Order monitoring outlasts HTTP timeouts, and long requests invite retries |
| Task queue vs in-process | **In-process asyncio + DB state + startup recovery** | Durability comes from the DB; Celery adds a broker and worker for no correctness gain at this scale |
| Postgres vs no DB | **Postgres** (SQLite in tests) | Idempotency, one active execution per account and recovery all need durable, constrained state |
| Redis vs none | **None** | DB constraints give idempotency; one worker gives correct in-memory rate limits. Documented as the step for horizontal scaling. |
| Own adapters vs library | **Own interface + thin httpx adapters** (SDK inside an adapter if needed); OpenAlgo documented as an option | Control over timeouts and errors is the core of trade safety; OpenAlgo is an AGPL platform, not a library |
| Real vs mock brokers | **Both, labelled honestly**; demo on the mock; live placement off by default | Can't verify live without accounts, and faking it would be worse than admitting it |
| Auto-retry vs manual reconciliation | **Auto-retry only when "not placed" is certain; otherwise reconcile, then `UNKNOWN`** | Duplicate trades are worse than missed ones |
| Atomic batch vs best effort | **Best effort per order + clear reporting** | Trades can't be rolled back; compensating trades are a human decision |
| Strict vs lenient validation | **Strict (fail closed)** | A rebalance built on a stale view of holdings is dangerous |
| Auto-cancel open orders vs leave | **Leave; report** | Cancelling is a trading decision; DAY validity limits the exposure |
| Monolith vs services | **Modular monolith** | Clear internal boundaries (domain, adapters, gateway, runner) without the cost of running separate services |

---

# Final summary

**1. Recommended architecture.** A modular FastAPI app (one process) backed by PostgreSQL. `POST /executions` authenticates the caller, requires an `Idempotency-Key`, validates the payload against live holdings, saves an execution and its orders in one transaction, and returns `202`. An in-process runner executes sells, then buys, through a `BrokerGateway` that centralizes rate limiting, safe-only retries, timeouts and token refresh on top of thin broker adapters (Zerodha, Fyers, AngelOne, Upstox, Groww, plus a scenario-driven Mock). Every order moves through a saved state machine and carries our `client_order_id` as the broker tag, so ambiguous timeouts are resolved by checking the order book, never by resending. Results go out through a log entry and a signed, retried webhook. No Redis, Celery or queue: DB constraints and write-before-send state changes provide idempotency and crash recovery.

**2. Diagram:** Step 3. **3. Components:** Step 3 table. **4. Data model:** `broker_connections`, `executions`, `orders` (+ optional `order_events`), Step 9. **5. API:** Step 10. **6. Execution flow:** synchronous validate-and-persist → async two-phase run → finalize → notify, Step 6. **7. Failure strategy:** retry only when "not placed" is certain; ambiguous → reconcile by tag → `UNKNOWN`/`NEEDS_REVIEW`; no rollback; notifications separate from the result (Steps 7–8). **8. Testing:** pure unit tests + parametrized adapter contract suite with respx fixtures + MockBroker scenario tests that count broker-side orders to prove no duplicates, plus idempotency concurrency, recovery and webhook tests (Step 12). **9. Docker:** `api` + `postgres` only, one uvicorn worker by design (Step 13). **10. Plan:** core engine end-to-end on the mock by ~hour 10, real adapters hours 10–16, docs and demo after, UI only if time remains (Step 16). **11. Repo structure:** Step 11. **12. Key assumptions:** MARKET / CNC / NSE / DAY, sells before buys, strict holdings validation, mandatory idempotency key, one active execution per broker account, no automatic resend after an ambiguous outcome, no auto-cancel, live trading disabled by default (Steps 2 and 19).
