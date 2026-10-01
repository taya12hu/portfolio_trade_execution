"""Post-execution notification. Always logged; also POSTed to `callback_url` when one was given.

Delivery is at-least-once: the consumer dedupes on `X-Event-Id`, which is stable for a given outcome
(retries and manual resends of the same outcome reuse it; a changed outcome after reconcile gets a
new one). The notification never changes the execution result.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import sign_payload
from app.db.models import Execution, Order, utcnow
from app.domain.models import NotificationStatus

log = get_logger(__name__)


def build_payload(execution: Execution, orders: list[Order]) -> dict[str, Any]:
    order_rows = [
        {
            "seq": o.seq,
            "symbol": o.symbol,
            "side": o.side,
            "instruction_type": o.instruction_type,
            "quantity": o.quantity,
            "filled_quantity": o.filled_quantity,
            "average_price": str(o.average_price) if o.average_price is not None else None,
            "status": o.status,
            "client_order_id": o.client_order_id,
            "broker_order_id": o.broker_order_id,
            "error_code": o.error_code,
            "error_message": o.error_message,
        }
        for o in orders
    ]
    outcome = {"status": execution.status, "summary": execution.summary, "orders": order_rows}
    digest = hashlib.sha256(json.dumps([execution.id, outcome], sort_keys=True, default=str).encode()).hexdigest()
    return {
        "event": "execution.finished",
        "event_id": f"evt_{digest[:24]}",
        "execution_id": execution.id,
        "connection_id": execution.connection_id,
        "broker": execution.broker,
        "mode": execution.mode,
        "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
        **outcome,
    }


class CallbackUrlRejected(ValueError):
    pass


async def check_callback_url(url: str, settings: Settings) -> None:
    """Basic SSRF protection outside dev: https only and no private/loopback/link-local targets.
    (DNS rebinding is not covered; see README limitations.)"""
    if settings.is_dev:
        return
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise CallbackUrlRejected("callback_url must use https")
    host = parts.hostname or ""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, parts.port or 443)
    except socket.gaierror as exc:
        raise CallbackUrlRejected("callback_url host does not resolve") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise CallbackUrlRejected("callback_url must not point to a private or reserved address")


class NotificationService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        settings: Settings,
        http: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._sm = sessionmaker
        self._settings = settings
        self._http = http
        self._sleep = sleep

    async def notify(self, execution_id: str) -> NotificationStatus:
        async with self._sm() as db:
            execution = await db.get(Execution, execution_id)
            orders = list(
                (await db.execute(select(Order).where(Order.execution_id == execution_id).order_by(Order.seq))).scalars()
            )
        payload = build_payload(execution, orders)
        log.info(
            "execution.summary",
            execution_id=execution_id,
            status=execution.status,
            summary=execution.summary,
            event_id=payload["event_id"],
        )
        if not execution.callback_url:
            await self._record(execution_id, NotificationStatus.SKIPPED, attempts=0)
            return NotificationStatus.SKIPPED

        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "portfolio-trade-execution/0.1",
            "X-Event-Id": payload["event_id"],
            "X-Signature": sign_payload(self._settings.webhook_secret, body),
        }
        attempts = 0
        for attempt in range(1, self._settings.webhook_max_attempts + 1):
            attempts = attempt
            try:
                resp = await self._http.post(
                    execution.callback_url, content=body, headers=headers, timeout=self._settings.webhook_timeout_s
                )
                if 200 <= resp.status_code < 300:
                    log.info("notification.sent", execution_id=execution_id, attempt=attempt, http_status=resp.status_code)
                    await self._record(execution_id, NotificationStatus.SENT, attempts)
                    return NotificationStatus.SENT
                log.warning("notification.failed", execution_id=execution_id, attempt=attempt, http_status=resp.status_code)
            except httpx.HTTPError as exc:
                log.warning("notification.failed", execution_id=execution_id, attempt=attempt, error_type=type(exc).__name__)
            if attempt < self._settings.webhook_max_attempts:
                await self._sleep(self._settings.webhook_backoff_base_s * (2 ** (attempt - 1)))
        await self._record(execution_id, NotificationStatus.FAILED, attempts)
        return NotificationStatus.FAILED

    async def _record(self, execution_id: str, status: NotificationStatus, attempts: int) -> None:
        async with self._sm() as db:
            execution = await db.get(Execution, execution_id)
            execution.notification_status = status.value
            execution.notification_attempts = (execution.notification_attempts or 0) + attempts
            if status is NotificationStatus.SENT:
                execution.notified_at = utcnow()
            await db.commit()
