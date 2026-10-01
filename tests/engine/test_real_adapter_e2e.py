"""A real adapter (Zerodha) through the whole stack — API, connection flow, engine, gateway —
with the broker's HTTP API mocked by respx."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
import respx

from tests.adapters.fixtures import form
from tests.conftest import by_symbol, post_execution, wait_done

KITE = "https://api.kite.trade"


@pytest.fixture
def kite_settings(settings):
    settings.zerodha_api_key = "kite-key"
    settings.zerodha_api_secret = "kite-secret"
    return settings


async def connect_zerodha(client, router) -> str:
    r = await client.post("/broker-connections", json={"broker": "zerodha"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "PENDING_LOGIN"
    redirect_params = parse_qs(urlsplit(body["login_url"]).query)["redirect_params"][0]
    state = parse_qs(unquote(redirect_params))["state"][0]

    router.post(f"{KITE}/session/token").mock(return_value=httpx.Response(
        200, json={"status": "success", "data": {"user_id": "AB1234", "access_token": "kite-at"}}))
    # The broker redirects the user's browser here (no API key on this route).
    r = await client.get("/broker-connections/zerodha/callback",
                         params={"request_token": "rt1", "status": "success", "state": state},
                         headers={"X-API-Key": ""})
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["broker_user_id"]) == ("ACTIVE", "AB1234")
    assert r.json()["connection_id"] == body["connection_id"]

    # A login state is single-use.
    r = await client.get("/broker-connections/zerodha/callback",
                         params={"request_token": "rt1", "status": "success", "state": state})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "INVALID_STATE")
    return body["connection_id"]


async def test_redirect_login_then_kill_switch_blocks_trading(app, client, kite_settings):
    with respx.mock(assert_all_called=False) as router:
        conn = await connect_zerodha(client, router)
        place = router.post(f"{KITE}/orders/regular")
        r = await post_execution(client, {"mode": "INITIAL", "connection_id": conn,
                                          "target": [{"symbol": "SBIN", "quantity": 1}]})
        assert (r.status_code, r.json()["error"]["code"]) == (403, "LIVE_TRADING_DISABLED")
        assert not place.called
    assert (await client.get("/executions")).json()["items"] == []


async def test_redirect_broker_without_app_credentials(client, settings):
    r = await client.post("/broker-connections", json={"broker": "zerodha"})
    assert (r.status_code, r.json()["error"]["code"]) == (503, "BROKER_NOT_CONFIGURED")


async def test_live_rebalance_through_the_zerodha_adapter(app, client, kite_settings):
    kite_settings.live_trading_enabled = True
    book: list[dict] = []

    def place(request: httpx.Request) -> httpx.Response:
        f = form(request)
        oid = str(1000 + len(book))
        book.append({"order_id": oid, "status": "COMPLETE", "tag": f["tag"], "tradingsymbol": f["tradingsymbol"],
                     "transaction_type": f["transaction_type"], "quantity": int(f["quantity"]),
                     "filled_quantity": int(f["quantity"]), "pending_quantity": 0, "average_price": 101.5})
        if f["tradingsymbol"] == "ITC":  # the order is accepted but the response never arrives
            raise httpx.ReadTimeout("lost response")
        return httpx.Response(200, json={"status": "success", "data": {"order_id": oid}})

    with respx.mock() as router:
        conn = await connect_zerodha(client, router)
        router.get(f"{KITE}/user/profile").mock(return_value=httpx.Response(200, json={"status": "success", "data": {}}))
        router.get(f"{KITE}/portfolio/holdings").mock(return_value=httpx.Response(200, json={"status": "success", "data": [
            {"tradingsymbol": "INFY", "exchange": "NSE", "quantity": 8, "t1_quantity": 0, "used_quantity": 0,
             "average_price": 1500}]}))
        place_route = router.post(f"{KITE}/orders/regular").mock(side_effect=place)
        router.get(f"{KITE}/orders").mock(side_effect=lambda _req: httpx.Response(
            200, json={"status": "success", "data": book}))

        r = await post_execution(client, {"mode": "REBALANCE", "connection_id": conn,
                                          "sell": [{"symbol": "INFY", "quantity": 8}],
                                          "buy": [{"symbol": "ITC", "quantity": 20}, {"symbol": "SBIN", "quantity": 3}]})
        assert r.status_code == 202, r.text
        ex = await wait_done(app, client, r.json()["execution_id"])

    assert ex["status"] == "COMPLETED", ex
    o = by_symbol(ex)
    assert all(o[s]["status"] == "FILLED" for s in ("INFY", "ITC", "SBIN"))
    assert place_route.call_count == 3  # ITC was reconciled from the order book, never resent
    tradingsymbols = [form(c.request)["tradingsymbol"] for c in place_route.calls]
    assert tradingsymbols[0] == "INFY"  # the sell went first
    assert {form(c.request)["tag"] for c in place_route.calls} == {x["client_order_id"] for x in ex["orders"]}
