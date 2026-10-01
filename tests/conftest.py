from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.main import create_app

API_KEY = "test-key"
OTHER_API_KEY = "other-key"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        # CI also runs the engine/API tests against Postgres via TEST_DATABASE_URL.
        database_url=os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        api_keys=f"{API_KEY}:owner-a,{OTHER_API_KEY}:owner-b",
        token_encryption_key=Fernet.generate_key().decode(),
        webhook_secret="whsec_test",
        log_level="WARNING",
        poll_interval_s=0.01,
        order_monitor_timeout_s=0.5,
        reconcile_attempts=3,
        reconcile_delay_s=0.01,
        retry_backoff_base_s=0.001,
        webhook_backoff_base_s=0.001,
        broker_max_retries=3,
    )


async def _reset_shared_database(url: str) -> None:
    """A shared (Postgres) test database must start empty, or startup recovery would pick up
    executions left RUNNING by a previous test."""
    from app.db.models import Base
    from app.db.session import create_engine

    engine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def app(settings):
    if not settings.database_url.startswith("sqlite"):
        await _reset_shared_database(settings.database_url)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        container = app.state.container
        # Deliver webhooks to this same app (the dev sink) without a network.
        loopback = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        container.notifications._http = loopback
        yield app
        await container.tasks.wait_all(timeout=10)
        await loopback.aclose()


@pytest.fixture
def container(app):
    return app.state.container


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-API-Key": API_KEY}
    ) as c:
        yield c


SINK = "http://test/dev/webhook-sink"


async def connect_mock(
    client: httpx.AsyncClient,
    client_id: str = "DEMO1",
    scenario: dict[str, Any] | None = None,
    holdings: dict[str, int] | None = None,
    api_key: str = API_KEY,
) -> str:
    creds: dict[str, Any] = {"client_id": client_id, "scenario": scenario or {}}
    if holdings:
        creds["initial_holdings"] = holdings
    r = await client.post(
        "/broker-connections", json={"broker": "mock", "credentials": creds}, headers={"X-API-Key": api_key}
    )
    assert r.status_code == 201, r.text
    return r.json()["connection_id"]


async def post_execution(client: httpx.AsyncClient, payload: dict[str, Any], key: str | None = None, **kw):
    return await client.post(
        "/executions", json=payload, headers={"Idempotency-Key": key or uuid.uuid4().hex, **kw.pop("headers", {})}, **kw
    )


async def run_execution(app, client: httpx.AsyncClient, payload: dict[str, Any], key: str | None = None) -> dict:
    r = await post_execution(client, payload, key)
    assert r.status_code == 202, r.text
    return await wait_done(app, client, r.json()["execution_id"])


async def wait_done(app, client: httpx.AsyncClient, execution_id: str) -> dict:
    await app.state.container.tasks.wait(execution_id, timeout=15)
    r = await client.get(f"/executions/{execution_id}")
    assert r.status_code == 200, r.text
    return r.json()


def by_symbol(execution: dict) -> dict[str, dict]:
    return {o["symbol"]: o for o in execution["orders"]}
