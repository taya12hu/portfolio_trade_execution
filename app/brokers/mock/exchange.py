"""In-memory exchange simulator backing the `mock` broker.

One `MockAccount` per broker client id: holdings, optional funds, an order book and a counter of
every place_order call the "broker" received (so tests can prove nothing was sent twice).

Orders evolve lazily: each order has a target state and a `ready_at` time; reads advance any order
whose time has come and apply fills to holdings and funds. State lives in process memory only.
"""

from __future__ import annotations

import itertools
import secrets
import time
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from app.domain.models import OrderState, Side


def mock_price(symbol: str) -> Decimal:
    """Deterministic pseudo price per symbol, 100.00 – 4999.95."""
    h = zlib.crc32(symbol.encode())
    return Decimal(100 + h % 4900) + Decimal(h % 20) * Decimal("0.05")


@dataclass
class MockOrder:
    order_id: str
    tag: str
    symbol: str
    side: Side
    quantity: int
    placed_at: datetime
    status: OrderState = OrderState.OPEN
    filled: int = 0
    avg_price: Decimal | None = None
    message: str | None = None
    # lazily applied outcome
    target_status: OrderState = OrderState.FILLED
    target_filled: int = 0
    target_message: str | None = None
    ready_at: float = 0.0
    settled: bool = False


@dataclass
class MockAccount:
    client_id: str
    holdings: dict[str, int] = field(default_factory=dict)
    avg_prices: dict[str, Decimal] = field(default_factory=dict)
    funds: Decimal | None = None  # None = unlimited
    orders: dict[str, MockOrder] = field(default_factory=dict)
    place_calls: int = 0  # every place_order request that reached the broker, whatever the outcome
    rate_limited_so_far: int = 0
    reads_failed_so_far: int = 0
    orders_accepted: int = 0
    valid_tokens: set[str] = field(default_factory=set)
    refresh_tokens: set[str] = field(default_factory=set)
    events: list[tuple[str, str, str]] = field(default_factory=list)  # (event, order_id, side)

    def orders_with_tag(self, tag: str) -> list[MockOrder]:
        return [o for o in self.orders.values() if o.tag == tag]


class MockExchange:
    def __init__(self, clock=time.monotonic) -> None:
        self.accounts: dict[str, MockAccount] = {}
        self._clock = clock
        self._ids = itertools.count(250_000_000_000_001)

    def account(self, client_id: str) -> MockAccount:
        acct = self.accounts.get(client_id)
        if acct is None:
            acct = MockAccount(client_id=client_id)
            self.accounts[client_id] = acct
        return acct

    def issue_session(self, acct: MockAccount) -> tuple[str, str]:
        access, refresh = "mock-at-" + secrets.token_hex(12), "mock-rt-" + secrets.token_hex(12)
        acct.valid_tokens.add(access)
        acct.refresh_tokens.add(refresh)
        return access, refresh

    def create_order(
        self,
        acct: MockAccount,
        *,
        tag: str,
        symbol: str,
        side: Side,
        quantity: int,
        target_status: OrderState,
        target_filled: int,
        target_message: str | None,
        delay_s: float,
    ) -> MockOrder:
        order = MockOrder(
            order_id=str(next(self._ids)),
            tag=tag,
            symbol=symbol,
            side=side,
            quantity=quantity,
            placed_at=datetime.now(UTC),
            target_status=target_status,
            target_filled=target_filled,
            target_message=target_message,
            ready_at=self._clock() + delay_s,
        )
        acct.orders[order.order_id] = order
        acct.orders_accepted += 1
        acct.events.append(("placed", order.order_id, side.value))
        self.advance(acct)
        return order

    def advance(self, acct: MockAccount) -> None:
        now = self._clock()
        for order in acct.orders.values():
            if order.settled or now < order.ready_at:
                continue
            self._settle(acct, order)

    def _settle(self, acct: MockAccount, order: MockOrder) -> None:
        order.settled = True
        status, filled, message = order.target_status, order.target_filled, order.target_message
        price = mock_price(order.symbol)

        # Exchange-side risk checks at execution time, like a real RMS.
        if filled > 0 and order.side is Side.SELL and acct.holdings.get(order.symbol, 0) < filled:
            status, filled, message = OrderState.REJECTED, 0, "RMS: insufficient holdings to sell"
        if filled > 0 and order.side is Side.BUY and acct.funds is not None and price * filled > acct.funds:
            status, filled, message = OrderState.REJECTED, 0, "RMS: insufficient funds"

        order.status, order.filled, order.message = status, filled, message
        if filled > 0:
            order.avg_price = price
            self._apply_fill(acct, order.symbol, order.side, filled, price)
        acct.events.append(("settled", order.order_id, order.side.value))

    @staticmethod
    def _apply_fill(acct: MockAccount, symbol: str, side: Side, qty: int, price: Decimal) -> None:
        held = acct.holdings.get(symbol, 0)
        if side is Side.BUY:
            prev_avg = acct.avg_prices.get(symbol, price)
            acct.avg_prices[symbol] = ((prev_avg * held) + price * qty) / (held + qty)
            acct.holdings[symbol] = held + qty
            if acct.funds is not None:
                acct.funds -= price * qty
        else:
            acct.holdings[symbol] = held - qty
            if acct.holdings[symbol] == 0:
                acct.holdings.pop(symbol)
                acct.avg_prices.pop(symbol, None)
            if acct.funds is not None:
                acct.funds += price * qty
