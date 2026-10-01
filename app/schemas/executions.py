"""API contract for executions. Shape validation lives here; cross-field and holdings rules live in
`app.execution.validator` so they can report every problem at once, per leg."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Union

from pydantic import AnyHttpUrl, BaseModel, BeforeValidator, ConfigDict, Field, field_validator

# Canonical symbol = NSE trading symbol, e.g. RELIANCE, M&M, BAJAJ-AUTO.
SYMBOL_PATTERN = r"^[A-Z0-9][A-Z0-9&\-]{0,19}$"


def _normalise_symbol(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


Symbol = Annotated[str, BeforeValidator(_normalise_symbol), Field(pattern=SYMBOL_PATTERN)]
# strict=True: 8.0, "8" and true are rejected rather than coerced.
Quantity = Annotated[int, Field(strict=True, gt=0)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuantityItem(_Strict):
    symbol: Symbol
    quantity: Quantity


class AdjustItem(_Strict):
    symbol: Symbol
    delta: Annotated[int, Field(strict=True)]

    @field_validator("delta")
    @classmethod
    def _non_zero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("delta must be non-zero")
        return v


class _ExecutionBase(_Strict):
    connection_id: Annotated[str, Field(min_length=1, max_length=64)]
    callback_url: AnyHttpUrl | None = None


class InitialExecutionRequest(_ExecutionBase):
    """First-time portfolio: buy every target quantity."""

    mode: Literal["INITIAL"]
    target: list[QuantityItem] = Field(default_factory=list)


class RebalanceExecutionRequest(_ExecutionBase):
    """Explicit rebalance instructions. The engine does not compute deltas."""

    mode: Literal["REBALANCE"]
    sell: list[QuantityItem] = Field(default_factory=list)  # exit or reduce an existing holding
    buy: list[QuantityItem] = Field(default_factory=list)  # BUY_NEW: symbols not currently held
    adjust: list[AdjustItem] = Field(default_factory=list)  # +n buys more, -n sells some


ExecutionRequest = Annotated[
    Union[InitialExecutionRequest, RebalanceExecutionRequest], Field(discriminator="mode")
]


class PlannedOrderOut(BaseModel):
    seq: int
    phase: int
    instruction_type: str
    symbol: str
    side: str
    quantity: int
    client_order_id: str | None = None


class OrderOut(BaseModel):
    id: str
    seq: int
    phase: int
    instruction_type: str
    symbol: str
    exchange: str
    side: str
    quantity: int
    status: str
    filled_quantity: int
    average_price: Decimal | None
    client_order_id: str
    broker_order_id: str | None
    attempts: int
    error_code: str | None
    error_message: str | None
    submitted_at: datetime | None
    completed_at: datetime | None


class NotificationOut(BaseModel):
    status: str
    attempts: int
    notified_at: datetime | None
    callback_url: str | None


class ExecutionOut(BaseModel):
    execution_id: str
    status: str
    mode: str
    connection_id: str
    broker: str
    error_code: str | None
    summary: dict[str, Any] | None
    notification: NotificationOut
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    status_url: str
    orders: list[OrderOut]


class ExecutionListItem(BaseModel):
    execution_id: str
    status: str
    mode: str
    connection_id: str
    summary: dict[str, Any] | None
    created_at: datetime
    finished_at: datetime | None


class ExecutionList(BaseModel):
    items: list[ExecutionListItem]
    limit: int
    offset: int


class PreviewOut(BaseModel):
    valid: bool
    orders: list[PlannedOrderOut]
    errors: list[dict[str, Any]]


class NotifyOut(BaseModel):
    execution_id: str
    notification_status: str
    attempts: int
