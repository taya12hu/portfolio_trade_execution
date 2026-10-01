# TODO — Portfolio Trade Execution Engine

Design: [`docs/PLAN.md`](docs/PLAN.md). Status legend: `[x]` done · `[~]` in progress · `[ ]` not started · `[-]` dropped (see decisions log).

**Priority rule:** a fully working, well-tested end-to-end flow on the **MockBroker** comes first (Phases 0–7).
Real broker adapters (Phase 8) are added one at a time, each checked against the broker's published docs
and covered by contract tests. We don't add a broker just to say five are supported.

---

## Phase 0 — Skeleton and tooling
- [x] `pyproject.toml` (deps + pytest config), `.gitignore`, `.env.example`
- [x] `app/core/config.py` — pydantic-settings, API keys → owners, limits, `LIVE_TRADING_ENABLED=false`
- [x] `app/core/logging.py` — structlog JSON, contextvars, secret redaction
- [x] `app/core/errors.py` — `ApiError` + one error envelope for all handlers
- [x] `app/core/security.py` — `X-API-Key` dependency, Fernet `TokenVault`
- [x] Local venv + `pytest` running green (100 tests, ~16s)

## Phase 1 — Pure core (no I/O)
- [x] `app/domain/models.py` — enums (`Side`, `OrderState`, `OrderStatus`, `ExecutionStatus`, `InstructionType`, `Mode`), canonical dataclasses
- [x] `app/domain/errors.py` — broker error taxonomy with "placed?" semantics
- [x] `app/schemas/` — request/response models; `mode` discriminated union; strict positive int quantities
- [x] `app/execution/validator.py` — duplicates, conflicts, caps, empty lists; holdings checks (INITIAL already-held, SELL > held, BUY_NEW held, ADJUST not held / oversell)
- [x] `app/execution/planner.py` — legs with phase (SELL=1, BUY=2), seq, `client_order_id` (`KX` + 16 base32)
- [x] `app/execution/status.py` — final-status aggregation + summary
- [x] Unit tests: validator (parametrized), planner, client_order_id, aggregation

## Phase 2 — Broker layer
- [x] `app/brokers/base.py` — `BrokerAdapter` ABC + `BrokerCapabilities`
- [x] `app/brokers/registry.py` — `@register_broker`, factory, `supported_brokers()`
- [x] `app/brokers/ratelimit.py` — async token bucket per (connection, category)
- [x] `app/brokers/gateway.py` — rate limit, timeouts, safe-only retries, single refresh-and-retry, live-trading guard, call logging
- [x] `app/brokers/mock/` — in-memory exchange (holdings + order book per client), scenarios: SUCCESS, REJECTED, REJECTED_ON_PLACE, PARTIAL_FILL, PENDING, CANCELLED, TIMEOUT_AFTER_PLACE, TIMEOUT_NOT_PLACED, BROKER_DOWN, AUTH_EXPIRED, `rate_limit_first_n`, `session_expired`, `funds`, `initial_holdings`
- [x] Unit tests: rate limiter, gateway retry classification (ambiguous is never retried), mock scenarios

## Phase 3 — Persistence and connections
- [x] `app/db/models.py` — `broker_connections`, `executions`, `orders`, `order_events`; unique `(owner_id, idempotency_key)`; partial unique active-execution index; unique `client_order_id`
- [x] `app/db/session.py` — async engine/sessionmaker (Postgres in Docker, SQLite for tests/dev)
- [x] `app/connections/service.py` — credential login, redirect login (`state` check), upsert by `(owner, broker, broker_user_id)`, encrypted tokens, refresh persistence, mark `EXPIRED`
- [x] Tests: tokens are encrypted at rest and never in responses

## Phase 4 — Execution engine
- [x] `app/execution/service.py` — idempotency lookup + request hash, owner-scoped connection, pre-flight (session, symbols, holdings), one-transaction insert, race handling (replay / 409), schedule runner
- [x] `app/execution/runner.py` — two phases; conditional `PENDING→SUBMITTING`; submit with semaphore; ambiguous → reconcile by tag → `UNKNOWN`; batch polling via order book; bounded monitor window; fail-fast after auth expiry; finalize; notify
- [x] `app/execution/recovery.py` — on startup: PENDING→SKIPPED, SUBMITTING/RECONCILING→reconcile, SUBMITTED/OPEN→resume polling, then finalize + notify
- [x] Manual reconcile (`UNKNOWN` → found / `FAILED(NOT_PLACED_CONFIRMED)` after a grace period; refresh open orders)
- [x] Task registry for clean shutdown

