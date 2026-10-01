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

Open <http://localhost:8000/ui>. It walks you through three steps: **Connect → Your trades → Review & place**.

1. **Connect:** choose **Practice account** (pretend money) and press **Connect**.
2. **Your trades:** an example portfolio (RELIANCE 10, TCS 5, INFY 8) is already filled in. Each line has a Buy/Sell switch, a stock and a quantity.
3. **Review & place:** the review shows exactly what will happen; sales are always listed first. Press **Place 3 orders**. Expect a green "All 3 orders completed" with the price paid for each.
4. **Rebalance with problems:**
   1. Press **Make more trades**. Your holdings now appear at the top.
   2. Press **Change** next to the connected account, open **Practice options**, and tick every box.
   3. Press **Connect**, then **Fill in an example**, add a line `ITC 20`, then **Review trades** and **Place orders**.
   4. Expect the sale to finish before the purchases start, HDFCBANK to be **Rejected** with the broker's reason, ITC to be **Done** even though the broker's reply was "lost", and the banner to read "3 of 4 orders completed".
5. **Mistakes are caught before anything is sent:** sell more than you own, sell a stock you don't own, or list the same stock twice. Each problem is explained next to the stock.
6. **Other things to try:**
   - **Show technical details** reveals broker order ids, our tags and retry counts.
   - **Recent activity** reopens past batches.
   - **Import from a file** accepts `RELIANCE,10` (buy) or `INFY,-8` (sell), one per line.
   - The page also works at phone width.

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
