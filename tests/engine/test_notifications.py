from __future__ import annotations

import json

import httpx
import respx

from app.container import _ssl_context

from app.core.security import verify_signature
from tests.conftest import connect_mock, run_execution

HOOK = "https://hooks.example.test/kalpi"


async def _use_real_http(container):
    """Swap the loopback client for a real one so respx can intercept the webhook."""
    container.notifications._http = httpx.AsyncClient(verify=_ssl_context())


async def test_webhook_failure_never_changes_the_execution_and_can_be_resent(app, client, container):
    await _use_real_http(container)
    conn = await connect_mock(client)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(HOOK).mock(return_value=httpx.Response(500))
        ex = await run_execution(app, client, {
            "mode": "INITIAL", "connection_id": conn, "callback_url": HOOK,
            "target": [{"symbol": "TCS", "quantity": 1}]})
        assert route.call_count == 3
        first_event_id = route.calls[0].request.headers["X-Event-Id"]
        assert {c.request.headers["X-Event-Id"] for c in route.calls} == {first_event_id}

    assert ex["status"] == "COMPLETED"
    assert ex["notification"]["status"] == "FAILED"
    assert ex["notification"]["attempts"] == 3

    with respx.mock() as router:
        route = router.post(HOOK).mock(return_value=httpx.Response(204))
        r = await client.post(f"/executions/{ex['execution_id']}/notify")
        assert r.status_code == 200
        assert r.json()["notification_status"] == "SENT"
        request = route.calls[0].request
        assert request.headers["X-Event-Id"] == first_event_id  # same outcome -> same id, consumer dedupes
        assert verify_signature("whsec_test", request.content, request.headers["X-Signature"])
        assert json.loads(request.content)["status"] == "COMPLETED"

    after = (await client.get(f"/executions/{ex['execution_id']}")).json()
    assert after["status"] == "COMPLETED" and after["notification"]["status"] == "SENT"


async def test_webhook_timeout_is_retried(app, client, container):
    await _use_real_http(container)
    conn = await connect_mock(client)
    with respx.mock() as router:
        route = router.post(HOOK).mock(side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200)])
        ex = await run_execution(app, client, {
            "mode": "INITIAL", "connection_id": conn, "callback_url": HOOK,
            "target": [{"symbol": "TCS", "quantity": 1}]})
    assert route.call_count == 2
    assert ex["notification"]["status"] == "SENT"


async def test_no_callback_url_means_logged_only(app, client):
    conn = await connect_mock(client)
    ex = await run_execution(app, client, {"mode": "INITIAL", "connection_id": conn,
                                           "target": [{"symbol": "TCS", "quantity": 1}]})
    assert ex["notification"]["status"] == "SKIPPED"


async def test_notify_on_a_running_execution_is_409(app, client):
    conn = await connect_mock(client, scenario={"default": "PENDING"})
    r = await client.post("/executions", headers={"Idempotency-Key": "n1"}, json={
        "mode": "INITIAL", "connection_id": conn, "target": [{"symbol": "TCS", "quantity": 1}]})
    r2 = await client.post(f"/executions/{r.json()['execution_id']}/notify")
    assert (r2.status_code, r2.json()["error"]["code"]) == (409, "EXECUTION_NOT_FINISHED")
    r3 = await client.post(f"/executions/{r.json()['execution_id']}/reconcile")
    assert r3.status_code == 409


async def test_callback_url_must_be_https_and_public_outside_dev(app, client, settings):
    conn = await connect_mock(client)
    settings.app_env = "prod"
    try:
        for url in ("http://example.com/hook", "https://127.0.0.1/hook", "https://localhost/hook"):
            r = await client.post("/executions", headers={"Idempotency-Key": url}, json={
                "mode": "INITIAL", "connection_id": conn, "callback_url": url,
                "target": [{"symbol": "TCS", "quantity": 1}]})
            assert (r.status_code, r.json()["error"]["code"]) == (422, "INVALID_CALLBACK_URL"), url
    finally:
        settings.app_env = "test"
