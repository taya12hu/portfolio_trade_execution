"""Adapter contract suite: every real adapter must satisfy the same behaviour. Adding a broker means
adding a BrokerSpec to fixtures.ALL — passing this suite is the definition of done."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.container import _ssl_context

from app.brokers.base import AdapterContext
from app.brokers.registry import create_adapter
from app.core.config import Settings
from app.domain.errors import (
    AmbiguousSubmission,
    BrokerProtocolError,
    BrokerRateLimited,
    BrokerUnavailable,
    InvalidInstrument,
    OrderRejected,
    ReauthRequired,
)
from app.domain.models import OrderRequest, Side
from tests.adapters.fixtures import ALL, SYMBOL, TAG, BrokerSpec

REQ = OrderRequest(symbol=SYMBOL, side=Side.BUY, quantity=5, client_order_id=TAG)
specs = pytest.mark.parametrize("spec", ALL, ids=[s.name for s in ALL])


@pytest.fixture
async def http():
    async with httpx.AsyncClient(verify=_ssl_context()) as client:  # reuse the process-wide context
        yield client


def adapter_for(spec: BrokerSpec, http: httpx.AsyncClient, **settings_kw):
    settings = Settings(app_env="test", **{**spec.settings, **settings_kw})
    return create_adapter(spec.name, AdapterContext(settings=settings, http=http), session=spec.session)


# ------------------------------------------------------------------ place_order

@specs
async def test_place_sends_the_tag_and_returns_the_broker_id(spec, http):
    with respx.mock() as router:
        route = router.post(spec.place_url).mock(return_value=httpx.Response(200, json=spec.place_ok))
        ack = await adapter_for(spec, http).place_order(REQ)
    assert ack.broker_order_id == spec.expected_order_id
    spec.check_place(route.calls.last.request)


@specs
async def test_place_429_is_rate_limited_and_honours_retry_after(spec, http):
    with respx.mock() as router:
        router.post(spec.place_url).mock(return_value=httpx.Response(429, headers={"Retry-After": "2"}, json={}))
        with pytest.raises(BrokerRateLimited) as info:
            await adapter_for(spec, http).place_order(REQ)
    assert info.value.retry_after == 2.0
    assert info.value.placed is False


@specs
@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow connect")])
async def test_place_failures_before_sending_are_not_placed(spec, http, exc):
    with respx.mock() as router:
        router.post(spec.place_url).mock(side_effect=exc)
        with pytest.raises(BrokerUnavailable):
            await adapter_for(spec, http).place_order(REQ)


@specs
@pytest.mark.parametrize("exc", [httpx.ReadTimeout("no answer"), httpx.RemoteProtocolError("dropped"),
                                 httpx.WriteTimeout("half sent")])
async def test_place_failures_after_sending_are_ambiguous(spec, http, exc):
    with respx.mock() as router:
        router.post(spec.place_url).mock(side_effect=exc)
        with pytest.raises(AmbiguousSubmission):
            await adapter_for(spec, http).place_order(REQ)


@specs
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_place_5xx_is_ambiguous(spec, http, status):
    with respx.mock() as router:
        router.post(spec.place_url).mock(return_value=httpx.Response(status, text="<html>Bad gateway</html>"))
        with pytest.raises(AmbiguousSubmission):
            await adapter_for(spec, http).place_order(REQ)


@specs
async def test_place_unparseable_or_incomplete_success_is_ambiguous(spec, http):
    with respx.mock() as router:
        router.post(spec.place_url).mock(return_value=httpx.Response(200, text="not json"))
        with pytest.raises(AmbiguousSubmission):
            await adapter_for(spec, http).place_order(REQ)
    with respx.mock() as router:
        router.post(spec.place_url).mock(return_value=httpx.Response(200, json=spec.place_without_id))
        with pytest.raises(AmbiguousSubmission):
            await adapter_for(spec, http).place_order(REQ)


@specs
async def test_place_rejection_is_final_and_keeps_the_reason(spec, http):
    status, body = spec.reject
    with respx.mock() as router:
        router.post(spec.place_url).mock(return_value=httpx.Response(status, json=body))
        with pytest.raises(OrderRejected) as info:
            await adapter_for(spec, http).place_order(REQ)
    assert info.value.message and info.value.message != "Order rejected"
    assert info.value.placed is True


@specs
async def test_place_with_a_dead_token_needs_reauth(spec, http):
    status, body = spec.auth_fail
    with respx.mock() as router:
        router.post(spec.place_url).mock(return_value=httpx.Response(status, json=body))
        with pytest.raises(ReauthRequired):
            await adapter_for(spec, http).place_order(REQ)


# ------------------------------------------------------------------ reads

@specs
async def test_order_book_mapping(spec, http):
    with respx.mock() as router:
        router.get(spec.orders_url).mock(return_value=httpx.Response(200, json=spec.orders_body))
        snaps = await adapter_for(spec, http).list_orders()
    assert [(s.broker_order_id, s.client_order_id, s.status, s.filled_qty) for s in snaps] == spec.expected_orders
    first = snaps[0]
    assert first.symbol == "SBIN" and first.side is Side.BUY and first.quantity == 5
    assert first.avg_price is not None


@specs
@pytest.mark.parametrize("response,expected", [
    (httpx.Response(503, text="down"), BrokerUnavailable),
    (httpx.Response(200, text="<html>"), BrokerProtocolError),
])
async def test_read_failures_are_retryable_errors(spec, http, response, expected):
    with respx.mock() as router:
        router.get(spec.orders_url).mock(return_value=response)
        with pytest.raises(expected):
            await adapter_for(spec, http).list_orders()


@specs
async def test_read_timeout_is_unavailable_not_ambiguous(spec, http):
    with respx.mock() as router:
        router.get(spec.orders_url).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(BrokerUnavailable):
            await adapter_for(spec, http).list_orders()


@specs
async def test_holdings_mapping(spec, http):
    with respx.mock() as router:
        router.get(spec.holdings_url).mock(return_value=httpx.Response(200, json=spec.holdings_body))
        holdings = await adapter_for(spec, http).get_holdings()
    assert [(h.symbol, h.quantity) for h in holdings] == spec.expected_holdings


@pytest.fixture
def seed_instrument_map(monkeypatch):
    """An unverified map like the hand-curated seed, independent of the generated data file."""
    from app.brokers import instruments

    data = (False, {"SBIN": {"isin": "INE062A01020", "token": "3045", "source": "angelone-docs"},
                    "TCS": {"isin": "INE467B01029", "token": "11536", "source": "curated"}})
    monkeypatch.setattr(instruments, "_load", lambda: data)


@specs
async def test_symbol_resolution(spec, http, seed_instrument_map):
    adapter = adapter_for(spec, http)
    assert adapter.resolve_symbol("SBIN").symbol == "SBIN"
    with pytest.raises(InvalidInstrument):
        adapter.resolve_symbol("SBIN", "MCX")
    if spec.uses_instrument_map:
        with pytest.raises(InvalidInstrument):
            adapter.resolve_symbol("NOTINMAP")
        # Unverified map entries are refused once live trading is on.
        with pytest.raises(InvalidInstrument, match="unverified"):
            adapter_for(spec, http, live_trading_enabled=True).resolve_symbol("TCS")
        adapter_for(spec, http, live_trading_enabled=True).resolve_symbol("SBIN")  # doc-verified entry


@specs
async def test_tags_fit_the_broker_limit(spec):
    from app.brokers.registry import get_adapter_class
    from app.execution.planner import new_client_order_id

    assert len(new_client_order_id()) <= get_adapter_class(spec.name).capabilities.max_tag_len


def test_generated_instrument_map_is_verified_and_complete():
    from app.brokers import instruments

    eq = instruments.lookup("KOTAKBANK", live_trading=True)
    assert (eq.isin, eq.token, eq.verified) == ("INE237A01036", "1922", True)
    assert instruments.lookup("SBIN", live_trading=True).token == "3045"
