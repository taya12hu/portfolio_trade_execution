#!/usr/bin/env bash
# End-to-end walkthrough against a running server (docker compose up, or uvicorn locally).
# Doubles as a smoke test: exits non-zero if any expectation fails.
#
#   BASE=http://localhost:8000 API_KEY=dev-key ./scripts/demo.sh
#
# Needs: curl, python3 (only for JSON parsing).
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
API_KEY="${API_KEY:-dev-key}"
if [[ -z "${PYTHON:-}" ]]; then  # python3 may be a non-working Store stub on Windows
  for c in python3 python; do "$c" -c 'import json' >/dev/null 2>&1 && PYTHON="$c" && break; done
fi
PY="${PYTHON:?python3 is required}"
RUN_ID="$(date +%s)"
CLIENT="DEMO${RUN_ID}"
SINK="${BASE}/dev/webhook-sink"
# The server delivers the webhook itself; from inside Docker its own address is localhost:8000.
CALLBACK="${CALLBACK:-http://localhost:8000/dev/webhook-sink}"

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$*"; exit 1; }
json() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

# api METHOD PATH [BODY] [EXTRA_CURL_ARGS...] -> sets $STATUS, $BODY, $HEADERS
api() {
  local method="$1" path="$2" body="${3:-}"; shift 3 || shift $#
  local hdr; hdr="$(mktemp)"
  if [[ -n "$body" ]]; then
    BODY="$(curl -sS -X "$method" "${BASE}${path}" -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' \
      -D "$hdr" -o - -w '\n%{http_code}' -d "$body" "$@")"
  else
    BODY="$(curl -sS -X "$method" "${BASE}${path}" -H "X-API-Key: ${API_KEY}" -D "$hdr" -o - -w '\n%{http_code}' "$@")"
  fi
  STATUS="${BODY##*$'\n'}"; BODY="${BODY%$'\n'*}"; HEADERS="$(cat "$hdr")"; rm -f "$hdr"
}

expect_status() { [[ "$STATUS" == "$1" ]] || fail "expected HTTP $1, got $STATUS: $BODY"; }

wait_finished() {  # poll until the execution is finished and its notification attempt is over
  local id="$1" status
  for _ in $(seq 1 120); do
    api GET "/executions/${id}"
    status="$(echo "$BODY" | json 'd["status"] + "/" + d["notification"]["status"]')"
    [[ "$status" != ACCEPTED/* && "$status" != RUNNING/* && "$status" != */PENDING ]] && return 0
    sleep 0.5
  done
  fail "execution ${id} did not finish"
}

# pyfmt '<python using d = parsed $BODY>'  (snippets come from quoted heredocs, so no escaping)
pyfmt() { BODY="$BODY" "$PY" -c "import json,os; d=json.loads(os.environ['BODY']); exec(open(0).read())"; }

show_orders() {
  pyfmt <<'PY'
print(f"execution {d['execution_id']}  status={d['status']}  notification={d['notification']['status']}")
for o in d["orders"]:
    print(f"  #{o['seq']} phase {o['phase']} {o['side']:4} {o['symbol']:9} qty {o['quantity']:>3}  "
          f"{o['status']:17} filled {o['filled_quantity']:>3}  attempts {o['attempts']}  "
          f"{o['error_code'] or ''} {(o['error_message'] or '')[:60]}")
print("  summary:", {k: v for k, v in d["summary"].items() if k != "by_status" and v})
PY
}

bold "1. Health and supported brokers"
api GET /health; expect_status 200; echo "$BODY"
api GET /brokers; expect_status 200
pyfmt <<'PY'
for b in d:
    print(f"  {b['name']:9} auth={b['auth_flow']:11} simulator={b['is_simulator']!s:5} live_enabled={b['live_enabled']}")
PY

bold "2. Connect the mock broker (${CLIENT}), scenario: everything succeeds"
api POST /broker-connections "{\"broker\":\"mock\",\"credentials\":{\"client_id\":\"${CLIENT}\",\"scenario\":{\"fill_delay_s\":0.3}}}"
expect_status 201
CONN="$(echo "$BODY" | json 'd["connection_id"]')"; echo "  connection_id=${CONN}"
api GET "/broker-connections/${CONN}/holdings"; echo "  holdings: $BODY"

bold "3. First-time portfolio: RELIANCE 10, TCS 5, INFY 8"
INITIAL="{\"mode\":\"INITIAL\",\"connection_id\":\"${CONN}\",\"callback_url\":\"${CALLBACK}\",\"target\":[{\"symbol\":\"RELIANCE\",\"quantity\":10},{\"symbol\":\"TCS\",\"quantity\":5},{\"symbol\":\"INFY\",\"quantity\":8}]}"
api POST /executions "$INITIAL" -H "Idempotency-Key: initial-${RUN_ID}"
expect_status 202
EX1="$(echo "$BODY" | json 'd["execution_id"]')"; echo "  202 Accepted, execution_id=${EX1}"
wait_finished "$EX1"; show_orders
[[ "$(echo "$BODY" | json 'd["status"]')" == "COMPLETED" ]] || fail "initial execution not COMPLETED"
api GET "/dev/webhook-sink?execution_id=${EX1}"
pyfmt <<'PY'
e = d[0]
print(f"  webhook: event_id={e['event_id']} signature_valid={e['signature_valid']} status={e['payload']['status']}")
PY

