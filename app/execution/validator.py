"""Pure validation of execution requests. No I/O: holdings are passed in.

Two passes:
  1. `validate_structure` — rules that need only the payload (duplicates, conflicts, caps, empty).
  2. `validate_against_holdings` — rules that compare the instruction categories with what the
     account actually holds. The engine never computes deltas, so a category that contradicts the
     holdings means the caller's view is stale, and we fail closed.

Both return every problem found (not just the first), each tied to a field path and symbol.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from app.domain.models import InstructionType, Side
from app.schemas.executions import InitialExecutionRequest, RebalanceExecutionRequest

AnyExecutionRequest = InitialExecutionRequest | RebalanceExecutionRequest


@dataclass(frozen=True)
class Instruction:
    instruction_type: InstructionType
    symbol: str
    side: Side
    quantity: int  # always positive; ADJUST sign is carried by `side`
    field: str  # where it came from in the request, e.g. "adjust[2]"


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    field: str | None = None
    symbol: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def to_instructions(req: AnyExecutionRequest) -> list[Instruction]:
    if isinstance(req, InitialExecutionRequest):
        return [
            Instruction(InstructionType.INITIAL_BUY, item.symbol, Side.BUY, item.quantity, f"target[{i}]")
            for i, item in enumerate(req.target)
        ]
    out = [
        Instruction(InstructionType.SELL, item.symbol, Side.SELL, item.quantity, f"sell[{i}]")
        for i, item in enumerate(req.sell)
    ]
    out += [
        Instruction(InstructionType.BUY_NEW, item.symbol, Side.BUY, item.quantity, f"buy[{i}]")
        for i, item in enumerate(req.buy)
    ]
    out += [
        Instruction(
            InstructionType.ADJUST,
            item.symbol,
            Side.BUY if item.delta > 0 else Side.SELL,
            abs(item.delta),
            f"adjust[{i}]",
        )
        for i, item in enumerate(req.adjust)
    ]
    return out


def validate_structure(
    req: AnyExecutionRequest, *, max_qty_per_order: int, max_orders_per_execution: int
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if isinstance(req, InitialExecutionRequest):
        lists = {"target": [i.symbol for i in req.target]}
        if not req.target:
            issues.append(ValidationIssue("EMPTY_PORTFOLIO", "target must contain at least one symbol", "target"))
    else:
        lists = {
            "sell": [i.symbol for i in req.sell],
            "buy": [i.symbol for i in req.buy],
            "adjust": [i.symbol for i in req.adjust],
        }
        if not (req.sell or req.buy or req.adjust):
            issues.append(
                ValidationIssue("EMPTY_INSTRUCTIONS", "at least one of sell, buy or adjust must be non-empty")
            )

    # Duplicates within one list are never merged silently.
    for name, symbols in lists.items():
        seen: set[str] = set()
        for idx, sym in enumerate(symbols):
            if sym in seen:
                issues.append(
                    ValidationIssue("DUPLICATE_SYMBOL", f"{sym} appears more than once in {name}", f"{name}[{idx}]", sym)
                )
            seen.add(sym)

    # A symbol may appear in only one instruction category.
    owners: dict[str, list[str]] = {}
    for name, symbols in lists.items():
        for sym in dict.fromkeys(symbols):
            owners.setdefault(sym, []).append(name)
    for sym, names in owners.items():
        if len(names) > 1:
            issues.append(
                ValidationIssue(
                    "CONFLICTING_INSTRUCTIONS", f"{sym} appears in more than one of: {', '.join(names)}", None, sym
                )
            )

    instructions = to_instructions(req)
    for ins in instructions:
        if ins.quantity > max_qty_per_order:
            issues.append(
                ValidationIssue(
                    "QUANTITY_LIMIT_EXCEEDED",
                    f"quantity {ins.quantity} exceeds the per-order cap of {max_qty_per_order}",
                    ins.field,
                    ins.symbol,
                )
            )
    if len(instructions) > max_orders_per_execution:
        issues.append(
            ValidationIssue(
                "TOO_MANY_ORDERS",
                f"{len(instructions)} orders exceeds the per-execution cap of {max_orders_per_execution}",
            )
        )
    return issues


def validate_against_holdings(
    instructions: list[Instruction], holdings: Mapping[str, int]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for ins in instructions:
        held = holdings.get(ins.symbol, 0)
        t = ins.instruction_type
        if t in (InstructionType.INITIAL_BUY, InstructionType.BUY_NEW):
            if held > 0:
                hint = (
                    "use a REBALANCE with adjust instead"
                    if t is InstructionType.INITIAL_BUY
                    else "use adjust to change an existing position"
                )
                issues.append(
                    ValidationIssue(
                        "ALREADY_HELD",
                        f"{ins.symbol} is already held ({held}); buying the full quantity would overshoot; {hint}",
                        ins.field,
                        ins.symbol,
                    )
                )
        elif t is InstructionType.SELL or (t is InstructionType.ADJUST and ins.side is Side.SELL):
            if held == 0:
                issues.append(ValidationIssue("NOT_HELD", f"{ins.symbol} is not held", ins.field, ins.symbol))
            elif ins.quantity > held:
                issues.append(
                    ValidationIssue(
                        "INSUFFICIENT_HOLDINGS",
                        f"cannot sell {ins.quantity} {ins.symbol}; only {held} held",
                        ins.field,
                        ins.symbol,
                    )
                )
        elif t is InstructionType.ADJUST and held == 0:
            issues.append(
                ValidationIssue(
                    "NOT_HELD", f"{ins.symbol} is not held; use buy for new positions", ins.field, ins.symbol
                )
            )
    return issues
