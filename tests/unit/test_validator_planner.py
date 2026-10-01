import re

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.models import PHASE_BUY, PHASE_SELL, ExecutionStatus, InstructionType, OrderStatus, Side
from app.execution.planner import new_client_order_id, plan_orders
from app.execution.status import OrderOutcome, aggregate
from app.execution.validator import to_instructions, validate_against_holdings, validate_structure
from app.schemas.executions import ExecutionRequest

parse = TypeAdapter(ExecutionRequest).validate_python
CAPS = {"max_qty_per_order": 1000, "max_orders_per_execution": 5}


def codes(issues):
    return sorted(i.code for i in issues)


# ---------- schema (shape) ----------

@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "INITIAL", "connection_id": "c", "target": [{"symbol": "TCS", "quantity": 0}]},
        {"mode": "INITIAL", "connection_id": "c", "target": [{"symbol": "TCS", "quantity": -3}]},
        {"mode": "INITIAL", "connection_id": "c", "target": [{"symbol": "TCS", "quantity": 2.5}]},
        {"mode": "INITIAL", "connection_id": "c", "target": [{"symbol": "TCS", "quantity": "5"}]},
        {"mode": "INITIAL", "connection_id": "c", "target": [{"symbol": "TCS", "quantity": 5.0}]},
        {"mode": "INITIAL", "connection_id": "c", "target": [{"symbol": "TC S", "quantity": 5}]},
        {"mode": "INITIAL", "connection_id": "c", "target": [{"symbol": "", "quantity": 5}]},
        {"mode": "REBALANCE", "connection_id": "c", "adjust": [{"symbol": "TCS", "delta": 0}]},
        {"mode": "SWAP", "connection_id": "c"},
        {"connection_id": "c", "target": []},
        {"mode": "REBALANCE", "connection_id": "c", "sells": []},  # typo'd field is rejected, not ignored
        {"mode": "INITIAL", "target": [{"symbol": "TCS", "quantity": 1}]},  # no connection
    ],
)
def test_schema_rejects_bad_shapes(payload):
    with pytest.raises(ValidationError):
        parse(payload)


def test_schema_normalises_symbol_case():
    req = parse({"mode": "INITIAL", "connection_id": "c", "target": [{"symbol": " reliance ", "quantity": 1}]})
    assert req.target[0].symbol == "RELIANCE"


def test_symbols_with_ampersand_and_hyphen_are_valid():
    req = parse({"mode": "INITIAL", "connection_id": "c", "target": [
        {"symbol": "M&M", "quantity": 1}, {"symbol": "BAJAJ-AUTO", "quantity": 1}]})
    assert [t.symbol for t in req.target] == ["M&M", "BAJAJ-AUTO"]


# ---------- structure ----------

def test_empty_initial_portfolio():
    req = parse({"mode": "INITIAL", "connection_id": "c", "target": []})
    assert codes(validate_structure(req, **CAPS)) == ["EMPTY_PORTFOLIO"]


def test_empty_rebalance():
    req = parse({"mode": "REBALANCE", "connection_id": "c"})
    assert codes(validate_structure(req, **CAPS)) == ["EMPTY_INSTRUCTIONS"]


def test_duplicate_symbols_are_reported_not_merged():
    req = parse({"mode": "INITIAL", "connection_id": "c", "target": [
        {"symbol": "TCS", "quantity": 1}, {"symbol": "tcs", "quantity": 2}]})
    issues = validate_structure(req, **CAPS)
    assert codes(issues) == ["DUPLICATE_SYMBOL"]
    assert issues[0].field == "target[1]"


def test_conflicting_categories():
    req = parse({"mode": "REBALANCE", "connection_id": "c",
                 "sell": [{"symbol": "TCS", "quantity": 1}],
                 "adjust": [{"symbol": "TCS", "delta": 2}]})
    issues = validate_structure(req, **CAPS)
    assert codes(issues) == ["CONFLICTING_INSTRUCTIONS"]
    assert issues[0].symbol == "TCS"


def test_caps():
    req = parse({"mode": "REBALANCE", "connection_id": "c",
                 "buy": [{"symbol": f"S{i}", "quantity": 1} for i in range(5)],
                 "adjust": [{"symbol": "BIG", "delta": -5000}]})
    assert codes(validate_structure(req, **CAPS)) == ["QUANTITY_LIMIT_EXCEEDED", "TOO_MANY_ORDERS"]


# ---------- holdings ----------

def test_initial_rejects_symbols_already_held_and_ignores_unrelated_holdings():
    req = parse({"mode": "INITIAL", "connection_id": "c", "target": [
        {"symbol": "TCS", "quantity": 1}, {"symbol": "INFY", "quantity": 1}]})
    issues = validate_against_holdings(to_instructions(req), {"TCS": 3, "WIPRO": 100})
    assert [(i.code, i.symbol) for i in issues] == [("ALREADY_HELD", "TCS")]