bold "4. Replay the same request with the same Idempotency-Key"
api POST /executions "$INITIAL" -H "Idempotency-Key: initial-${RUN_ID}"
expect_status 200
[[ "$(echo "$BODY" | json 'd["execution_id"]')" == "$EX1" ]] || fail "replay returned a different execution"
echo "$HEADERS" | grep -i '^idempotent-replayed: true' >/dev/null || fail "missing Idempotent-Replayed header"
echo "  200 OK, same execution_id, Idempotent-Replayed: true"
api GET "/broker-connections/${CONN}/holdings"; echo "  holdings unchanged: $BODY"

bold "5. Reconnect with a hostile scenario and rebalance"
echo "  HDFCBANK -> rejected by RMS, ITC -> response lost after the order was placed, first 2 orders get HTTP 429"
api POST /broker-connections "{\"broker\":\"mock\",\"credentials\":{\"client_id\":\"${CLIENT}\",\"scenario\":{\"fill_delay_s\":0.3,\"rate_limit_first_n\":2,\"retry_after_s\":0.2,\"symbols\":{\"HDFCBANK\":\"REJECTED\",\"ITC\":\"TIMEOUT_AFTER_PLACE\"}}}}"
expect_status 201
REBAL="{\"mode\":\"REBALANCE\",\"connection_id\":\"${CONN}\",\"callback_url\":\"${CALLBACK}\",\"sell\":[{\"symbol\":\"INFY\",\"quantity\":8}],\"buy\":[{\"symbol\":\"HDFCBANK\",\"quantity\":4},{\"symbol\":\"ITC\",\"quantity\":20}],\"adjust\":[{\"symbol\":\"TCS\",\"delta\":-2},{\"symbol\":\"RELIANCE\",\"delta\":5}]}"
api POST /executions "$REBAL" -H "Idempotency-Key: rebalance-${RUN_ID}"
expect_status 202
EX2="$(echo "$BODY" | json 'd["execution_id"]')"

bold "6. Result: sells first, rejection reason, ITC reconciled with exactly one broker order"
wait_finished "$EX2"; show_orders
[[ "$(echo "$BODY" | json 'd["status"]')" == "PARTIALLY_COMPLETED" ]] || fail "rebalance should be PARTIALLY_COMPLETED"
[[ "$(echo "$BODY" | json '[o for o in d["orders"] if o["symbol"]=="ITC"][0]["status"]')" == "FILLED" ]] || fail "ITC should be reconciled and FILLED"
api GET "/broker-connections/${CONN}/holdings"; echo "  holdings now: $BODY"

bold "7. Validation and conflicts (nothing is sent to the broker)"
api POST /executions "{\"mode\":\"REBALANCE\",\"connection_id\":\"${CONN}\",\"buy\":[{\"symbol\":\"WIPRO\",\"quantity\":1},{\"symbol\":\"WIPRO\",\"quantity\":2}],\"adjust\":[{\"symbol\":\"WIPRO\",\"delta\":1}]}" -H "Idempotency-Key: bad1-${RUN_ID}"
expect_status 422; echo "  422 (payload rules): $BODY"
api POST /executions "{\"mode\":\"REBALANCE\",\"connection_id\":\"${CONN}\",\"sell\":[{\"symbol\":\"TCS\",\"quantity\":500}],\"buy\":[{\"symbol\":\"ITC\",\"quantity\":1}],\"adjust\":[{\"symbol\":\"SBIN\",\"delta\":-1}]}" -H "Idempotency-Key: bad2-${RUN_ID}"
expect_status 422; echo "  422 (vs live holdings): $BODY"
api POST /broker-connections "{\"broker\":\"mock\",\"credentials\":{\"client_id\":\"${CLIENT}SLOW\",\"scenario\":{\"fill_delay_s\":5}}}"
SLOW="$(echo "$BODY" | json 'd["connection_id"]')"
api POST /executions "{\"mode\":\"INITIAL\",\"connection_id\":\"${SLOW}\",\"target\":[{\"symbol\":\"SBIN\",\"quantity\":1}]}" -H "Idempotency-Key: slow-a-${RUN_ID}"
expect_status 202
api POST /executions "{\"mode\":\"INITIAL\",\"connection_id\":\"${SLOW}\",\"target\":[{\"symbol\":\"LT\",\"quantity\":1}]}" -H "Idempotency-Key: slow-b-${RUN_ID}"
expect_status 409; echo "  409: $BODY"

bold "Demo finished: all expectations met"
