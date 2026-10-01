"""End-to-end flows through the real API, engine and gateway against the mock exchange."""

from __future__ import annotations

from app.core.security import verify_signature
from tests.conftest import SINK, by_symbol, connect_mock, post_execution, run_execution


def acct(container, client_id="DEMO1"):
    return container.mock_exchange.accounts[client_id]


async def test_initial_portfolio_happy_path(app, client, container):
    conn = await connect_mock(client)
    ex = await run_execution(app, client, {
        "mode": "INITIAL", "connection_id": conn, "callback_url": SINK,
        "target": [{"symbol": "RELIANCE", "quantity": 10}, {"symbol": "TCS", "quantity": 5}, {"symbol": "INFY", "quantity": 8}],
    })
    assert ex["status"] == "COMPLETED"
    assert ex["summary"]["filled"] == 3
    assert all(o["status"] == "FILLED" and o["filled_quantity"] == o["quantity"] and o["broker_order_id"] for o in ex["orders"])
    assert all(o["average_price"] is not None for o in ex["orders"])

    holdings = (await client.get(f"/broker-connections/{conn}/holdings")).json()
    assert {h["symbol"]: h["quantity"] for h in holdings} == {"RELIANCE": 10, "TCS": 5, "INFY": 8}

    # Signed webhook delivered to the dev sink.
    assert ex["notification"]["status"] == "SENT"
    [event] = (await client.get("/dev/webhook-sink", params={"execution_id": ex["execution_id"]})).json()
    assert event["signature_valid"] is True
    assert event["payload"]["status"] == "COMPLETED"
    assert event["payload"]["summary"]["filled"] == 3
    assert event["event_id"] == event["payload"]["event_id"]
    assert acct(container).place_calls == 3


async def test_rebalance_runs_every_sell_before_any_buy(app, client, container):
    conn = await connect_mock(client, scenario={"fill_delay_s": 0.05},
                              holdings={"INFY": 8, "TCS": 5, "RELIANCE": 10})
    ex = await run_execution(app, client, {
        "mode": "REBALANCE", "connection_id": conn,
        "buy": [{"symbol": "HDFCBANK", "quantity": 4}, {"symbol": "ITC", "quantity": 20}],
        "sell": [{"symbol": "INFY", "quantity": 8}],
        "adjust": [{"symbol": "TCS", "delta": -2}, {"symbol": "RELIANCE", "delta": 5}],
    })
    assert ex["status"] == "COMPLETED"
    orders = by_symbol(ex)
    assert [(o["symbol"], o["side"], o["phase"]) for o in ex["orders"]] == [
        ("INFY", "SELL", 1), ("TCS", "SELL", 1), ("HDFCBANK", "BUY", 2), ("ITC", "BUY", 2), ("RELIANCE", "BUY", 2)]
    assert orders["TCS"]["instruction_type"] == "ADJUST" and orders["TCS"]["quantity"] == 2

    # Exchange-side event log: both sells settled before the first buy was even placed.
    events = acct(container).events
    first_buy = next(i for i, e in enumerate(events) if e[0] == "placed" and e[2] == "BUY")
    sells_settled = [i for i, e in enumerate(events) if e[0] == "settled" and e[2] == "SELL"]
    assert len(sells_settled) == 2 and max(sells_settled) < first_buy

    holdings = {h["symbol"]: h["quantity"] for h in (await client.get(f"/broker-connections/{conn}/holdings")).json()}
    assert holdings == {"TCS": 3, "RELIANCE": 15, "HDFCBANK": 4, "ITC": 20}


