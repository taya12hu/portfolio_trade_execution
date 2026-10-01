"""Canonical types the engine works with. Adapters translate to and from these.

No FastAPI, SQLAlchemy or broker imports are allowed in `app.domain`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Mode(StrEnum):
    INITIAL = "INITIAL"
    REBALANCE = "REBALANCE"


class InstructionType(StrEnum):
    INITIAL_BUY = "INITIAL_BUY"
    SELL = "SELL"
    BUY_NEW = "BUY_NEW"
    ADJUST = "ADJUST"


# Orders run in phases: every SELL settles before the first BUY is placed.
PHASE_SELL = 1
PHASE_BUY = 2


class OrderState(StrEnum):
    """Order state as reported by a broker, after adapter normalisation."""

    OPEN = "OPEN"  # accepted, nothing filled yet (includes broker-side pending/validation states)
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # still live at the broker with some quantity filled
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"  # cancelled or expired; filled_qty may be > 0


class OrderStatus(StrEnum):
    """Our lifecycle for one order leg. Every transition is committed before the next broker call."""

    PENDING = "PENDING"  # planned, never sent
    SUBMITTING = "SUBMITTING"  # committed just before place_order(); outcome not yet known
    RECONCILING = "RECONCILING"  # place_order() outcome ambiguous; searching the order book by tag
    SUBMITTED = "SUBMITTED"  # broker acknowledged; no status snapshot yet
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"  # definitely not placed (e.g. auth expired, broker down, retries exhausted)
    UNKNOWN = "UNKNOWN"  # may or may not be placed; never auto-resent, needs review
    SKIPPED = "SKIPPED"  # never sent (e.g. server restarted before submission)


# The runner stops tracking an order once it reaches one of these.
TERMINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
        OrderStatus.UNKNOWN,
        OrderStatus.SKIPPED,
    }
)
# Placed at the broker and possibly still working there.
LIVE_ORDER_STATUSES = frozenset({OrderStatus.SUBMITTED, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED})


class ExecutionStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


ACTIVE_EXECUTION_STATUSES = frozenset({ExecutionStatus.ACCEPTED, ExecutionStatus.RUNNING})


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # no callback_url; the summary was only logged


class ConnectionStatus(StrEnum):
    PENDING_LOGIN = "PENDING_LOGIN"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class AuthFlow(StrEnum):
    REDIRECT = "REDIRECT"  # browser login, broker redirects back with a code
    CREDENTIALS = "CREDENTIALS"  # client id + PIN/TOTP (or key/secret) posted directly


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: int
    client_order_id: str
    exchange: str = "NSE"
    order_type: str = "MARKET"
    product: str = "CNC"
    validity: str = "DAY"


@dataclass(frozen=True)
class PlaceOrderAck:
    broker_order_id: str


@dataclass(frozen=True)
class OrderSnapshot:
    broker_order_id: str
    status: OrderState
    client_order_id: str | None = None  # the tag we sent, as echoed by the broker
    symbol: str | None = None
    side: Side | None = None
    quantity: int | None = None
    filled_qty: int = 0
    pending_qty: int | None = None
    avg_price: Decimal | None = None
    status_message: str | None = None
    raw_status: str | None = None
    placed_at: datetime | None = None


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: int  # sellable quantity (settled + T1)
    avg_price: Decimal | None = None


@dataclass(frozen=True)
class BrokerSession:
    access_token: str
    broker_user_id: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # broker-specific non-secret values


@dataclass(frozen=True)
class BrokerInstrument:
    symbol: str  # canonical NSE trading symbol
    exchange: str
    broker_symbol: str  # what the broker expects (e.g. "NSE:RELIANCE-EQ", "NSE_EQ|INE002A01018")
    token: str | None = None  # broker/exchange instrument token where needed


@dataclass(frozen=True)
class RateLimits:
    orders_per_sec: float = 10.0
    reads_per_sec: float = 5.0


@dataclass(frozen=True)
class BrokerCapabilities:
    auth_flow: AuthFlow
    supports_refresh: bool
    supports_order_tag: bool = True
    max_tag_len: int = 20
    rate_limits: RateLimits = field(default_factory=RateLimits)
    is_simulator: bool = False
