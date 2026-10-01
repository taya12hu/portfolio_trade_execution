# How to verify the project

Five ways to check it, from fastest to most thorough. Commands run from the repo root on Windows (Git Bash or PowerShell). On macOS or Linux, use `.venv/bin/` instead of `.venv/Scripts/`.

## 1. Automated tests (2 minutes)

```bash
.venv/Scripts/python -m pytest
```

Expect `230 passed`. Where the tests run:
- **Without `TEST_DATABASE_URL` in `.env`:** on SQLite, in about 15 seconds.
- **With `TEST_DATABASE_URL` set (e.g. Supabase):** the engine and API tests run against that Postgres. Expect about 20 seconds per test, because every query crosses the internet (about 165 ms per round trip). That's network distance, not a bug. **These tests drop every table in that database.**

| Folder | What it proves |
|---|---|
| `tests/unit` | validation rules, sells-before-buys planning, retry rules (an ambiguous order is never retried) |
| `tests/adapters` | all five real broker adapters behave the same way (payloads, tags, errors, statuses) |
| `tests/engine` | the full flow on the mock broker: lost responses, duplicates, 429s, partial fills, crash recovery, webhooks |
| `tests/api` | authentication, error format, one user can't see another's data, tokens never leak |

## 2. Docker + demo script (3 minutes)

```bash
docker compose up --build -d
docker compose ps
bash scripts/demo.sh
```

After `docker compose ps`, both containers should show `(healthy)`.

The demo script ends with `Demo finished: all expectations met` and stops with an error if anything is wrong. It walks through:
1. Supported brokers (the mock is live; the five real brokers have live trading off).
2. A first-time portfolio: 3 buys, all `FILLED`, with a signed webhook (`signature_valid=True`).
3. The same request sent again: `200`, the same execution, nothing traded twice.
4. A rebalance with problems injected: the sells run first, HDFCBANK is rejected with a reason, ITC's response is lost but the order is found by its tag, and the 429s are retried (`attempts 2`).
5. Bad requests: `422` with a reason per order, and `409` for a second execution while one is running.

## 3. Click through the UI

Open <http://localhost:8000/ui> (API key `dev-key`):
1. **Connect:** broker `mock`, client id e.g. `ME1`. Holdings show as none.
2. **Execute:** with the default first-time portfolio. Results show 3 `FILLED`, notification `SENT`.
3. **Execute again** without changing anything: you get "idempotent replay" and the same execution.
4. **Rebalance with problems:** change the scenario to
   `{"fill_delay_s": 0.5, "rate_limit_first_n": 2, "symbols": {"HDFCBANK": "REJECTED", "ITC": "TIMEOUT_AFTER_PLACE"}}`,
   click **Connect** again, switch to **Rebalance**, **Preview** (the plan lists the sells first), then **Execute**.
5. **Never-placed order:** try `"ITC": "TIMEOUT_NOT_PLACED"`. ITC becomes `UNKNOWN` and the execution `NEEDS_REVIEW`. It is never resent.
6. **CSV upload:** a file with columns `action,symbol,quantity`, where action is `BUY`, `SELL` or `ADJUST`.

## 4. Swagger, the database and the logs

- **API explorer:** <http://localhost:8000/docs>. Open an endpoint, click **Try it out**, put `dev-key` in its `X-API-Key` field (and any value in `Idempotency-Key` for `POST /executions`), then **Execute**.
- **Webhooks received:** `GET /dev/webhook-sink`.
- **What happened to each order:**
  ```bash
  docker compose exec db psql -U trade -d trade -c "select o.symbol, e.from_status, e.to_status, e.created_at from order_events e join orders o on o.id = e.order_id order by e.id desc limit 20;"
  ```
- **Live logs**, one JSON line per event:
  ```bash
  docker compose logs -f api
  ```
  Search for an `execution_id` to follow one run, e.g. `order.submit.ambiguous` → `order.reconcile.found` → `order.status.changed FILLED`.
- **Crash recovery:** start a slow execution (scenario `{"fill_delay_s": 60}`), then run `docker compose restart api`. The logs show `recovery.scheduled`, and nothing is resent.

## 5. CI on GitHub

The repo's **Actions** tab (<https://github.com/taya12hu/portfolio_trade_execution/actions>) runs on every push:
1. All tests on SQLite.
2. The engine and API tests on Postgres 16.
3. A Docker image build.

## Requirements → where to see them

| Requirement | Where | How to see it |
|---|---|---|
| ≥5 brokers, one interface | `app/brokers/base.py`, `app/brokers/<name>/adapter.py` | `GET /brokers`; `tests/adapters/test_contract.py` |
| Broker authentication | `app/connections/service.py` | Connect in the UI; redirect login is tested in `tests/engine/test_real_adapter_e2e.py` |
| First-time portfolio | `mode: INITIAL` | demo step 3 / UI |
| Rebalance (SELL / BUY_NEW / ADJUST) | `app/execution/validator.py`, `planner.py` | demo step 5 / UI Preview |
| Notification (success, failure, reasons) | `app/notifications/service.py` | `GET /dev/webhook-sink`, `signature_valid=true` |
| Broker failures, rate limits, failed trades | `app/brokers/gateway.py`, `app/execution/runner.py` | demo step 6; mock scenarios in the README |
| FastAPI + Docker + compose | `app/main.py`, `Dockerfile`, `docker-compose.yml` | section 2 above |
| README (setup, architecture, rebalance logic, library choice) | `README.md` | — |
| Bonus frontend | `frontend/index.html` | <http://localhost:8000/ui> |

## Known limits (by design or out of scope)

- **Real brokers are untested live:** the five adapters have never run against real accounts, which needs credentials, broker API subscriptions and a static IP. Live trading is off unless `LIVE_TRADING_ENABLED=true`.
- **Simulator memory:** the mock broker's state lives in memory, so restarting the API forgets mock holdings (users simply reconnect).
- **Not built yet:** Alembic migrations, an OpenAlgo adapter, live status streaming. See `TODO.md`.