async def test_timeout_after_place_is_reconciled_without_a_duplicate(app, client, container):
    conn = await connect_mock(client, scenario={"symbols": {"ITC": "TIMEOUT_AFTER_PLACE"}})
    ex = await run_execution(app, client, {
        "mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "ITC", "quantity": 20}]})
    [order] = ex["orders"]
    assert order["status"] == "FILLED" and order["filled_quantity"] == 20
    assert order["broker_order_id"] is not None
    assert order["error_code"] is None
    assert ex["status"] == "COMPLETED"
    a = acct(container)
    assert a.place_calls == 1  # never resent
    assert len(a.orders_with_tag(order["client_order_id"])) == 1


async def test_timeout_not_placed_becomes_unknown_and_is_never_resent(app, client, container, settings):
    conn = await connect_mock(client, scenario={"symbols": {"ITC": "TIMEOUT_NOT_PLACED"}})
    ex = await run_execution(app, client, {
        "mode": "INITIAL", "connection_id": conn, "callback_url": SINK,
        "target": [{"symbol": "ITC", "quantity": 20}, {"symbol": "TCS", "quantity": 1}]})
    orders = by_symbol(ex)
    assert orders["ITC"]["status"] == "UNKNOWN"
    assert orders["ITC"]["error_code"] == "OUTCOME_UNKNOWN"
    assert orders["TCS"]["status"] == "FILLED"
    assert ex["status"] == "NEEDS_REVIEW"
    assert acct(container).place_calls == 2  # one call per order: ITC was not retried
    [event] = (await client.get("/dev/webhook-sink", params={"execution_id": ex["execution_id"]})).json()
    assert event["payload"]["status"] == "NEEDS_REVIEW"
    assert event["payload"]["summary"]["unknown"] == 1

    # Manual reconcile inside the grace period: still UNKNOWN (absence isn't proof yet).
    r = await client.post(f"/executions/{ex['execution_id']}/reconcile")
    assert r.status_code == 200
    assert by_symbol(r.json())["ITC"]["status"] == "UNKNOWN"

    # After the grace period, confirmed absent -> FAILED(NOT_PLACED_CONFIRMED), safe to re-run.
    settings.reconcile_confirm_after_s = 0
    r = await client.post(f"/executions/{ex['execution_id']}/reconcile")
    itc = by_symbol(r.json())["ITC"]
    assert (itc["status"], itc["error_code"]) == ("FAILED", "NOT_PLACED_CONFIRMED")
    assert r.json()["status"] == "PARTIALLY_COMPLETED"
    assert acct(container).place_calls == 2


async def test_reconcile_finds_an_order_that_appeared_late(app, client, container):
    """Broker accepted the order but it showed up in the book only after our checks."""
    conn = await connect_mock(client, scenario={"symbols": {"ITC": "TIMEOUT_NOT_PLACED"}})
    ex = await run_execution(app, client, {
        "mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "ITC", "quantity": 20}]})
    [order] = ex["orders"]
    assert order["status"] == "UNKNOWN"
    # Simulate the late appearance in the broker's book.
    from app.domain.models import OrderState, Side
    a = acct(container)
    container.mock_exchange.create_order(a, tag=order["client_order_id"], symbol="ITC", side=Side.BUY, quantity=20,
                                         target_status=OrderState.FILLED, target_filled=20, target_message=None, delay_s=0)
    r = await client.post(f"/executions/{ex['execution_id']}/reconcile")
    [order] = r.json()["orders"]
    assert order["status"] == "FILLED" and order["broker_order_id"] is not None
    assert r.json()["status"] == "COMPLETED"


async def test_rate_limited_orders_are_retried_and_counted(app, client, container):
    conn = await connect_mock(client, scenario={"rate_limit_first_n": 2, "retry_after_s": 0.01})
    ex = await run_execution(app, client, {
        "mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "TCS", "quantity": 1}]})
    [order] = ex["orders"]
    assert order["status"] == "FILLED"
    assert order["attempts"] == 3
    a = acct(container)
    assert a.place_calls == 3 and len(a.orders) == 1


async def test_partial_fill_and_pending_are_reported_not_cancelled(app, client, container):
    conn = await connect_mock(client, scenario={"symbols": {"INFY": "PARTIAL_FILL", "WIPRO": "PENDING"}})
    ex = await run_execution(app, client, {
        "mode": "INITIAL", "connection_id": conn,
        "target": [{"symbol": "INFY", "quantity": 8}, {"symbol": "WIPRO", "quantity": 3}]})
    orders = by_symbol(ex)
    assert (orders["INFY"]["status"], orders["INFY"]["filled_quantity"]) == ("PARTIALLY_FILLED", 4)
    assert (orders["WIPRO"]["status"], orders["WIPRO"]["filled_quantity"]) == ("OPEN", 0)
    assert ex["status"] == "PARTIALLY_COMPLETED"
    assert ex["summary"]["open"] == 2 and ex["summary"]["partially_filled"] == 1
    assert all(o.status.value in ("OPEN", "PARTIALLY_FILLED") for o in acct(container).orders.values())


async def test_rejections_and_cancellations_carry_reasons(app, client):
    conn = await connect_mock(client, scenario={"symbols": {
        "HDFCBANK": "REJECTED", "SBIN": "REJECTED_ON_PLACE", "LT": "CANCELLED"}})
    ex = await run_execution(app, client, {
        "mode": "INITIAL", "connection_id": conn,
        "target": [{"symbol": s, "quantity": 1} for s in ("HDFCBANK", "SBIN", "LT", "TCS")]})
    o = by_symbol(ex)
    assert (o["HDFCBANK"]["status"], o["HDFCBANK"]["error_code"]) == ("REJECTED", "ORDER_REJECTED")
    assert "RMS" in o["HDFCBANK"]["error_message"]
    assert (o["SBIN"]["status"], o["SBIN"]["error_code"]) == ("REJECTED", "ORDER_REJECTED")
    assert o["SBIN"]["broker_order_id"] is None
    assert (o["LT"]["status"], o["LT"]["error_code"]) == ("CANCELLED", "ORDER_CANCELLED")
    assert o["TCS"]["status"] == "FILLED"
    assert ex["status"] == "PARTIALLY_COMPLETED"


async def test_buys_are_still_attempted_after_a_failed_sell_and_rms_stops_unfunded_ones(app, client):
    conn = await connect_mock(client, holdings={"INFY": 10},
                              scenario={"funds": 0, "symbols": {"INFY": "REJECTED"}})
    ex = await run_execution(app, client, {
        "mode": "REBALANCE", "connection_id": conn,
        "sell": [{"symbol": "INFY", "quantity": 10}], "buy": [{"symbol": "TCS", "quantity": 1}]})
    o = by_symbol(ex)
    assert o["INFY"]["status"] == "REJECTED"
    assert o["TCS"]["status"] == "REJECTED" and "insufficient funds" in o["TCS"]["error_message"]
    assert ex["status"] == "FAILED"


async def test_session_expiring_mid_run_fails_remaining_orders_without_sending(app, client, container, settings):
    settings.submit_concurrency = 1
    conn = await connect_mock(client, scenario={"expire_session_after_orders": 1})
    ex = await run_execution(app, client, {
        "mode": "INITIAL", "connection_id": conn,
        "target": [{"symbol": s, "quantity": 1} for s in ("TCS", "INFY", "ITC")]})
    statuses = [(o["symbol"], o["status"], o["error_code"]) for o in ex["orders"]]
    assert statuses[0][1] == "SUBMITTED"  # placed; status unknowable once the token died
    assert statuses[1:] == [("INFY", "FAILED", "AUTH_EXPIRED"), ("ITC", "FAILED", "AUTH_EXPIRED")]
    assert ex["error_code"] == "BROKER_REAUTH_REQUIRED"
    assert acct(container).place_calls == 2  # ITC failed fast without a broker call
    assert (await client.get(f"/broker-connections/{conn}")).json()["status"] == "EXPIRED"
    # New executions are refused until the user reconnects.
    r = await post_execution(client, {"mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "SBIN", "quantity": 1}]})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "BROKER_REAUTH_REQUIRED")


async def test_session_refresh_mid_run_is_transparent(app, client, container):
    conn = await connect_mock(client, scenario={"expire_session_after_orders": 1, "supports_refresh": True})
    ex = await run_execution(app, client, {
        "mode": "INITIAL", "connection_id": conn,
        "target": [{"symbol": s, "quantity": 1} for s in ("TCS", "INFY", "ITC")]})
    assert ex["status"] == "COMPLETED"
    assert (await client.get(f"/broker-connections/{conn}")).json()["status"] == "ACTIVE"


async def test_broker_down_fails_orders_after_bounded_retries(app, client, container, settings):
    conn = await connect_mock(client, scenario={"symbols": {"TCS": "BROKER_DOWN"}})
    ex = await run_execution(app, client, {
        "mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "TCS", "quantity": 1}]})
    [o] = ex["orders"]
    assert (o["status"], o["error_code"]) == ("FAILED", "BROKER_UNAVAILABLE")
    assert o["attempts"] == settings.broker_max_retries + 1
    assert ex["status"] == "FAILED"


async def test_preflight_expired_session_without_refresh_creates_nothing(app, client, container):
    conn = await connect_mock(client, scenario={"session_expired": True})
    r = await post_execution(client, {"mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "TCS", "quantity": 1}]})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "BROKER_REAUTH_REQUIRED")
    assert (await client.get("/executions")).json()["items"] == []
    assert (await client.get(f"/broker-connections/{conn}")).json()["status"] == "EXPIRED"
    assert acct(container).place_calls == 0


async def test_preflight_refreshes_an_expired_session_when_supported(app, client):
    conn = await connect_mock(client, scenario={"session_expired": True, "supports_refresh": True})
    ex = await run_execution(app, client, {
        "mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "TCS", "quantity": 1}]})
    assert ex["status"] == "COMPLETED"


async def test_preflight_broker_down_returns_503_and_creates_nothing(app, client, container):
    conn = await connect_mock(client, scenario={"fail_reads_first_n": 100})
    r = await post_execution(client, {"mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "TCS", "quantity": 1}]})
    assert (r.status_code, r.json()["error"]["code"]) == (503, "BROKER_UNAVAILABLE")
    assert (await client.get("/executions")).json()["items"] == []


async def test_transient_read_failures_during_monitoring_are_tolerated(app, client, container):
    conn = await connect_mock(client, scenario={"fill_delay_s": 0.05})
    r = await post_execution(client, {"mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "TCS", "quantity": 1}]})
    assert r.status_code == 202
    acct(container).reads_failed_so_far = -2  # next two reads fail
    from tests.conftest import wait_done
    ex = await wait_done(app, client, r.json()["execution_id"])
    assert ex["status"] == "COMPLETED"


async def test_invalid_symbol_and_holdings_mismatches_are_422_per_leg(app, client, container):
    conn = await connect_mock(client, holdings={"INFY": 8}, scenario={"symbols": {"BADSYM": "INVALID_SYMBOL"}})
    r = await post_execution(client, {"mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "BADSYM", "quantity": 1}]})
    assert r.status_code == 422
    assert [(d["code"], d["symbol"]) for d in r.json()["error"]["details"]] == [("INVALID_SYMBOL", "BADSYM")]

    r = await post_execution(client, {
        "mode": "REBALANCE", "connection_id": conn,
        "sell": [{"symbol": "INFY", "quantity": 9}], "buy": [{"symbol": "INFY", "quantity": 1}],
        "adjust": [{"symbol": "TCS", "delta": 2}, {"symbol": "TCS", "delta": 1}]})
    assert r.status_code == 422
    assert sorted(d["code"] for d in r.json()["error"]["details"]) == ["CONFLICTING_INSTRUCTIONS", "DUPLICATE_SYMBOL"]

    r = await post_execution(client, {
        "mode": "REBALANCE", "connection_id": conn,
        "sell": [{"symbol": "INFY", "quantity": 9}], "adjust": [{"symbol": "TCS", "delta": 2}]})
    assert r.status_code == 422
    assert [(d["code"], d["field"]) for d in r.json()["error"]["details"]] == [
        ("INSUFFICIENT_HOLDINGS", "sell[0]"), ("NOT_HELD", "adjust[0]")]
    assert acct(container).place_calls == 0


async def test_preview_plans_without_trading(app, client, container):
    conn = await connect_mock(client, holdings={"INFY": 8})
    r = await client.post("/executions/preview", json={
        "mode": "REBALANCE", "connection_id": conn,
        "sell": [{"symbol": "INFY", "quantity": 8}], "buy": [{"symbol": "TCS", "quantity": 2}]})
    body = r.json()
    assert r.status_code == 200 and body["valid"] is True
    assert [(o["phase"], o["side"], o["symbol"]) for o in body["orders"]] == [(1, "SELL", "INFY"), (2, "BUY", "TCS")]
    r = await client.post("/executions/preview", json={
        "mode": "REBALANCE", "connection_id": conn, "sell": [{"symbol": "INFY", "quantity": 9}]})
    assert r.json()["valid"] is False and r.json()["errors"][0]["code"] == "INSUFFICIENT_HOLDINGS"
    assert acct(container).place_calls == 0
    assert (await client.get("/executions")).json()["items"] == []
