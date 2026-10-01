"""Gateway retry classification: the property that prevents duplicate trades."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.brokers.base import BrokerAdapter
from app.brokers.gateway import BrokerGateway
from app.brokers.ratelimit import RateLimiterRegistry, TokenBucket
from app.core.config import Settings
from app.domain.errors import (
    AmbiguousSubmission,
    BrokerProtocolError,
    BrokerRateLimited,
    BrokerUnavailable,
    LiveTradingDisabled,
    OrderRejected,
    ReauthRequired,
)
from app.domain.models import (
    AuthFlow,
    BrokerCapabilities,
    BrokerSession,
    OrderRequest,
    PlaceOrderAck,
    RateLimits,
    Side,
)

REQ = OrderRequest(symbol="TCS", side=Side.BUY, quantity=1, client_order_id="KXTEST")


class FakeAdapter(BrokerAdapter):
    name = "fake"
    capabilities = BrokerCapabilities(
        auth_flow=AuthFlow.CREDENTIALS, supports_refresh=True,
        rate_limits=RateLimits(orders_per_sec=1000, reads_per_sec=1000), is_simulator=True,
    )

    def __init__(self, place_effects=(), read_effects=(), refresh_ok=True, simulator=True):
        super().__init__(ctx=None, session=BrokerSession(access_token="t0", broker_user_id="u"))
        self.place_effects = list(place_effects)
        self.read_effects = list(read_effects)
        self.place_calls = 0
        self.read_calls = 0
        self.refresh_ok = refresh_ok
        self.refreshes = 0
        self.expired_token = None
        if not simulator:
            self.capabilities = BrokerCapabilities(auth_flow=AuthFlow.CREDENTIALS, supports_refresh=True,
                                                   is_simulator=False)

    async def _effect(self, effects):
        eff = effects.pop(0) if effects else "ok"
        if eff == "hang":
            await asyncio.sleep(10)
        if isinstance(eff, BaseException):
            raise eff
        return eff

    async def place_order(self, req):
        self.place_calls += 1
        token = self.session.access_token
        await asyncio.sleep(0)  # let concurrent callers interleave
        if token == self.expired_token:
            raise ReauthRequired("token expired")
        await self._effect(self.place_effects)
        return PlaceOrderAck(broker_order_id=f"B{self.place_calls}")

    async def list_orders(self):
        self.read_calls += 1
        await self._effect(self.read_effects)
        return []

    async def refresh_session(self):
        self.refreshes += 1
        if not self.refresh_ok:
            raise ReauthRequired("nope")
        return BrokerSession(access_token=f"t{self.refreshes}", broker_user_id="u")

    async def complete_login(self, params): ...
    async def validate_session(self): ...
    async def get_holdings(self): return []
    async def get_order(self, broker_order_id): ...
    def resolve_symbol(self, symbol, exchange="NSE"): ...


def gw(adapter, **settings_kw):
    settings = Settings(_env_file=None, app_env="test", retry_backoff_base_s=0.001, broker_max_retries=3, **settings_kw)
    events = {"refreshed": 0, "expired": 0}

    async def on_refreshed(_s):
        events["refreshed"] += 1

    async def on_expired():
        events["expired"] += 1

    g = BrokerGateway(adapter, settings=settings, limiters=RateLimiterRegistry(), account_key="acct",
                      on_session_refreshed=on_refreshed, on_session_expired=on_expired)
    return g, events


async def test_ambiguous_submission_is_never_retried():
    a = FakeAdapter(place_effects=[AmbiguousSubmission("read timeout")])
    g, _ = gw(a)
    with pytest.raises(AmbiguousSubmission):
        await g.place_order(REQ)
    assert a.place_calls == 1


@pytest.mark.parametrize("exc", [BrokerProtocolError("garbage"), ValueError("adapter bug")])
async def test_unclear_place_failures_become_ambiguous_and_are_not_retried(exc):
    a = FakeAdapter(place_effects=[exc])
    g, _ = gw(a)
    with pytest.raises(AmbiguousSubmission):
        await g.place_order(REQ)
    assert a.place_calls == 1


async def test_hung_place_order_is_ambiguous():
    a = FakeAdapter(place_effects=["hang"])
    g, _ = gw(a, broker_connect_timeout_s=0.01, broker_read_timeout_s=0.01)
    g._guard_s = 0.05
    with pytest.raises(AmbiguousSubmission):
        await g.place_order(REQ)
    assert a.place_calls == 1


async def test_not_placed_errors_are_retried():
    a = FakeAdapter(place_effects=[BrokerUnavailable(), BrokerRateLimited(retry_after=0.001)])
    g, _ = gw(a)
    result = await g.place_order(REQ)
    assert (result.attempts, result.ack.broker_order_id, a.place_calls) == (3, "B3", 3)


async def test_retries_are_bounded():
    a = FakeAdapter(place_effects=[BrokerUnavailable()] * 10)
    g, _ = gw(a)
    with pytest.raises(BrokerUnavailable) as info:
        await g.place_order(REQ)
    assert a.place_calls == 4 and info.value.attempts == 4


async def test_rejections_are_not_retried():
    a = FakeAdapter(place_effects=[OrderRejected("RMS")])
    g, _ = gw(a)
    with pytest.raises(OrderRejected):
        await g.place_order(REQ)
    assert a.place_calls == 1


async def test_reauth_refreshes_once_then_retries():
    a = FakeAdapter(place_effects=[ReauthRequired()])
    g, events = gw(a)
    result = await g.place_order(REQ)
    assert result.attempts == 2 and a.refreshes == 1 and events["refreshed"] == 1
    assert a.session.access_token == "t1"


async def test_failed_refresh_marks_expired_and_later_calls_fail_fast():
    a = FakeAdapter(place_effects=[ReauthRequired()], refresh_ok=False)
    g, events = gw(a)
    with pytest.raises(ReauthRequired):
        await g.place_order(REQ)
    assert events["expired"] == 1 and g.auth_failed
    with pytest.raises(ReauthRequired):
        await g.place_order(REQ)
    with pytest.raises(ReauthRequired):
        await g.list_orders()
    assert a.place_calls == 1 and a.read_calls == 0


async def test_concurrent_reauth_refreshes_only_once():
    a = FakeAdapter()
    a.expired_token = "t0"  # all three calls start with the dead token
    g, _ = gw(a)
    results = await asyncio.gather(*(g.place_order(REQ) for _ in range(3)))
    assert a.refreshes == 1
    assert all(r.attempts == 2 for r in results)


async def test_reads_retry_protocol_errors_and_timeouts():
    a = FakeAdapter(read_effects=[BrokerProtocolError("bad json"), BrokerUnavailable()])
    g, _ = gw(a)
    assert await g.list_orders() == []
    assert a.read_calls == 3


async def test_kill_switch_blocks_real_brokers_only():
    real = FakeAdapter(simulator=False)
    g, _ = gw(real)
    with pytest.raises(LiveTradingDisabled):
        await g.place_order(REQ)
    assert real.place_calls == 0
    g2, _ = gw(FakeAdapter(simulator=False), live_trading_enabled=True)
    assert (await g2.place_order(REQ)).attempts == 1


async def test_token_bucket_limits_rate():
    bucket = TokenBucket(rate_per_sec=50)
    start = time.monotonic()
    for _ in range(60):  # 50 burst + 10 at 50/s ≈ 0.2s
        await bucket.acquire()
    assert time.monotonic() - start >= 0.18