## Phase 5 — API and notifications
- [x] Routers: `/health`, `/brokers`, `/broker-connections` (+ callback, get, list, holdings, delete), `/executions` (+ preview, get, list, reconcile, notify), `/dev/webhook-sink`
- [x] Request-id middleware; error envelope on every error path (incl. malformed JSON)
- [x] `app/notifications/` — log notifier + webhook (HMAC `X-Signature`, stable `X-Event-Id`, 3 attempts with backoff, 5s timeout), `notification_status` stored separately from the execution result
- [x] `callback_url` checks: https + no private IPs outside dev

## Phase 6 — End-to-end tests on the mock (milestone)
- [x] Happy path INITIAL → COMPLETED + webhook received + signature verifies
- [x] REBALANCE: sells all finish before the first buy is placed
- [x] TIMEOUT_AFTER_PLACE → reconciled, **mock order book has exactly 1 order**
- [x] TIMEOUT_NOT_PLACED → UNKNOWN, **0 resends**, execution NEEDS_REVIEW
- [x] Rate limited N times → filled, attempts counted
- [x] Partial fill / pending past the window → reported, not cancelled
- [x] Auth expired mid-run → remaining orders FAILED(AUTH_EXPIRED) without broker calls; connection EXPIRED
- [x] Broker down → FAILED(BROKER_UNAVAILABLE); pre-flight broker down → 503, no execution
- [x] Idempotency: replay → same id + `Idempotent-Replayed`; changed body → 422; 5 concurrent identical → 1 execution; different key while running → 409
- [x] Recovery: seeded SUBMITTING / SUBMITTED / PENDING orders → reconciled / resumed / skipped
- [x] Webhook 500×3 → notification FAILED, execution status unchanged; resend endpoint
- [x] API: auth, missing idempotency key, envelope, owner scoping, no tokens in any response

## Phase 7 — Packaging and docs
- [x] `Dockerfile` (3.12-slim, non-root, healthcheck, 1 worker) + `docker-compose.yml` (api + postgres 16)
- [x] `scripts/demo.sh` — curl walkthrough of the PLAN Step 17 demo; **passes against a local server** (SQLite)
- [x] `README.md` per PLAN Step 18, including the "simulator vs live" disclaimer
- [ ] Verify `docker compose up` + `demo.sh` on a machine with Docker (Docker is not installed on the dev machine)

## Phase 8 — Real broker adapters (one at a time, each checked against docs)
Each one: payload/endpoint check against current docs → adapter + mappers → respx contract tests.
Live order placement stays blocked unless `LIVE_TRADING_ENABLED=true`.
- [x] Shared: transport error classification (`app/brokers/http.py`) — connect failures = not sent; anything after send = ambiguous
- [x] Shared: instrument map with provenance; unverified entries refused when live; `scripts/refresh_instruments.py`
- [x] Shared: parametrized adapter contract suite (`tests/adapters/test_contract.py`, 21 cases × 5 brokers)
- [x] Zerodha (Kite Connect v3) — checked against kite.trade docs (orders, user, portfolio, exceptions)
- [x] Upstox (v2 auth/reads, v3 place) — checked against upstox.com developer docs
- [x] Fyers (v3) — checked against the official `fyers-apiv3` SDK source + FYERS community docs (docs site not fetchable)
- [x] AngelOne (SmartAPI) — checked against smartapi.angelbroking.com docs + official SDK source
- [x] Groww (Trade API) — checked against groww.in trade-api docs + official `growwapi` SDK source
- [x] Full live-mode rebalance through the real Zerodha adapter (mocked Kite HTTP) incl. lost response → reconciled
- [x] Ran `scripts/refresh_instruments.py`: 2,670 NSE equities from the Upstox master; all 2,668 overlapping AngelOne tokens agree
- [ ] Verify each adapter against a real account (needs credentials, API subscriptions and a static IP)

## Phase 9 — SHOULD
- [ ] Alembic migrations (replace `create_all`)
- [x] GitHub Actions: pytest on SQLite, engine/API tests on Postgres 16, docker build (`.github/workflows/ci.yml`)
- [~] Postgres-backed test run — wired via `TEST_DATABASE_URL`; runs in CI, not run locally (no Postgres/Docker here)

