"""Execution-level outcome derived from its orders. Pure."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.domain.models import LIVE_ORDER_STATUSES, ExecutionStatus, OrderStatus


@dataclass(frozen=True)
class OrderOutcome:
    symbol: str
    side: str
    status: OrderStatus
    quantity: int
    filled_quantity: int


def aggregate(orders: Iterable[OrderOutcome]) -> tuple[ExecutionStatus, dict[str, Any]]:
    orders = list(orders)
    by_status = {s.value: 0 for s in OrderStatus}
    for o in orders:
        by_status[o.status.value] += 1

    partially_filled = sum(1 for o in orders if 0 < o.filled_quantity < o.quantity)
    still_open = sum(1 for o in orders if o.status in LIVE_ORDER_STATUSES)
    any_filled = any(o.filled_quantity > 0 for o in orders)

    if by_status[OrderStatus.UNKNOWN] > 0:
        status = ExecutionStatus.NEEDS_REVIEW
    elif orders and all(o.status is OrderStatus.FILLED for o in orders):
        status = ExecutionStatus.COMPLETED
    elif any_filled or still_open:
        status = ExecutionStatus.PARTIALLY_COMPLETED
    else:
        status = ExecutionStatus.FAILED

    summary = {
        "total": len(orders),
        "filled": by_status[OrderStatus.FILLED],
        "partially_filled": partially_filled,
        "open": still_open,
        "rejected": by_status[OrderStatus.REJECTED],
        "cancelled": by_status[OrderStatus.CANCELLED],
        "failed": by_status[OrderStatus.FAILED],
        "unknown": by_status[OrderStatus.UNKNOWN],
        "skipped": by_status[OrderStatus.SKIPPED],
        "by_status": {k: v for k, v in by_status.items() if v},
    }
    return status, summary
