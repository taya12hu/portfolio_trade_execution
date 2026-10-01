"""Crash recovery: seed the DB as a dead process would have left it, then run startup recovery."""

from __future__ import annotations

from app.db.models import Execution, Order, OrderEvent, new_id, utcnow
from app.domain.models import OrderState, Side
from app.execution.recovery import recover_interrupted_executions
from tests.conftest import by_symbol, connect_mock, wait_done
from sqlalchemy import select


def _order(execution_id, seq, phase, symbol, side, status, client_order_id, broker_order_id=None):
    return Order(
        execution_id=execution_id, seq=seq, phase=phase, instruction_type="BUY_NEW" if side == "BUY" else "SELL",
        symbol=symbol, exchange="NSE", side=side, quantity=5, client_order_id=client_order_id,
        broker_order_id=broker_order_id, status=status, filled_quantity=0, attempts=1 if status != "PENDING" else 0,
        submitted_at=utcnow() if status != "PENDING" else None,
    )


async def test_recovery_reconciles_resumes_and_skips_without_sending(app, client, container):
    conn = await connect_mock(client, holdings={"INFY": 5, "WIPRO": 5})
    acct = container.mock_exchange.accounts["DEMO1"]
    ex_mod = container.mock_exchange

    # What the broker has: the SUBMITTING sell actually reached it; the SUBMITTED sell is working.
    ex_mod.create_order(acct, tag="KXSUBMITTINGFOUND1", symbol="INFY", side=Side.SELL, quantity=5,
                        target_status=OrderState.FILLED, target_filled=5, target_message=None, delay_s=0)
    live = ex_mod.create_order(acct, tag="KXSUBMITTEDLIVE001", symbol="WIPRO", side=Side.SELL, quantity=5,
                               target_status=OrderState.FILLED, target_filled=5, target_message=None, delay_s=0)
    calls_before = acct.place_calls

    execution_id = new_id()
    async with container.sessionmaker() as db:
        db.add(Execution(
            id=execution_id, owner_id="owner-a", connection_id=conn, broker="mock", idempotency_key="crash",
            request_hash="x", mode="REBALANCE", request_payload={}, status="RUNNING", started_at=utcnow(),
            notification_status="PENDING", notification_attempts=0,
        ))
        await db.flush()
        db.add_all([
            _order(execution_id, 1, 1, "INFY", "SELL", "SUBMITTING", "KXSUBMITTINGFOUND1"),
            _order(execution_id, 2, 1, "WIPRO", "SELL", "SUBMITTED", "KXSUBMITTEDLIVE001", live.order_id),
            _order(execution_id, 3, 2, "ITC", "BUY", "SUBMITTING", "KXSUBMITTINGLOST01"),
            _order(execution_id, 4, 2, "TCS", "BUY", "PENDING", "KXNEVERSENT0000001"),
        ])
        await db.commit()

    assert await recover_interrupted_executions(container.sessionmaker, container.runner) == [execution_id]
    ex = await wait_done(app, client, execution_id)
    o = by_symbol(ex)
    assert o["INFY"]["status"] == "FILLED"  # found by tag
    assert o["WIPRO"]["status"] == "FILLED"  # polling resumed
    assert (o["ITC"]["status"], o["ITC"]["error_code"]) == ("UNKNOWN", "OUTCOME_UNKNOWN")  # not found, not resent
    assert (o["TCS"]["status"], o["TCS"]["error_code"]) == ("SKIPPED", "SERVER_RESTARTED")
    assert ex["status"] == "NEEDS_REVIEW"
    assert acct.place_calls == calls_before  # recovery never sends an order

    async with container.sessionmaker() as db:
        events = list((await db.execute(select(OrderEvent).where(OrderEvent.execution_id == execution_id))).scalars())
    assert any(e.from_status == "SUBMITTING" and e.to_status == "RECONCILING" for e in events)


async def test_recovery_of_an_accepted_execution_skips_everything(app, client, container):
    conn = await connect_mock(client)
    execution_id = new_id()
    async with container.sessionmaker() as db:
        db.add(Execution(
            id=execution_id, owner_id="owner-a", connection_id=conn, broker="mock", idempotency_key="crash2",
            request_hash="x", mode="INITIAL", request_payload={}, status="ACCEPTED",
            notification_status="PENDING", notification_attempts=0,
        ))
        await db.flush()
        db.add(_order(execution_id, 1, 2, "TCS", "BUY", "PENDING", "KXNEVERSENT0000002"))
        await db.commit()
    await recover_interrupted_executions(container.sessionmaker, container.runner)
    ex = await wait_done(app, client, execution_id)
    assert ex["status"] == "FAILED"
    assert ex["orders"][0]["status"] == "SKIPPED"
    assert container.mock_exchange.accounts["DEMO1"].place_calls == 0
