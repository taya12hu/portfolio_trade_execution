"""BrokerGateway: the single place for cross-cutting broker concerns.

- rate limiting (token bucket per account, separate buckets for orders and reads)
- an overall timeout guard on every call
- retries of SAFE failures only:
    reads        -> retry BrokerUnavailable / BrokerRateLimited / BrokerProtocolError
    place_order  -> retry only BrokerUnavailable / BrokerRateLimited (the broker never took it)
  anything ambiguous on place_order (AmbiguousSubmission, guard timeout, unparseable response,
  unexpected exception) is raised as AmbiguousSubmission and NEVER retried
- one session refresh + retry on ReauthRequired (if the broker supports refresh); after a refresh
  fails, every later call fails fast without touching the broker
- the LIVE_TRADING_ENABLED kill switch for real brokers
- structured logging of every broker call
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from app.brokers.base import BrokerAdapter
from app.brokers.ratelimit import RateLimiterRegistry
from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.errors import (
    AmbiguousSubmission,
    BrokerError,
    BrokerProtocolError,
    BrokerRateLimited,
    BrokerUnavailable,
    LiveTradingDisabled,
    ReauthRequired,
)
from app.domain.models import BrokerInstrument, BrokerSession, Holding, OrderRequest, OrderSnapshot, PlaceOrderAck

log = get_logger(__name__)
T = TypeVar("T")

SessionCallback = Callable[[BrokerSession], Awaitable[None]]
ExpiredCallback = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class PlaceResult:
    ack: PlaceOrderAck
    attempts: int


class BrokerGateway:
    def __init__(
        self,
        adapter: BrokerAdapter,
        *,
        settings: Settings,
        limiters: RateLimiterRegistry,
        account_key: str,
        on_session_refreshed: SessionCallback | None = None,
        on_session_expired: ExpiredCallback | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.adapter = adapter
        self.settings = settings
        self.account_key = account_key
        self._on_refreshed = on_session_refreshed
        self._on_expired = on_session_expired
        self._sleep = sleep
        limits = adapter.capabilities.rate_limits
        self._order_bucket = limiters.get(account_key, "orders", limits.orders_per_sec)
        self._read_bucket = limiters.get(account_key, "reads", limits.reads_per_sec)
        self._refresh_lock = asyncio.Lock()
        self.auth_failed = False
        # Guard against an adapter call hanging forever; slightly above the HTTP timeouts.
        self._guard_s = settings.broker_connect_timeout_s + settings.broker_read_timeout_s + 1.0

    @property
    def broker(self) -> str:
        return self.adapter.name

    # ------------------------------------------------------------------ public API

    def ensure_can_trade(self) -> None:
        if not self.adapter.capabilities.is_simulator and not self.settings.live_trading_enabled:
            raise LiveTradingDisabled(
                f"live order placement is disabled for {self.broker} (set LIVE_TRADING_ENABLED=true)"
            )

    def resolve_symbol(self, symbol: str, exchange: str = "NSE") -> BrokerInstrument:
        return self.adapter.resolve_symbol(symbol, exchange)

    async def validate_session(self) -> None:
        await self._read("validate_session", self.adapter.validate_session)

    async def get_holdings(self) -> list[Holding]:
        return await self._read("get_holdings", self.adapter.get_holdings)

    async def list_orders(self) -> list[OrderSnapshot]:
        return await self._read("list_orders", self.adapter.list_orders)

    async def get_order(self, broker_order_id: str) -> OrderSnapshot:
        return await self._read("get_order", lambda: self.adapter.get_order(broker_order_id))

    async def place_order(self, req: OrderRequest) -> PlaceResult:
        self.ensure_can_trade()
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            self._fail_fast_if_auth_failed(attempt)
            await self._order_bucket.acquire()
            session_before = self.adapter.session
            started = time.perf_counter()
            try:
                ack = await asyncio.wait_for(self.adapter.place_order(req), timeout=self._guard_s)
            except asyncio.TimeoutError:
                err: BrokerError = AmbiguousSubmission("no response from broker within the guard timeout")
                self._log_call("place_order", started, attempt, err, req)
                raise self._with_attempts(err, attempt) from None
            except asyncio.CancelledError:
                raise
            except ReauthRequired as exc:
                self._log_call("place_order", started, attempt, exc, req)
                if not refreshed and await self._try_refresh(session_before):
                    refreshed = True
                    continue
                await self._mark_expired()
                raise self._with_attempts(exc, attempt)
            except (BrokerRateLimited, BrokerUnavailable) as exc:
                # The broker did not take the order: safe to retry.
                self._log_call("place_order", started, attempt, exc, req)
                if attempt > self.settings.broker_max_retries:
                    raise self._with_attempts(exc, attempt)
                await self._sleep(self._backoff(exc, attempt))
                continue
            except BrokerProtocolError as exc:
                # An unparseable answer to a write might hide an accepted order.
                err = AmbiguousSubmission(f"unexpected place_order response: {exc.message}")
                self._log_call("place_order", started, attempt, err, req)
                raise self._with_attempts(err, attempt) from exc
            except BrokerError as exc:  # AmbiguousSubmission, OrderRejected, InvalidInstrument, ...
                self._log_call("place_order", started, attempt, exc, req)
                raise self._with_attempts(exc, attempt)
            except Exception as exc:  # adapter bug: we cannot know whether it was sent
                err = AmbiguousSubmission(f"unexpected adapter error: {type(exc).__name__}")
                log.exception("broker.place_order.unexpected_error", broker=self.broker)
                raise self._with_attempts(err, attempt) from exc
            self._log_call("place_order", started, attempt, None, req, broker_order_id=ack.broker_order_id)
            return PlaceResult(ack=ack, attempts=attempt)

    # ------------------------------------------------------------------ internals

    async def _read(self, method: str, fn: Callable[[], Awaitable[T]]) -> T:
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            self._fail_fast_if_auth_failed(attempt)
            await self._read_bucket.acquire()
            session_before = self.adapter.session
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(fn(), timeout=self._guard_s)
            except asyncio.TimeoutError:
                exc_: BrokerError = BrokerUnavailable(f"{method} timed out")
                self._log_call(method, started, attempt, exc_)
                if attempt > self.settings.broker_max_retries:
                    raise exc_ from None
                await self._sleep(self._backoff(exc_, attempt))
                continue
            except ReauthRequired as exc:
                self._log_call(method, started, attempt, exc)
                if not refreshed and await self._try_refresh(session_before):
                    refreshed = True
                    continue
                await self._mark_expired()
                raise
            except (BrokerRateLimited, BrokerUnavailable, BrokerProtocolError) as exc:
                self._log_call(method, started, attempt, exc)
                if attempt > self.settings.broker_max_retries:
                    raise
                await self._sleep(self._backoff(exc, attempt))
                continue
            except BrokerError as exc:
                self._log_call(method, started, attempt, exc)
                raise
            self._log_call(method, started, attempt, None)
            return result

    def _fail_fast_if_auth_failed(self, attempt: int) -> None:
        if self.auth_failed:
            raise self._with_attempts(ReauthRequired("broker session expired earlier in this run"), attempt - 1)

    async def _try_refresh(self, session_before: BrokerSession | None) -> bool:
        """Returns True if the caller should retry with a (possibly concurrently) refreshed session."""
        async with self._refresh_lock:
            if self.adapter.session is not session_before and not self.auth_failed:
                return True  # another task refreshed while we waited
            if self.auth_failed or not self.adapter.supports_refresh:
                return False
            try:
                new_session = await asyncio.wait_for(self.adapter.refresh_session(), timeout=self._guard_s)
            except Exception as exc:  # any refresh failure means re-login is needed
                log.warning("broker.auth.refresh_failed", broker=self.broker, error_type=type(exc).__name__)
                return False
            self.adapter.session = new_session
            if self._on_refreshed:
                await self._on_refreshed(new_session)
            log.info("broker.auth.refreshed", broker=self.broker)
            return True

    async def _mark_expired(self) -> None:
        async with self._refresh_lock:
            if self.auth_failed:
                return
            self.auth_failed = True
        log.warning("broker.auth.expired", broker=self.broker)
        if self._on_expired:
            await self._on_expired()

    def _backoff(self, exc: BrokerError, attempt: int) -> float:
        if isinstance(exc, BrokerRateLimited) and exc.retry_after is not None:
            return min(max(exc.retry_after, 0.0), self.settings.max_retry_after_s)
        return min(self.settings.retry_backoff_base_s * (2 ** (attempt - 1)), self.settings.max_retry_after_s)

    @staticmethod
    def _with_attempts(exc: BrokerError, attempts: int) -> BrokerError:
        exc.attempts = attempts  # type: ignore[attr-defined]
        return exc

    def _log_call(
        self,
        method: str,
        started: float,
        attempt: int,
        error: BrokerError | None,
        req: OrderRequest | None = None,
        **extra: Any,
    ) -> None:
        fields: dict[str, Any] = {
            "broker": self.broker,
            "method": method,
            "attempt": attempt,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "outcome": "ok" if error is None else "error",
            **extra,
        }
        if req is not None:
            fields.update(
                client_order_id=req.client_order_id, symbol=req.symbol, side=req.side.value, quantity=req.quantity
            )
        if error is not None:
            fields.update(error_code=error.code, error_message=error.message)
            if isinstance(error, BrokerRateLimited):
                log.warning("broker.rate_limited", retry_after=error.retry_after, **fields)
                return
        log.info("broker.call", **fields)