## Phase 10 — NICE TO HAVE
- [x] Single-file HTML UI at `/ui` (connect → paste/upload CSV → preview → execute → live results, idempotent re-click); checked in a browser
- [ ] `OpenAlgoAdapter`
- [ ] SSE live status, Prometheus metrics, `sell_failure_policy`

---

## Decisions log
Implementation choices that refine or deviate from the plan.

| Date | Decision | Why |
|---|---|---|
| 2026-10-01 | Order monitoring polls the **order book once per tick for the whole phase** (`list_orders`) instead of `get_order` per order | One read per poll interval regardless of batch size; stays well inside broker read limits. `get_order` remains in the interface. |
| 2026-10-01 | Added `PENDING_LOGIN` connection status + `oauth_state` column | Redirect brokers need a pending row to tie the callback's `state` back to an owner. |
| 2026-10-01 | OAuth callback route is not API-key protected | It is a browser redirect from the broker; the single-use `state` authenticates it. |
| 2026-10-01 | Mock token expiry is modelled as `session_expired` / `expire_session_after_orders` instead of a per-symbol `AUTH_EXPIRED` | Expiry is a property of the session, not of a symbol; this also exercises the refresh path realistically. |
| 2026-10-01 | `X-Event-Id` is derived from the execution outcome (not just the execution id) | Retries/resends of the same outcome dedupe; an outcome changed by reconcile gets a new id instead of being silently dropped by the consumer. |
| 2026-10-01 | Manual reconcile marks `UNKNOWN` → `FAILED(NOT_PLACED_CONFIRMED)` only after `RECONCILE_CONFIRM_AFTER_S` (120s) | Absence from the book seconds after a timeout is not proof (broker lag). |
| 2026-10-01 | Pre-flight rejects real brokers with `403 LIVE_TRADING_DISABLED` before anything is created | Better than creating an execution whose every order fails. The gateway also enforces it per order (defence in depth). |
| 2026-10-01 | Unexpected exceptions inside the run → fail-safe: PENDING→SKIPPED, SUBMITTING/RECONCILING→UNKNOWN, finalize | An internal bug must never leave the account locked in RUNNING or claim an uncertain order failed. |
| 2026-10-01 | Found + fixed: concurrent identical requests could get 409 instead of a replay | The busy-account check ran before re-checking the idempotency key. Now any conflict re-checks the key first (`_replay_or_conflict`). |
| 2026-10-01 | Shared SSL context for all httpx clients | Building one costs ~0.35s on Windows; per-client creation made app startup and the test suite 4x slower. |
| 2026-10-01 | Found + fixed: an undecryptable stored session (e.g. `TOKEN_ENCRYPTION_KEY` changed) returned 500 | Now treated as an expired session: `409 BROKER_REAUTH_REQUIRED` + connection `EXPIRED`; reconnecting fixes it. Found by killing and restarting the server mid-execution. |
| 2026-10-01 | Any 5xx / unparseable / id-less reply to a **write** is `AmbiguousSubmission` for every broker | e.g. Kite documents 502 `NetworkException` as "API unable to communicate with the OMS" — the order may exist. |
| 2026-10-01 | AngelOne: HTTP 403 without a token error code = rate limit | SmartAPI documents rate-limit rejections as 403; treating them as auth failures would wrongly expire the connection. |
| 2026-10-01 | AngelOne `status` compared explicitly against true | Documented failure envelope uses the string `"false"`, which is truthy in Python. |
| 2026-10-01 | Fyers `supports_refresh=False` | Fyers refresh needs the user's PIN, which we never store. |
| 2026-10-01 | Upstox: v3 place endpoint with `slice=false`; more than one returned order id → ambiguous | v2 place is deprecated; a sliced order must surface as several broker orders for review, not be silently tracked as one. |
| 2026-10-01 | Instrument identifiers carry provenance; unverified ones are refused when live | A wrong ISIN/token trades the wrong stock. Only Upstox and AngelOne need them. |
| 2026-10-01 | No `scripconsent` sent to AngelOne | Consenting to trade surveillance-list stocks is the user's decision, not the engine's. |
| 2026-10-01 | Fyers order-book tag compared after the last `:` | Defensive against a `"<n>:<tag>"` echo format; unverified, flagged in the adapter docstring. |
| 2026-10-01 | Instrument map regenerated from official masters | It caught a wrong hand-curated ISIN (KOTAKBANK `INE237A01028` → `INE237A01036`, changed by a stock split). Confirms the rule: never trust hand-written identifiers for live orders. |
