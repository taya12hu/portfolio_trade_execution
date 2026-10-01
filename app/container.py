"""Wires the application's long-lived objects together (one per app instance)."""

from __future__ import annotations

import ssl
from collections import deque
from functools import lru_cache
from typing import Any

import certifi
import httpx

from app.brokers.base import AdapterContext
from app.brokers.mock.exchange import MockExchange
from app.brokers.ratelimit import RateLimiterRegistry
from app.brokers.registry import load_brokers
from app.connections.service import ConnectionService
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import TokenVault
from app.db.session import create_engine, create_sessionmaker, init_db
from app.execution.recovery import recover_interrupted_executions
from app.execution.runner import ExecutionRunner
from app.execution.service import ExecutionService
from app.execution.tasks import TaskRegistry
from app.notifications.service import NotificationService

log = get_logger(__name__)


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    # Building a CA bundle context is slow (~0.3s); do it once per process and share it.
    return ssl.create_default_context(cafile=certifi.where())


class Container:
    def __init__(self, settings: Settings) -> None:
        load_brokers()
        self.settings = settings
        self.engine = create_engine(settings.database_url)
        self.sessionmaker = create_sessionmaker(self.engine)
        self.vault = TokenVault(settings.token_encryption_key, allow_ephemeral=settings.is_dev)
        self.limiters = RateLimiterRegistry()
        self.mock_exchange = MockExchange()
        # Separate connect vs read timeouts: a connect failure means "not sent", a read timeout is ambiguous.
        self.broker_http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.broker_connect_timeout_s,
                read=settings.broker_read_timeout_s,
                write=settings.broker_read_timeout_s,
                pool=settings.broker_connect_timeout_s,
            ),
            follow_redirects=False,
            verify=_ssl_context(),
        )
        self.webhook_http = httpx.AsyncClient(follow_redirects=False, verify=_ssl_context())
        self.adapter_ctx = AdapterContext(settings=settings, http=self.broker_http, mock_exchange=self.mock_exchange)
        self.tasks = TaskRegistry()
        self.connections = ConnectionService(self.sessionmaker, self.vault, settings, self.adapter_ctx, self.limiters)
        self.notifications = NotificationService(self.sessionmaker, settings, self.webhook_http)
        self.runner = ExecutionRunner(self.sessionmaker, self.connections, self.notifications, settings, self.tasks)
        self.executions = ExecutionService(
            self.sessionmaker, self.connections, self.runner, self.notifications, settings
        )
        self.webhook_sink: deque[dict[str, Any]] = deque(maxlen=200)

    async def startup(self) -> None:
        await init_db(self.engine)
        recovered = await recover_interrupted_executions(self.sessionmaker, self.runner)
        log.info("app.started", env=self.settings.app_env, live_trading=self.settings.live_trading_enabled,
                 recovered_executions=len(recovered))

    async def shutdown(self, grace_s: float = 5.0) -> None:
        await self.tasks.shutdown(grace_s)
        await self.broker_http.aclose()
        await self.webhook_http.aclose()
        await self.engine.dispose()
