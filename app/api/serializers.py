from __future__ import annotations

from app.db.models import BrokerConnection, Execution, Order
from app.execution.planner import PlannedOrder
from app.schemas.connections import ConnectionOut
from app.schemas.executions import (
    ExecutionListItem,
    ExecutionOut,
    NotificationOut,
    OrderOut,
    PlannedOrderOut,
)


def connection_out(row: BrokerConnection, login_url: str | None = None) -> ConnectionOut:
    # Deliberately field-by-field: tokens and config never leave the service.
    return ConnectionOut(
        connection_id=row.id,
        broker=row.broker,
        status=row.status,
        broker_user_id=row.broker_user_id,
        token_expires_at=row.token_expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        login_url=login_url,
    )


def order_out(o: Order) -> OrderOut:
    return OrderOut(
        id=o.id,
        seq=o.seq,
        phase=o.phase,
        instruction_type=o.instruction_type,
        symbol=o.symbol,
        exchange=o.exchange,
        side=o.side,
        quantity=o.quantity,
        status=o.status,
        filled_quantity=o.filled_quantity,
        average_price=o.average_price,
        client_order_id=o.client_order_id,
        broker_order_id=o.broker_order_id,
        attempts=o.attempts,
        error_code=o.error_code,
        error_message=o.error_message,
        submitted_at=o.submitted_at,
        completed_at=o.completed_at,
    )


def execution_out(e: Execution, orders: list[Order]) -> ExecutionOut:
    return ExecutionOut(
        execution_id=e.id,
        status=e.status,
        mode=e.mode,
        connection_id=e.connection_id,
        broker=e.broker,
        error_code=e.error_code,
        summary=e.summary,
        notification=NotificationOut(
            status=e.notification_status,
            attempts=e.notification_attempts,
            notified_at=e.notified_at,
            callback_url=e.callback_url,
        ),
        created_at=e.created_at,
        started_at=e.started_at,
        finished_at=e.finished_at,
        status_url=f"/executions/{e.id}",
        orders=[order_out(o) for o in orders],
    )


def execution_list_item(e: Execution) -> ExecutionListItem:
    return ExecutionListItem(
        execution_id=e.id,
        status=e.status,
        mode=e.mode,
        connection_id=e.connection_id,
        summary=e.summary,
        created_at=e.created_at,
        finished_at=e.finished_at,
    )


def planned_out(p: PlannedOrder) -> PlannedOrderOut:
    return PlannedOrderOut(
        seq=p.seq,
        phase=p.phase,
        instruction_type=p.instruction_type.value,
        symbol=p.symbol,
        side=p.side.value,
        quantity=p.quantity,
        client_order_id=None,  # assigned for real only when the execution is created
    )
