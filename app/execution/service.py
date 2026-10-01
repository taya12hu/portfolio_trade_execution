"""ExecutionService: the synchronous half of POST /executions.

idempotency lookup -> structural validation -> connection -> pre-flight against the broker
(session, symbols, holdings) -> plan -> ONE transaction (execution + orders) -> schedule the runner.

It never places orders itself.
"""

from __future__ import annotations

import hashlib
import json
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.brokers.gateway import BrokerGateway
from app.connections.service import ConnectionService, api_error_from_broker
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.logging import get_logger
from app.db.models import Execution, Order, new_id
from app.domain.errors import BrokerError, InvalidInstrument
from app.domain.models import ACTIVE_EXECUTION_STATUSES, ExecutionStatus, NotificationStatus, OrderStatus
from app.execution.planner import PlannedOrder, plan_orders
from app.execution.runner import ExecutionRunner
from app.execution.validator import (
    AnyExecutionRequest,
    Instruction,
    ValidationIssue,
    to_instructions,
    validate_against_holdings,
    validate_structure,
)
from app.notifications.service import CallbackUrlRejected, NotificationService, check_callback_url

log = get_logger(__name__)

_ACTIVE = [s.value for s in ACTIVE_EXECUTION_STATUSES]


def request_hash(req: AnyExecutionRequest) -> str:
    canonical = json.dumps(req.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _issues_error(issues: list[ValidationIssue]) -> ApiError:
    return ApiError(
        422, "INVALID_INSTRUCTIONS", "The instructions failed validation", [i.to_dict() for i in issues]
    )


class ExecutionService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        connections: ConnectionService,
        runner: ExecutionRunner,
        notifications: NotificationService,
        settings: Settings,
    ) -> None:
        self._sm = sessionmaker
        self._connections = connections
        self._runner = runner
        self._notifications = notifications
        self._settings = settings

    # ------------------------------------------------------------------ create

    async def create(
        self, owner_id: str, idempotency_key: str, req: AnyExecutionRequest
    ) -> tuple[Execution, list[Order], bool]:
        """Returns (execution, orders, replayed)."""
        req_hash = request_hash(req)
        existing = await self._by_key(owner_id, idempotency_key)
        if existing is not None:
            return await self._replay(existing, req_hash)

        issues = validate_structure(
            req,
            max_qty_per_order=self._settings.max_qty_per_order,
            max_orders_per_execution=self._settings.max_orders_per_execution,
        )
        if issues:
            log.info("execution.rejected_validation", issues=[i.code for i in issues])
            raise _issues_error(issues)
        if req.callback_url is not None:
            try:
                await check_callback_url(str(req.callback_url), self._settings)
            except CallbackUrlRejected as exc:
                raise ApiError(422, "INVALID_CALLBACK_URL", str(exc), [{"field": "callback_url"}]) from exc

        conn, gateway = await self._connections.gateway_for_owner(owner_id, req.connection_id)
        if await self._active_for(conn.id) is not None:
            return await self._replay_or_conflict(owner_id, idempotency_key, req_hash, conn.id)

        instructions = to_instructions(req)
        issues = await self._preflight(gateway, instructions)
        if issues:
            log.info("execution.rejected_validation", issues=[i.code for i in issues])
            raise _issues_error(issues)

        planned = plan_orders(instructions)
        execution = Execution(
            id=new_id(),
            owner_id=owner_id,
            connection_id=conn.id,
            broker=conn.broker,
            idempotency_key=idempotency_key,
            request_hash=req_hash,
            mode=req.mode,
            request_payload=req.model_dump(mode="json"),
            status=ExecutionStatus.ACCEPTED.value,
            callback_url=str(req.callback_url) if req.callback_url else None,
            notification_status=NotificationStatus.PENDING.value,
            notification_attempts=0,
        )
        orders = [self._order_row(execution.id, p) for p in planned]
        try:
            async with self._sm() as db:
                db.add(execution)
                await db.flush()  # execution row first: orders reference it
                db.add_all(orders)
                await db.commit()
        except IntegrityError:
            # Lost a race: either the same Idempotency-Key, or another execution became active.
            return await self._replay_or_conflict(owner_id, idempotency_key, req_hash, conn.id)

        log.info("execution.accepted", execution_id=execution.id, orders=len(orders), mode=req.mode)
        self._runner.schedule(execution.id)
        return execution, orders, False

    async def preview(self, owner_id: str, req: AnyExecutionRequest) -> tuple[list[PlannedOrder], list[ValidationIssue]]:
        issues = validate_structure(
            req,
            max_qty_per_order=self._settings.max_qty_per_order,
            max_orders_per_execution=self._settings.max_orders_per_execution,
        )
        if issues:
            return [], issues
        _conn, gateway = await self._connections.gateway_for_owner(owner_id, req.connection_id)
        instructions = to_instructions(req)
        issues = await self._preflight(gateway, instructions)
        return ([] if issues else plan_orders(instructions)), issues

    async def _preflight(self, gateway: BrokerGateway, instructions: list[Instruction]) -> list[ValidationIssue]:
        try:
            gateway.ensure_can_trade()
            await gateway.validate_session()
            issues: list[ValidationIssue] = []
            for ins in instructions:
                try:
                    gateway.resolve_symbol(ins.symbol)
                except InvalidInstrument as exc:
                    issues.append(ValidationIssue("INVALID_SYMBOL", exc.message, ins.field, ins.symbol))
            if issues:
                return issues
            holdings: dict[str, int] = {}
            for h in await gateway.get_holdings():
                holdings[h.symbol] = holdings.get(h.symbol, 0) + h.quantity
        except BrokerError as exc:
            raise api_error_from_broker(exc) from exc
        return validate_against_holdings(instructions, holdings)

    @staticmethod
    def _order_row(execution_id: str, p: PlannedOrder) -> Order:
        return Order(
            execution_id=execution_id,
            seq=p.seq,
            phase=p.phase,
            instruction_type=p.instruction_type.value,
            symbol=p.symbol,
            exchange="NSE",
            side=p.side.value,
            quantity=p.quantity,
            order_type="MARKET",
            product="CNC",
            client_order_id=p.client_order_id,
            status=OrderStatus.PENDING.value,
            filled_quantity=0,
            attempts=0,
        )

    async def _replay(self, existing: Execution, req_hash: str) -> tuple[Execution, list[Order], bool]:
        if existing.request_hash != req_hash:
            raise ApiError(
                422,
                "IDEMPOTENCY_KEY_REUSED",
                "This Idempotency-Key was already used with a different request body",
                [{"execution_id": existing.id}],
            )
        log.info("execution.replayed", execution_id=existing.id)
        execution, orders = await self.get_with_orders(existing.owner_id, existing.id)
        return execution, orders, True

    async def _replay_or_conflict(
        self, owner_id: str, idempotency_key: str, req_hash: str, connection_id: str
    ) -> tuple[Execution, list[Order], bool]:
        """The account is busy or our insert lost a race. If the winner used *this* Idempotency-Key it
        is the same request retried concurrently -> replay it. Otherwise -> 409."""
        existing = await self._by_key(owner_id, idempotency_key)
        if existing is not None:
            return await self._replay(existing, req_hash)
        active = await self._active_for(connection_id)
        if active is not None:
            raise self._in_progress(active)
        raise ApiError(409, "CONFLICT", "Concurrent update; retry the request with the same Idempotency-Key")

    @staticmethod
    def _in_progress(active: Execution) -> ApiError:
        return ApiError(
            409,
            "EXECUTION_IN_PROGRESS",
            "Another execution is still running on this broker account",
            [{"execution_id": active.id, "status": active.status}],
        )

    async def _by_key(self, owner_id: str, key: str) -> Execution | None:
        async with self._sm() as db:
            return (
                await db.execute(
                    select(Execution).where(Execution.owner_id == owner_id, Execution.idempotency_key == key)
                )
            ).scalar_one_or_none()

    async def _active_for(self, connection_id: str) -> Execution | None:
        async with self._sm() as db:
            return (
                await db.execute(
                    select(Execution).where(Execution.connection_id == connection_id, Execution.status.in_(_ACTIVE))
                )
            ).scalar_one_or_none()

    # ------------------------------------------------------------------ queries

    async def get_with_orders(self, owner_id: str, execution_id: str) -> tuple[Execution, list[Order]]:
        async with self._sm() as db:
            execution = await db.get(Execution, execution_id)
            if execution is None or execution.owner_id != owner_id:
                raise ApiError(404, "EXECUTION_NOT_FOUND", "Execution not found")
            orders = list(
                (await db.execute(select(Order).where(Order.execution_id == execution_id).order_by(Order.seq))).scalars()
            )
        return execution, orders

    async def list_for_owner(
        self, owner_id: str, *, connection_id: str | None, status: str | None, limit: int, offset: int
    ) -> list[Execution]:
        stmt = select(Execution).where(Execution.owner_id == owner_id)
        if connection_id:
            stmt = stmt.where(Execution.connection_id == connection_id)
        if status:
            stmt = stmt.where(Execution.status == status)
        stmt = stmt.order_by(Execution.created_at.desc()).limit(limit).offset(offset)
        async with self._sm() as db:
            return list((await db.execute(stmt)).scalars())

    async def count_active(self) -> int:
        async with self._sm() as db:
            return (await db.execute(select(func.count()).where(Execution.status.in_(_ACTIVE)))).scalar_one()

    # ------------------------------------------------------------------ follow-ups

    async def _finished(self, owner_id: str, execution_id: str) -> Execution:
        execution, _ = await self.get_with_orders(owner_id, execution_id)
        if execution.status in _ACTIVE:
            raise ApiError(409, "EXECUTION_NOT_FINISHED", "The execution is still running")
        return execution

    async def reconcile(self, owner_id: str, execution_id: str) -> tuple[Execution, list[Order]]:
        execution = await self._finished(owner_id, execution_id)
        _conn, gateway = await self._connections.gateway_for_owner(owner_id, execution.connection_id)
        try:
            await self._runner.reconcile_finished(execution_id, gateway)
        except BrokerError as exc:
            raise api_error_from_broker(exc) from exc
        return await self.get_with_orders(owner_id, execution_id)

    async def resend_notification(self, owner_id: str, execution_id: str) -> Execution:
        await self._finished(owner_id, execution_id)
        await self._notifications.notify(execution_id)
        execution, _ = await self.get_with_orders(owner_id, execution_id)
        return execution
