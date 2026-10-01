from __future__ import annotations

import asyncio

from tests.conftest import OTHER_API_KEY, connect_mock, post_execution, wait_done


def initial(conn, *symbols):
    return {"mode": "INITIAL", "connection_id": conn, "target": [{"symbol": s, "quantity": 1} for s in symbols]}


async def test_replay_returns_the_same_execution_and_trades_once(app, client, container):
    conn = await connect_mock(client)
    payload = initial(conn, "TCS", "INFY")
    r1 = await post_execution(client, payload, key="key-1")
    assert r1.status_code == 202
    await wait_done(app, client, r1.json()["execution_id"])

    r2 = await post_execution(client, payload, key="key-1")
    assert r2.status_code == 200
    assert r2.headers["Idempotent-Replayed"] == "true"
    assert r2.json()["execution_id"] == r1.json()["execution_id"]
    assert r2.json()["status"] == "COMPLETED"  # replay shows the current state
    assert container.mock_exchange.accounts["DEMO1"].place_calls == 2


async def test_symbol_case_and_whitespace_do_not_change_the_request_hash(app, client):
    conn = await connect_mock(client)
    r1 = await post_execution(client, initial(conn, "TCS"), key="k")
    await wait_done(app, client, r1.json()["execution_id"])
    r2 = await post_execution(client, {"mode": "INITIAL", "connection_id": conn,
                                       "target": [{"symbol": " tcs", "quantity": 1}]}, key="k")
    assert r2.status_code == 200 and r2.json()["execution_id"] == r1.json()["execution_id"]


async def test_same_key_with_a_different_body_is_rejected(app, client):
    conn = await connect_mock(client)
    r1 = await post_execution(client, initial(conn, "TCS"), key="key-2")
    await wait_done(app, client, r1.json()["execution_id"])
    r2 = await post_execution(client, initial(conn, "INFY"), key="key-2")
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_concurrent_identical_requests_create_one_execution(app, client, container):
    conn = await connect_mock(client)
    payload = initial(conn, "TCS", "INFY", "ITC")
    responses = await asyncio.gather(*(post_execution(client, payload, key="dup") for _ in range(5)))
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 200, 200, 200, 202], [r.text for r in responses]
    ids = {r.json()["execution_id"] for r in responses}
    assert len(ids) == 1
    await wait_done(app, client, ids.pop())
    assert container.mock_exchange.accounts["DEMO1"].place_calls == 3
    assert len((await client.get("/executions")).json()["items"]) == 1


async def test_second_execution_on_a_busy_account_gets_409(app, client, container):
    conn = await connect_mock(client, scenario={"default": "PENDING"})
    r1 = await post_execution(client, initial(conn, "TCS"), key="a")
    assert r1.status_code == 202
    r2 = await post_execution(client, initial(conn, "INFY"), key="b")  # double click with a fresh key
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "EXECUTION_IN_PROGRESS"
    assert r2.json()["error"]["details"][0]["execution_id"] == r1.json()["execution_id"]
    await wait_done(app, client, r1.json()["execution_id"])
    # Once finished, the account is free again.
    r3 = await post_execution(client, initial(conn, "INFY"), key="c")
    assert r3.status_code == 202


async def test_concurrent_requests_with_different_keys_only_one_wins(app, client, container):
    conn = await connect_mock(client, scenario={"fill_delay_s": 0.3})  # the winner stays RUNNING for a while
    responses = await asyncio.gather(*(post_execution(client, initial(conn, "TCS"), key=f"k{i}") for i in range(5)))
    codes = sorted(r.status_code for r in responses)
    assert codes == [202, 409, 409, 409, 409], [r.text for r in responses]
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {"EXECUTION_IN_PROGRESS"}
    await container.tasks.wait_all(10)
    assert container.mock_exchange.accounts["DEMO1"].place_calls == 1


async def test_idempotency_keys_are_scoped_per_owner(app, client):
    conn_a = await connect_mock(client)
    conn_b = await connect_mock(client, client_id="OTHER", api_key=OTHER_API_KEY)
    r1 = await post_execution(client, initial(conn_a, "TCS"), key="shared")
    r2 = await post_execution(client, initial(conn_b, "TCS"), key="shared", headers={"X-API-Key": OTHER_API_KEY})
    assert r1.status_code == r2.status_code == 202
    assert r1.json()["execution_id"] != r2.json()["execution_id"]
    await wait_done(app, client, r1.json()["execution_id"])
    await app.state.container.tasks.wait_all(10)
