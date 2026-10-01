"""Pure planning: validated instructions -> ordered order legs."""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass

from app.domain.models import PHASE_BUY, PHASE_SELL, InstructionType, Side
from app.execution.validator import Instruction

CLIENT_ORDER_ID_PREFIX = "KX"
CLIENT_ORDER_ID_LEN = 18  # fits the strictest broker tag limit we support (20 chars, alphanumeric)


@dataclass(frozen=True)
class PlannedOrder:
    seq: int
    phase: int
    instruction_type: InstructionType
    symbol: str
    side: Side
    quantity: int
    client_order_id: str


def new_client_order_id() -> str:
    """`KX` + 16 base32 chars (80 random bits), upper-case alphanumeric only."""
    body = base64.b32encode(uuid.uuid4().bytes).decode().rstrip("=")[:16]
    return CLIENT_ORDER_ID_PREFIX + body


def plan_orders(instructions: list[Instruction]) -> list[PlannedOrder]:
    """Every SELL goes in phase 1 and every BUY in phase 2, keeping the request order within each
    phase. Sequence numbers are global and stable."""
    sells = [i for i in instructions if i.side is Side.SELL]
    buys = [i for i in instructions if i.side is Side.BUY]
    planned: list[PlannedOrder] = []
    for phase, group in ((PHASE_SELL, sells), (PHASE_BUY, buys)):
        for ins in group:
            planned.append(
                PlannedOrder(
                    seq=len(planned) + 1,
                    phase=phase,
                    instruction_type=ins.instruction_type,
                    symbol=ins.symbol,
                    side=ins.side,
                    quantity=ins.quantity,
                    client_order_id=new_client_order_id(),
                )
            )
    return planned
