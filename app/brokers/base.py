"""The broker adapter contract.

Adapters are thin translators: canonical request -> broker payload, broker response -> canonical
types, broker failure -> canonical error (`app.domain.errors`). They must not retry, sleep, rate
limit or touch the database; `BrokerGateway` does all of that once for every broker.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from app.core.config import Settings
from app.domain.errors import ReauthRequired
from app.domain.models import (
    BrokerCapabilities,
    BrokerInstrument,
    BrokerSession,
    Holding,
    OrderRequest,
    OrderSnapshot,
    PlaceOrderAck,
)


@dataclass
class AdapterContext:
    settings: Settings
    http: httpx.AsyncClient
    mock_exchange: Any = None  # app.brokers.mock.exchange.MockExchange; only the mock uses it


class BrokerAdapter(ABC):
    name: ClassVar[str]
    capabilities: ClassVar[BrokerCapabilities]

    def __init__(
        self, ctx: AdapterContext, session: BrokerSession | None = None, config: dict[str, Any] | None = None
    ) -> None:
        self.ctx = ctx
        self.session = session
        self.config = config or {}

    @property
    def supports_refresh(self) -> bool:
        return self.capabilities.supports_refresh

    def _require_session(self) -> BrokerSession:
        if self.session is None:
            raise ReauthRequired("no broker session")
        return self.session

    # ---- auth ----
    def login_url(self, state: str) -> str | None:
        """Browser login URL for REDIRECT brokers; None for CREDENTIALS brokers."""
        return None

    @abstractmethod
    async def complete_login(self, params: dict[str, Any]) -> BrokerSession:
        """Exchange a redirect code/request token, or credentials (+TOTP), for a session.
        Raises InvalidCredentials / BrokerUnavailable."""

    def connection_config(self, params: dict[str, Any]) -> dict[str, Any]:
        """Non-secret per-connection config to persist from the login params (default: none)."""
        return {}

    async def refresh_session(self) -> BrokerSession:
        raise ReauthRequired(f"{self.name} does not support session refresh")

    @abstractmethod
    async def validate_session(self) -> None:
        """A cheap authenticated call (profile/funds). Raises ReauthRequired if the token is dead."""

    # ---- reads ----
    @abstractmethod
    async def get_holdings(self) -> list[Holding]: ...

    @abstractmethod
    async def get_order(self, broker_order_id: str) -> OrderSnapshot: ...

    @abstractmethod
    async def list_orders(self) -> list[OrderSnapshot]:
        """Today's order book. Used for batch status polling and tag reconciliation."""

    @abstractmethod
    def resolve_symbol(self, symbol: str, exchange: str = "NSE") -> BrokerInstrument:
        """Canonical NSE symbol -> broker instrument. Raises InvalidInstrument."""

    # ---- writes ----
    @abstractmethod
    async def place_order(self, req: OrderRequest) -> PlaceOrderAck:
        """Must send `req.client_order_id` as the broker order tag. Must raise AmbiguousSubmission
        (never BrokerUnavailable) for anything that happens after the request may have been sent."""

    async def cancel_order(self, broker_order_id: str) -> None:  # interface only; unused by the engine
        raise NotImplementedError
