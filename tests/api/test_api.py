from __future__ import annotations

from sqlalchemy import select

from app.db.models import BrokerConnection
from tests.conftest import OTHER_API_KEY, connect_mock, post_execution, run_execution


async def test_health_needs_no_key(client):
    r = await client.get("/health", headers={"X-API-Key": ""})
    assert r.json() == {"status": "ok", "db": "ok"}


async def test_api_key_required(client):
    for headers in ({"X-API-Key": ""}, {"X-API-Key": "nope"}):
        r = await client.get("/brokers", headers=headers)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHORIZED"


async def test_brokers_listing(client):
    brokers = {b["name"]: b for b in (await client.get("/brokers")).json()}
    assert brokers["mock"]["is_simulator"] is True and brokers["mock"]["live_enabled"] is True


async def test_missing_idempotency_key_is_400(client):
    conn = await connect_mock(client)
    r = await client.post("/executions", json={"mode": "INITIAL", "connection_id": conn,
                                               "target": [{"symbol": "TCS", "quantity": 1}]})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED")


async def test_malformed_json_and_schema_errors_use_the_envelope(client):
    r = await client.post("/executions", content=b"{not json", headers={"Idempotency-Key": "x", "Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = await post_execution(client, {"mode": "INITIAL", "connection_id": "c", "target": [{"symbol": "TCS", "quantity": 0}]})
    err = r.json()["error"]
    assert r.status_code == 422 and err["code"] == "VALIDATION_ERROR"
    assert any("quantity" in d["field"] for d in err["details"])
    r = await post_execution(client, {"mode": "SWAP", "connection_id": "c"})
    assert r.status_code == 422


async def test_unknown_routes_use_the_envelope(client):
    r = await client.get("/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


async def test_unsupported_broker(client):
    r = await client.post("/broker-connections", json={"broker": "sharekhan", "credentials": {}})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "UNSUPPORTED_BROKER" and "mock" in body["details"][0]["supported"]


async def test_mock_login_errors(client):
    r = await client.post("/broker-connections", json={"broker": "mock", "credentials": {"client_id": "A", "scenario": {"login": "INVALID_CREDENTIALS"}}})
    assert (r.status_code, r.json()["error"]["code"]) == (401, "INVALID_BROKER_CREDENTIALS")
    r = await client.post("/broker-connections", json={"broker": "mock", "credentials": {"client_id": "A", "scenario": {"login": "BROKER_DOWN"}}})
    assert (r.status_code, r.json()["error"]["code"]) == (503, "BROKER_UNAVAILABLE")
    r = await client.post("/broker-connections", json={"broker": "mock", "credentials": {"client_id": "A", "scenario": {"defualt": "SUCCESS"}}})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "INVALID_CONNECTION_PARAMS")
    r = await client.post("/broker-connections", json={"broker": "mock", "credentials": {}})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "INVALID_CONNECTION_PARAMS")


async def test_reconnecting_updates_the_same_connection(client):
    c1 = await connect_mock(client, holdings={"INFY": 3})
    c2 = await connect_mock(client, scenario={"symbols": {"TCS": "REJECTED"}})
    assert c1 == c2
    holdings = (await client.get(f"/broker-connections/{c1}/holdings")).json()
    assert holdings[0]["symbol"] == "INFY" and holdings[0]["quantity"] == 3


async def test_owner_scoping(app, client):
    conn = await connect_mock(client)
    ex = await run_execution(app, client, {"mode": "INITIAL", "connection_id": conn,
                                           "target": [{"symbol": "TCS", "quantity": 1}]})
    other = {"X-API-Key": OTHER_API_KEY}
    assert (await client.get(f"/broker-connections/{conn}", headers=other)).status_code == 404
    assert (await client.get(f"/broker-connections/{conn}/holdings", headers=other)).status_code == 404
    assert (await client.get(f"/executions/{ex['execution_id']}", headers=other)).status_code == 404
    assert (await client.get("/executions", headers=other)).json()["items"] == []
    r = await post_execution(client, {"mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "INFY", "quantity": 1}]},
                             headers=other)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "CONNECTION_NOT_FOUND")


async def test_tokens_never_leave_the_service_and_are_encrypted_at_rest(app, client, container):
    conn = await connect_mock(client)
    ex = await run_execution(app, client, {"mode": "INITIAL", "connection_id": conn,
                                           "target": [{"symbol": "TCS", "quantity": 1}]})
    acct = container.mock_exchange.accounts["DEMO1"]
    [token] = acct.valid_tokens
    [refresh] = acct.refresh_tokens
    bodies = [
        (await client.get(f"/broker-connections/{conn}")).text,
        (await client.get("/broker-connections")).text,
        (await client.get(f"/executions/{ex['execution_id']}")).text,
        (await client.get("/executions")).text,
        (await client.get("/dev/webhook-sink")).text,
    ]
    for body in bodies:
        assert token not in body and refresh not in body and "mock-at-" not in body
    async with container.sessionmaker() as db:
        row = (await db.execute(select(BrokerConnection).where(BrokerConnection.id == conn))).scalar_one()
    assert row.access_token_enc and token not in row.access_token_enc
    assert container.vault.decrypt(row.access_token_enc) == token


async def test_revoke_wipes_tokens_and_blocks_trading(client, container):
    conn = await connect_mock(client)
    assert (await client.delete(f"/broker-connections/{conn}")).status_code == 204
    assert (await client.get(f"/broker-connections/{conn}")).json()["status"] == "REVOKED"
    async with container.sessionmaker() as db:
        row = await db.get(BrokerConnection, conn)
    assert row.access_token_enc is None and row.refresh_token_enc is None
    r = await post_execution(client, {"mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "TCS", "quantity": 1}]})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "BROKER_REAUTH_REQUIRED")


async def test_list_and_filter_executions(app, client):
    conn = await connect_mock(client)
    await run_execution(app, client, {"mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "TCS", "quantity": 1}]})
    items = (await client.get("/executions", params={"status": "COMPLETED"})).json()["items"]
    assert len(items) == 1 and items[0]["connection_id"] == conn
    assert (await client.get("/executions", params={"status": "FAILED"})).json()["items"] == []


async def test_fat_finger_caps(client, settings):
    conn = await connect_mock(client)
    r = await post_execution(client, {"mode": "INITIAL", "connection_id": conn,
                                      "target": [{"symbol": "TCS", "quantity": settings.max_qty_per_order + 1}]})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["code"] == "QUANTITY_LIMIT_EXCEEDED"


async def test_request_id_is_echoed(client):
    r = await client.get("/health", headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"


async def test_undecryptable_session_means_reconnect_not_500(client, container):
    """E.g. TOKEN_ENCRYPTION_KEY rotated/lost between restarts."""
    from cryptography.fernet import Fernet

    from app.core.security import TokenVault

    conn = await connect_mock(client)
    container.connections._vault = TokenVault(Fernet.generate_key().decode(), allow_ephemeral=False)
    r = await client.get(f"/broker-connections/{conn}/holdings")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "BROKER_REAUTH_REQUIRED")
    assert (await client.get(f"/broker-connections/{conn}")).json()["status"] == "EXPIRED"
    # Reconnecting fixes it.
    assert await connect_mock(client) == conn
    assert (await client.get(f"/broker-connections/{conn}/holdings")).status_code == 200


async def test_console_is_served(client):
    r = await client.get("/ui")
    assert r.status_code == 200 and "Trade Execution Console" in r.text
    assert (await client.get("/", follow_redirects=False)).headers["location"] == "/ui"
