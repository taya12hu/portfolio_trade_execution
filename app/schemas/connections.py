from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CreateConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker: str = Field(min_length=1, max_length=32)
    # Broker-specific login inputs. Used once for the token exchange and never stored
    # (except the mock's non-secret `scenario` / `initial_holdings` config).
    credentials: dict[str, Any] = Field(default_factory=dict)


class ConnectionOut(BaseModel):
    connection_id: str
    broker: str
    status: str
    broker_user_id: str | None
    token_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    login_url: str | None = None


class HoldingOut(BaseModel):
    symbol: str
    quantity: int
    avg_price: Decimal | None


class BrokerInfo(BaseModel):
    name: str
    auth_flow: str
    supports_refresh: bool
    is_simulator: bool
    live_enabled: bool