@pytest.mark.parametrize(
    "payload,holdings,expected",
    [
        ({"sell": [{"symbol": "TCS", "quantity": 5}]}, {"TCS": 5}, []),
        ({"sell": [{"symbol": "TCS", "quantity": 6}]}, {"TCS": 5}, ["INSUFFICIENT_HOLDINGS"]),
        ({"sell": [{"symbol": "TCS", "quantity": 1}]}, {}, ["NOT_HELD"]),
        ({"buy": [{"symbol": "TCS", "quantity": 1}]}, {"TCS": 1}, ["ALREADY_HELD"]),
        ({"buy": [{"symbol": "TCS", "quantity": 1}]}, {}, []),
        ({"adjust": [{"symbol": "TCS", "delta": 2}]}, {}, ["NOT_HELD"]),
        ({"adjust": [{"symbol": "TCS", "delta": 2}]}, {"TCS": 1}, []),
        ({"adjust": [{"symbol": "TCS", "delta": -2}]}, {"TCS": 1}, ["INSUFFICIENT_HOLDINGS"]),
        ({"adjust": [{"symbol": "TCS", "delta": -1}]}, {"TCS": 1}, []),
    ],
)
def test_rebalance_holdings_rules(payload, holdings, expected):
    req = parse({"mode": "REBALANCE", "connection_id": "c", **payload})
    assert codes(validate_against_holdings(to_instructions(req), holdings)) == expected


# ---------- planner ----------

def test_planner_puts_all_sells_before_buys_and_splits_adjust_by_sign():
    req = parse({"mode": "REBALANCE", "connection_id": "c",
                 "buy": [{"symbol": "HDFCBANK", "quantity": 4}],
                 "sell": [{"symbol": "INFY", "quantity": 8}],
                 "adjust": [{"symbol": "TCS", "delta": -2}, {"symbol": "RELIANCE", "delta": 5}]})
    legs = plan_orders(to_instructions(req))
    assert [(l.seq, l.phase, l.side, l.symbol, l.quantity, l.instruction_type) for l in legs] == [
        (1, PHASE_SELL, Side.SELL, "INFY", 8, InstructionType.SELL),
        (2, PHASE_SELL, Side.SELL, "TCS", 2, InstructionType.ADJUST),
        (3, PHASE_BUY, Side.BUY, "HDFCBANK", 4, InstructionType.BUY_NEW),
        (4, PHASE_BUY, Side.BUY, "RELIANCE", 5, InstructionType.ADJUST),
    ]
    assert len({l.client_order_id for l in legs}) == 4


def test_initial_plan_is_all_buys():
    req = parse({"mode": "INITIAL", "connection_id": "c", "target": [{"symbol": "TCS", "quantity": 1}]})
    [leg] = plan_orders(to_instructions(req))
    assert (leg.phase, leg.side, leg.instruction_type) == (PHASE_BUY, Side.BUY, InstructionType.INITIAL_BUY)


def test_client_order_id_format():
    ids = {new_client_order_id() for _ in range(2000)}
    assert len(ids) == 2000
    assert all(re.fullmatch(r"KX[A-Z2-7]{16}", i) for i in ids)


# ---------- aggregation ----------

def o(status, qty=10, filled=0):
    return OrderOutcome("X", "BUY", status, qty, filled)


@pytest.mark.parametrize(
    "orders,expected",
    [
        ([o(OrderStatus.FILLED, filled=10)] * 3, ExecutionStatus.COMPLETED),
        ([o(OrderStatus.FILLED, filled=10), o(OrderStatus.REJECTED)], ExecutionStatus.PARTIALLY_COMPLETED),
        ([o(OrderStatus.REJECTED), o(OrderStatus.FAILED)], ExecutionStatus.FAILED),
        ([o(OrderStatus.OPEN)], ExecutionStatus.PARTIALLY_COMPLETED),
        ([o(OrderStatus.CANCELLED, filled=4)], ExecutionStatus.PARTIALLY_COMPLETED),
        ([o(OrderStatus.FILLED, filled=10), o(OrderStatus.UNKNOWN)], ExecutionStatus.NEEDS_REVIEW),
        ([o(OrderStatus.SKIPPED)], ExecutionStatus.FAILED),
    ],
)
def test_aggregate_status(orders, expected):
    status, summary = aggregate(orders)
    assert status is expected
    assert summary["total"] == len(orders)


def test_summary_counts():
    _, s = aggregate([o(OrderStatus.FILLED, filled=10), o(OrderStatus.PARTIALLY_FILLED, filled=3),
                      o(OrderStatus.REJECTED), o(OrderStatus.UNKNOWN)])
    assert (s["filled"], s["partially_filled"], s["open"], s["rejected"], s["unknown"]) == (1, 1, 1, 1, 1)
