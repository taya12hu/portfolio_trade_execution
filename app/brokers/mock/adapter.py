"""`mock` broker: a scenario-driven simulator implementing the full adapter contract.

Connect with:
    {"broker": "mock", "credentials": {
        "client_id": "DEMO1",
        "initial_holdings": {"INFY": 10},          # only applied the first time a client id is seen
        "scenario": {
            "default": "SUCCESS",
            "symbols": {"TCS": "REJECTED", "ITC": "TIMEOUT_AFTER_PLACE"},
            "rate_limit_first_n": 2,
            "fill_delay_s": 0.5
        }}}

Per-symbol behaviours: see `Behaviour`. Account-level knobs: see `MockScenario`.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.brokers.base import BrokerAdapter
from app.brokers.mock.exchange import MockAccount, MockExchange, MockOrder
from app.brokers.registry import register_broker
from app.domain.errors import (
    AmbiguousSubmission,
    BrokerProtocolError,
    BrokerRateLimited,
    BrokerUnavailable,
    InvalidConnectionParams,
    InvalidCredentials,
    InvalidInstrument,
    OrderRejected,
    ReauthRequired,
)
from app.domain.models import (
    AuthFlow,
    BrokerCapabilities,
    BrokerInstrument,
    BrokerSession,
    Holding,
    OrderRequest,
    OrderSnapshot,
    OrderState,
    PlaceOrderAck,
    RateLimits,
)

Behaviour = Literal[
    "SUCCESS",  # accepted, fills fully (after fill_delay_s)
    "REJECTED",  # accepted, then rejected by RMS (status REJECTED with a reason)
    "REJECTED_ON_PLACE",  # place_order itself returns an error
    "PARTIAL_FILL",  # half fills, the rest stays open
    "PENDING",  # accepted, never fills
    "CANCELLED",  # accepted, then cancelled by the exchange
    "TIMEOUT_AFTER_PLACE",  # order IS placed, but the response is lost (read timeout)
    "TIMEOUT_NOT_PLACED",  # request times out and the order never reaches the book
    "BROKER_DOWN",  # place_order fails with connection errors
    "INVALID_SYMBOL",  # symbol cannot be resolved
]


class MockScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: Behaviour = "SUCCESS"
    symbols: dict[str, Behaviour] = Field(default_factory=dict)
    fill_delay_s: float = Field(default=0.0, ge=0, le=300)
    rate_limit_first_n: int = Field(default=0, ge=0)  # first N place_order calls get 429
    retry_after_s: float = Field(default=0.05, ge=0, le=10)
    fail_reads_first_n: int = Field(default=0, ge=0)  # first N read calls fail (broker unavailable)
    session_expired: bool = False  # the token from login is already dead
    expire_session_after_orders: int | None = Field(default=None, ge=0)  # token dies mid-run
    supports_refresh: bool = False
    login: Literal["OK", "INVALID_CREDENTIALS", "BROKER_DOWN"] = "OK"
    funds: Decimal | None = Field(default=None, ge=0)  # None = unlimited
    latency_ms: int = Field(default=0, ge=0, le=30_000)

    def behaviour(self, symbol: str) -> Behaviour:
        return self.symbols.get(symbol, self.default)


_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")


def parse_scenario(raw: Any) -> MockScenario:
    try:
        scenario = MockScenario.model_validate(raw or {})
    except ValidationError as exc:
        raise InvalidConnectionParams(f"invalid mock scenario: {exc.errors()[0]['msg']}") from exc
    return scenario.model_copy(update={"symbols": {k.upper(): v for k, v in scenario.symbols.items()}})


@register_broker("mock")
class MockBrokerAdapter(BrokerAdapter):
    capabilities = BrokerCapabilities(
        auth_flow=AuthFlow.CREDENTIALS,
        supports_refresh=False,  # overridden per connection by scenario.supports_refresh
        supports_order_tag=True,
        max_tag_len=20,
        rate_limits=RateLimits(orders_per_sec=10, reads_per_sec=20),
        is_simulator=True,
    )

    @property
    def scenario(self) -> MockScenario:
        return parse_scenario(self.config.get("scenario"))

    @property
    def supports_refresh(self) -> bool:
        return self.scenario.supports_refresh

    @property
    def exchange(self) -> MockExchange:
        return self.ctx.mock_exchange

    # ---- auth ----
    def connection_config(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"scenario": parse_scenario(params.get("scenario")).model_dump(mode="json")}

    async def complete_login(self, params: dict[str, Any]) -> BrokerSession:
        client_id = str(params.get("client_id") or "").strip()
        if not client_id:
            raise InvalidConnectionParams("client_id is required")
        scenario = parse_scenario(params.get("scenario"))
        await self._latency(scenario)
        if scenario.login == "INVALID_CREDENTIALS":
            raise InvalidCredentials("invalid client id or PIN (simulated)")
        if scenario.login == "BROKER_DOWN":
            raise BrokerUnavailable("broker login service unavailable (simulated)")

        is_new = client_id not in self.exchange.accounts
        acct = self.exchange.account(client_id)
        if is_new:
            self._seed(acct, params.get("initial_holdings") or {}, scenario)
        elif scenario.funds is not None:
            acct.funds = scenario.funds
        acct.valid_tokens.clear()
        access, refresh = self.exchange.issue_session(acct)
        if scenario.session_expired:
            acct.valid_tokens.discard(access)
        return BrokerSession(
            access_token=access,
            refresh_token=refresh,
            broker_user_id=client_id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )

    @staticmethod
    def _seed(acct: MockAccount, holdings: Any, scenario: MockScenario) -> None:
        if not isinstance(holdings, dict):
            raise InvalidConnectionParams("initial_holdings must be an object of SYMBOL: quantity")
        for symbol, qty in holdings.items():
            if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                raise InvalidConnectionParams(f"initial_holdings[{symbol}] must be a positive integer")
            acct.holdings[str(symbol).upper()] = qty
        acct.funds = scenario.funds

    async def refresh_session(self) -> BrokerSession:
        session = self._require_session()
        acct = self._account()
        if not self.scenario.supports_refresh or session.refresh_token not in acct.refresh_tokens:
            raise ReauthRequired("refresh not possible")
        acct.refresh_tokens.discard(session.refresh_token)
        access, refresh = self.exchange.issue_session(acct)
        return BrokerSession(
            access_token=access,
            refresh_token=refresh,
            broker_user_id=session.broker_user_id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )

    async def validate_session(self) -> None:
        await self._authed_read()

    # ---- reads ----
    async def get_holdings(self) -> list[Holding]:
        acct = await self._authed_read()
        return [Holding(symbol=s, quantity=q, avg_price=acct.avg_prices.get(s)) for s, q in acct.holdings.items()]

    async def list_orders(self) -> list[OrderSnapshot]:
        acct = await self._authed_read()
        return [self._snapshot(o) for o in acct.orders.values()]

    async def get_order(self, broker_order_id: str) -> OrderSnapshot:
        acct = await self._authed_read()
        order = acct.orders.get(broker_order_id)
        if order is None:
            raise BrokerProtocolError(f"order {broker_order_id} not found")
        return self._snapshot(order)

    def resolve_symbol(self, symbol: str, exchange: str = "NSE") -> BrokerInstrument:
        if exchange != "NSE" or not _SYMBOL_RE.match(symbol) or self.scenario.behaviour(symbol) == "INVALID_SYMBOL":
            raise InvalidInstrument(f"unknown instrument {exchange}:{symbol}")
        return BrokerInstrument(symbol=symbol, exchange=exchange, broker_symbol=f"{symbol}-EQ")

    # ---- writes ----
    async def place_order(self, req: OrderRequest) -> PlaceOrderAck:
        scenario = self.scenario
        await self._latency(scenario)
        acct = self._account()
        acct.place_calls += 1
        self._check_token(acct)

        if acct.rate_limited_so_far < scenario.rate_limit_first_n:
            acct.rate_limited_so_far += 1
            raise BrokerRateLimited("Too many requests (simulated)", retry_after=scenario.retry_after_s)

        behaviour = scenario.behaviour(req.symbol)
        if behaviour == "BROKER_DOWN":
            raise BrokerUnavailable("connection refused (simulated)")
        if behaviour == "TIMEOUT_NOT_PLACED":
            raise AmbiguousSubmission("read timeout (simulated; order never reached the book)")
        if behaviour == "REJECTED_ON_PLACE":
            raise OrderRejected("Order validation failed: quantity freeze limit (simulated)")
        if behaviour == "INVALID_SYMBOL":
            raise OrderRejected(f"instrument {req.symbol} not tradable")

        status, filled, message = self._target(behaviour, req.quantity)
        order = self.exchange.create_order(
            acct,
            tag=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            quantity=req.quantity,
            target_status=status,
            target_filled=filled,
            target_message=message,
            delay_s=scenario.fill_delay_s,
        )
        n = scenario.expire_session_after_orders
        if n is not None and acct.orders_accepted >= n:
            acct.valid_tokens.clear()  # token dies after this order was accepted
        if behaviour == "TIMEOUT_AFTER_PLACE":
            raise AmbiguousSubmission("read timeout (simulated; order WAS placed)")
        return PlaceOrderAck(broker_order_id=order.order_id)

    # ---- helpers ----
    @staticmethod
    def _target(behaviour: Behaviour, qty: int) -> tuple[OrderState, int, str | None]:
        if behaviour == "REJECTED":
            return OrderState.REJECTED, 0, "RMS: margin exceeds available limit (simulated)"
        if behaviour == "PARTIAL_FILL":
            filled = qty // 2
            return (OrderState.PARTIALLY_FILLED if filled else OrderState.OPEN), filled, None
        if behaviour == "PENDING":
            return OrderState.OPEN, 0, None
        if behaviour == "CANCELLED":
            return OrderState.CANCELLED, 0, "Cancelled by exchange: outside price band (simulated)"
        return OrderState.FILLED, qty, None  # SUCCESS, TIMEOUT_AFTER_PLACE

    def _account(self) -> MockAccount:
        session = self._require_session()
        acct = self.exchange.accounts.get(session.broker_user_id)
        if acct is None:  # e.g. the process restarted and the simulator's memory is gone
            raise ReauthRequired("unknown session (simulator state was reset)")
        return acct

    def _check_token(self, acct: MockAccount) -> None:
        if self._require_session().access_token not in acct.valid_tokens:
            raise ReauthRequired("access token expired (simulated)")

    async def _authed_read(self) -> MockAccount:
        scenario = self.scenario
        await self._latency(scenario)
        acct = self._account()
        self._check_token(acct)
        if acct.reads_failed_so_far < scenario.fail_reads_first_n:
            acct.reads_failed_so_far += 1
            raise BrokerUnavailable("service unavailable (simulated)")
        self.exchange.advance(acct)
        return acct

    @staticmethod
    async def _latency(scenario: MockScenario) -> None:
        if scenario.latency_ms:
            await asyncio.sleep(scenario.latency_ms / 1000)

    @staticmethod
    def _snapshot(o: MockOrder) -> OrderSnapshot:
        return OrderSnapshot(
            broker_order_id=o.order_id,
            client_order_id=o.tag,
            status=o.status,
            symbol=o.symbol,
            side=o.side,
            quantity=o.quantity,
            filled_qty=o.filled,
            pending_qty=o.quantity - o.filled if o.status in (OrderState.OPEN, OrderState.PARTIALLY_FILLED) else 0,
            avg_price=o.avg_price,
            status_message=o.message,
            raw_status=o.status.value,
            placed_at=o.placed_at,
        )
