r"""ExecutionRunner: the per-order state machine.

    PENDING --(commit SUBMITTING)--> place_order()
       ack -----------------------> SUBMITTED --poll--> OPEN / PARTIALLY_FILLED
                                                     \-> FILLED | REJECTED | CANCELLED
       not placed (retries exhausted, auth) -> FAILED
       OrderRejected / InvalidInstrument   -> REJECTED
       AmbiguousSubmission -> RECONCILING --tag found--> SUBMITTED (adopt broker_order_id)
                                          --not found--> UNKNOWN   (never auto-resent)
    restart: PENDING -> SKIPPED, SUBMITTING/RECONCILING -> reconcile, SUBMITTED/OPEN -> resume polling

Every transition is a conditional UPDATE (`WHERE status = <last known>`) committed before the next
broker call, so the database always says what may already be at the broker.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.brokers.gateway import BrokerGateway
from app.connections.service import ConnectionService
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import BrokerConnection, Execution, Order, OrderEvent, utcnow
from app.domain.errors import (
    AmbiguousSubmission,
    BrokerError,
    InvalidInstrument,
    OrderRejected,
    ReauthRequired,
)
from app.domain.models import (
    ACTIVE_EXECUTION_STATUSES,
    LIVE_ORDER_STATUSES,
    PHASE_BUY,
    PHASE_SELL,
    TERMINAL_ORDER_STATUSES,
    ExecutionStatus,
    OrderRequest,
    OrderSnapshot,
    OrderState,
    OrderStatus,
    Side,
)
from app.execution.status import OrderOutcome, aggregate
from app.execution.tasks import TaskRegistry
from app.notifications.service import NotificationService

log = get_logger(__name__)

_ACTIVE = [s.value for s in ACTIVE_EXECUTION_STATUSES]
# Orders that may be at the broker but whose fate we never learned.
_UNRESOLVED = (OrderStatus.SUBMITTING, OrderStatus.RECONCILING)


@dataclass
class TrackedOrder:
    """In-memory mirror of an `orders` row; `status` is the last status we committed."""

    id: str
    execution_id: str
    seq: int
    phase: int
    symbol: str
    exchange: str
    side: Side
    quantity: int
    client_order_id: str
    status: OrderStatus
    broker_order_id: str | None
    filled_quantity: int
    average_price: Decimal | None
    attempts: int
    submitted_at: datetime | None

    @classmethod
    def from_row(cls, r: Order) -> TrackedOrder:
        return cls(
            id=r.id,
            execution_id=r.execution_id,
            seq=r.seq,
            phase=r.phase,
            symbol=r.symbol,
            exchange=r.exchange,
            side=Side(r.side),
            quantity=r.quantity,
            client_order_id=r.client_order_id,
            status=OrderStatus(r.status),
            broker_order_id=r.broker_order_id,
            filled_quantity=r.filled_quantity or 0,
            average_price=r.average_price,
            attempts=r.attempts or 0,
            submitted_at=r.submitted_at,
        )

    def log_fields(self) -> dict[str, Any]:
        return {
            "order_id": self.id,
            "seq": self.seq,
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
        }


def status_from_snapshot(snap: OrderSnapshot) -> OrderStatus:
    match snap.status:
        case OrderState.FILLED:
            return OrderStatus.FILLED
        case OrderState.REJECTED:
            return OrderStatus.REJECTED
        case OrderState.CANCELLED:
            return OrderStatus.CANCELLED
        case OrderState.PARTIALLY_FILLED:
            return OrderStatus.PARTIALLY_FILLED
        case _:
            return OrderStatus.PARTIALLY_FILLED if snap.filled_qty > 0 else OrderStatus.OPEN


class ExecutionRunner:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        connections: ConnectionService,
        notifications: NotificationService,
        settings: Settings,
        tasks: TaskRegistry,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sm = sessionmaker
        self._connections = connections
        self._notifications = notifications
        self._settings = settings
        self._tasks = tasks
        self._sleep = sleep
        self._clock = clock

    # ================================================================== entry points

    def schedule(self, execution_id: str, *, recover: bool = False) -> asyncio.Task:
        return self._tasks.spawn(execution_id, self.run(execution_id, recover=recover))

    async def run(self, execution_id: str, *, recover: bool = False) -> None:
        structlog.contextvars.bind_contextvars(execution_id=execution_id)
        try:
            finished = await self._execute(execution_id, recover)
        except asyncio.CancelledError:
            log.warning("execution.interrupted", message="left for startup recovery")
            raise
        except Exception:
            log.exception("execution.crashed")
            finished = await self._fail_safe(execution_id)
        if finished:
            try:
                await self._notifications.notify(execution_id)
            except Exception:
                log.exception("notification.error")

    # ================================================================== the run

    async def _execute(self, execution_id: str, recover: bool) -> bool:
        async with self._sm() as db:
            ex = await db.get(Execution, execution_id)
            if ex is None or ex.status not in _ACTIVE:
                return False
            conn = await db.get(BrokerConnection, ex.connection_id)
            rows = (await db.execute(select(Order).where(Order.execution_id == execution_id).order_by(Order.seq))).scalars()
            orders = [TrackedOrder.from_row(r) for r in rows]
            if ex.status == ExecutionStatus.ACCEPTED.value:
                ex.status = ExecutionStatus.RUNNING.value
                ex.started_at = utcnow()
                await db.commit()
        structlog.contextvars.bind_contextvars(connection_id=conn.id, broker=conn.broker)
        log.info("execution.started", recover=recover, orders=len(orders))

        try:
            gateway: BrokerGateway | None = self._connections.gateway_for(conn)
        except ReauthRequired as exc:
            log.warning("execution.connection_unusable", error=str(exc))
            await self._connections.mark_expired(conn.id)
            gateway = None

        if gateway is None:
            await self._sweep(orders, pending_to=OrderStatus.FAILED, code="AUTH_EXPIRED",
                              message="broker connection is not active; not sent")
            await self._finalize(execution_id, error_code="BROKER_REAUTH_REQUIRED")
            return True

        if recover:
            await self._recover(orders, gateway)

        for phase in (PHASE_SELL, PHASE_BUY):
            phase_orders = [o for o in orders if o.phase == phase]
            if phase_orders:
                await self._run_phase(phase, phase_orders, gateway)

        await self._sweep(orders, pending_to=OrderStatus.SKIPPED, code="NOT_SUBMITTED",
                          message="order was never submitted")
        await self._finalize(execution_id, error_code="BROKER_REAUTH_REQUIRED" if gateway.auth_failed else None)
        return True

    async def _run_phase(self, phase: int, phase_orders: list[TrackedOrder], gateway: BrokerGateway) -> None:
        name = "SELL" if phase == PHASE_SELL else "BUY"
        log.info("execution.phase.started", phase=name, orders=len(phase_orders))
        sem = asyncio.Semaphore(self._settings.submit_concurrency)
        pending = [o for o in phase_orders if o.status is OrderStatus.PENDING]
        results = await asyncio.gather(*(self._submit(o, gateway, sem) for o in pending), return_exceptions=True)
        for o, res in zip(pending, results):
            if isinstance(res, BaseException):
                log.error("order.submit.error", error_type=type(res).__name__, error=str(res), **o.log_fields())
        await self._monitor([o for o in phase_orders if o.status in LIVE_ORDER_STATUSES], gateway)
        log.info(
            "execution.phase.finished",
            phase=name,
            statuses={o.client_order_id: o.status.value for o in phase_orders},
        )

    # ================================================================== submit

    async def _submit(self, o: TrackedOrder, gateway: BrokerGateway, sem: asyncio.Semaphore) -> None:
        async with sem:
            if gateway.auth_failed:
                await self._transition(o, OrderStatus.FAILED, error_code="AUTH_EXPIRED",
                                       error_message="broker session expired earlier in this run; not sent")
                return
            # Write-before-send. The conditional update is also the claim: only one runner can win it.
            if not await self._transition(o, OrderStatus.SUBMITTING, submitted_at=utcnow()):
                return
            log.info("order.submit.started", **o.log_fields())
            req = OrderRequest(
                symbol=o.symbol, side=o.side, quantity=o.quantity, client_order_id=o.client_order_id, exchange=o.exchange
            )
            try:
                result = await gateway.place_order(req)
            except AmbiguousSubmission as exc:
                log.warning("order.submit.ambiguous", error=exc.message, **o.log_fields())
                await self._transition(o, OrderStatus.RECONCILING, attempts=_attempts(exc),
                                       error_code=exc.code, error_message=exc.message)
            except (OrderRejected, InvalidInstrument) as exc:
                await self._transition(o, OrderStatus.REJECTED, attempts=_attempts(exc),
                                       error_code=exc.code, error_message=exc.message)
                return
            except BrokerError as exc:  # placed=False: auth, rate limit / unavailable after retries, kill switch
                await self._transition(o, OrderStatus.FAILED, attempts=_attempts(exc),
                                       error_code=exc.code, error_message=f"{exc.message} (not placed)")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # our own failure after a possible send: treat as ambiguous
                log.exception("order.submit.unexpected", **o.log_fields())
                await self._transition(o, OrderStatus.RECONCILING, error_code="INTERNAL_ERROR",
                                       error_message=type(exc).__name__)
            else:
                o.broker_order_id = result.ack.broker_order_id
                await self._transition(o, OrderStatus.SUBMITTED, broker_order_id=result.ack.broker_order_id,
                                       attempts=result.attempts)
                log.info("order.submit.ack", attempts=result.attempts, **o.log_fields())
                return
        # Reconcile outside the semaphore so waiting on broker lag doesn't hold up other submissions.
        await self._reconcile([o], gateway)

    # ================================================================== reconcile

    def _match(self, o: TrackedOrder, book: list[OrderSnapshot], gateway: BrokerGateway) -> list[OrderSnapshot]:
        if gateway.adapter.capabilities.supports_order_tag:
            return [s for s in book if s.client_order_id == o.client_order_id]
        # Fallback for brokers without tags: same instrument/side/qty placed after we submitted.
        cutoff = (o.submitted_at or utcnow()) - timedelta(seconds=5)
        return [
            s for s in book
            if s.symbol == o.symbol and s.side is o.side and s.quantity == o.quantity
            and (s.placed_at is None or s.placed_at >= cutoff)
        ]

    async def _reconcile(self, orders: list[TrackedOrder], gateway: BrokerGateway) -> None:
        """Look for our tag in the order book a few times (brokers can lag). Found -> adopt it.
        Not found -> UNKNOWN. Never resend."""
        unresolved = [o for o in orders if o.status is OrderStatus.RECONCILING]
        attempts = max(1, self._settings.reconcile_attempts)
        for attempt in range(1, attempts + 1):
            try:
                book = await gateway.list_orders()
            except ReauthRequired:
                break
            except BrokerError as exc:
                log.warning("order.reconcile.book_unavailable", attempt=attempt, error_code=exc.code)
                book = None
            if book is not None:
                for o in list(unresolved):
                    matches = self._match(o, book, gateway)
                    if len(matches) == 1:
                        snap = matches[0]
                        o.broker_order_id = snap.broker_order_id
                        await self._transition(
                            o, OrderStatus.SUBMITTED, broker_order_id=snap.broker_order_id,
                            error_code=None, error_message=None,
                            detail={"reconciled": True, "attempt": attempt},
                        )
                        log.info("order.reconcile.found", attempt=attempt, **o.log_fields())
                        await self._apply_snapshot(o, snap)
                        unresolved.remove(o)
                    elif len(matches) > 1:
                        await self._transition(
                            o, OrderStatus.UNKNOWN, error_code="MULTIPLE_MATCHES",
                            error_message=f"{len(matches)} broker orders match; review manually (not resent)",
                        )
                        unresolved.remove(o)
            if not unresolved:
                return
            if attempt < attempts:
                await self._sleep(self._settings.reconcile_delay_s)
        for o in unresolved:
            log.warning("order.reconcile.not_found", **o.log_fields())
            await self._transition(
                o, OrderStatus.UNKNOWN, error_code="OUTCOME_UNKNOWN",
                error_message="broker response was lost and the order was not found in the order book; "
                "it was NOT resent. Run reconcile later to confirm.",
            )

    # ================================================================== monitor

    async def _monitor(self, orders: list[TrackedOrder], gateway: BrokerGateway) -> None:
        tracked = {o.broker_order_id: o for o in orders if o.status in LIVE_ORDER_STATUSES and o.broker_order_id}
        deadline = self._clock() + self._settings.order_monitor_timeout_s
        while tracked:
            try:
                book = await gateway.list_orders()
            except ReauthRequired:
                log.warning("order.monitor.auth_expired", remaining=len(tracked))
                return
            except BrokerError as exc:
                log.warning("order.monitor.book_unavailable", error_code=exc.code)
                book = None
            if book is not None:
                by_id = {s.broker_order_id: s for s in book}
                for bid, o in list(tracked.items()):
                    snap = by_id.get(bid)
                    if snap is not None:
                        await self._apply_snapshot(o, snap)
                    if o.status in TERMINAL_ORDER_STATUSES:
                        del tracked[bid]
            if not tracked:
                return
            if self._clock() >= deadline:
                log.info("order.monitor.window_elapsed", still_open=[o.client_order_id for o in tracked.values()])
                return
            await self._sleep(self._settings.poll_interval_s)

    async def _apply_snapshot(self, o: TrackedOrder, snap: OrderSnapshot) -> None:
        if o.status in TERMINAL_ORDER_STATUSES:
            return  # never move a finished order backwards
        new_status = status_from_snapshot(snap)
        filled = max(o.filled_quantity, snap.filled_qty or 0)  # fills never decrease
        if new_status is OrderStatus.FILLED and filled == 0:
            filled = o.quantity
        avg = snap.avg_price if snap.avg_price is not None else o.average_price
        if new_status is o.status and filled == o.filled_quantity and avg == o.average_price:
            return
        fields: dict[str, Any] = {"filled_quantity": filled, "average_price": avg, "broker_status_raw": snap.raw_status}
        if new_status is OrderStatus.REJECTED:
            fields.update(error_code="ORDER_REJECTED", error_message=snap.status_message or "rejected by broker")
        elif new_status is OrderStatus.CANCELLED:
            fields.update(error_code="ORDER_CANCELLED", error_message=snap.status_message or "cancelled at broker")
        await self._transition(o, new_status, **fields)

    # ================================================================== recovery

    async def _recover(self, orders: list[TrackedOrder], gateway: BrokerGateway) -> None:
        for o in orders:
            if o.status is OrderStatus.PENDING:
                await self._transition(o, OrderStatus.SKIPPED, error_code="SERVER_RESTARTED",
                                       error_message="server restarted before submission; not sent (re-run if still wanted)")
            elif o.status is OrderStatus.SUBMITTING:
                await self._transition(o, OrderStatus.RECONCILING, detail={"recovery": True})
        to_reconcile = [o for o in orders if o.status is OrderStatus.RECONCILING]
        if to_reconcile:
            await self._reconcile(to_reconcile, gateway)
        log.info(
            "recovery.resumed",
            reconciled=len(to_reconcile),
            live=sum(1 for o in orders if o.status in LIVE_ORDER_STATUSES),
        )

    async def _fail_safe(self, execution_id: str) -> bool:
        """After an unexpected crash inside the run: never leave the account locked in RUNNING."""
        try:
            async with self._sm() as db:
                rows = (await db.execute(select(Order).where(Order.execution_id == execution_id))).scalars()
                orders = [TrackedOrder.from_row(r) for r in rows]
            await self._sweep(orders, pending_to=OrderStatus.SKIPPED, code="INTERNAL_ERROR",
                              message="execution aborted by an internal error; not sent")
            await self._finalize(execution_id, error_code="INTERNAL_ERROR")
            return True
        except Exception:
            log.exception("execution.fail_safe_failed")
            return False

    async def _sweep(self, orders: list[TrackedOrder], *, pending_to: OrderStatus, code: str, message: str) -> None:
        for o in orders:
            if o.status is OrderStatus.PENDING:
                await self._transition(o, pending_to, error_code=code, error_message=message)
            elif o.status in _UNRESOLVED:
                await self._transition(o, OrderStatus.UNKNOWN, error_code="OUTCOME_UNKNOWN",
                                       error_message="order may have reached the broker; not resent, needs review")

    # ================================================================== manual reconcile

    async def reconcile_finished(self, execution_id: str, gateway: BrokerGateway) -> None:
        """POST /executions/{id}/reconcile: re-check UNKNOWN and still-open orders against the order book.
        Broker errors propagate to the caller."""
        async with self._sm() as db:
            rows = (await db.execute(select(Order).where(Order.execution_id == execution_id).order_by(Order.seq))).scalars()
            orders = [TrackedOrder.from_row(r) for r in rows]
        book = await gateway.list_orders()
        by_id = {s.broker_order_id: s for s in book}
        now = utcnow()
        for o in orders:
            if o.status is OrderStatus.UNKNOWN:
                matches = self._match(o, book, gateway)
                if len(matches) == 1:
                    o.broker_order_id = matches[0].broker_order_id
                    await self._transition(o, OrderStatus.SUBMITTED, broker_order_id=o.broker_order_id,
                                           error_code=None, error_message=None, detail={"manual_reconcile": True})
                    await self._apply_snapshot(o, matches[0])
                elif not matches and o.submitted_at is not None and (
                    now - o.submitted_at
                ).total_seconds() >= self._settings.reconcile_confirm_after_s:
                    await self._transition(o, OrderStatus.FAILED, error_code="NOT_PLACED_CONFIRMED",
                                           error_message="confirmed absent from the broker order book; safe to re-run")
            elif o.status in LIVE_ORDER_STATUSES and o.broker_order_id in by_id:
                await self._apply_snapshot(o, by_id[o.broker_order_id])
        status, summary = aggregate(_outcomes(orders))
        async with self._sm() as db:
            await db.execute(
                update(Execution)
                .where(Execution.id == execution_id, Execution.status.not_in(_ACTIVE))
                .values(status=status.value, summary=summary, updated_at=utcnow())
            )
            await db.commit()
        log.info("execution.reconciled", execution_id=execution_id, status=status.value)

    # ================================================================== persistence

    async def _transition(
        self, o: TrackedOrder, to: OrderStatus, *, detail: dict[str, Any] | None = None, **fields: Any
    ) -> bool:
        """Conditional update from the last committed status. Returns False if the row moved under us."""
        now = utcnow()
        values: dict[str, Any] = {"status": to.value, "updated_at": now, **fields}
        if to in TERMINAL_ORDER_STATUSES:
            values["completed_at"] = now
        async with self._sm() as db:
            res = await db.execute(
                update(Order).where(Order.id == o.id, Order.status == o.status.value).values(**values)
            )
            if res.rowcount != 1:
                await db.rollback()
                log.warning("order.transition.conflict", expected=o.status.value, to=to.value, **o.log_fields())
                return False
            if to is not o.status:
                db.add(
                    OrderEvent(
                        order_id=o.id,
                        execution_id=o.execution_id,
                        from_status=o.status.value,
                        to_status=to.value,
                        detail=_jsonable({**fields, **(detail or {})}),
                    )
                )
            await db.commit()
        previous = o.status
        o.status = to
        for key in ("broker_order_id", "filled_quantity", "average_price", "attempts", "submitted_at"):
            if key in fields:
                setattr(o, key, fields[key])
        if to is not previous:
            log.info(
                "order.status.changed",
                from_status=previous.value,
                status=to.value,
                filled_quantity=o.filled_quantity,
                error_code=fields.get("error_code"),
                **o.log_fields(),
            )
        return True

    async def _finalize(self, execution_id: str, error_code: str | None = None) -> None:
        async with self._sm() as db:
            rows = list((await db.execute(select(Order).where(Order.execution_id == execution_id))).scalars())
            status, summary = aggregate(_outcomes([TrackedOrder.from_row(r) for r in rows]))
            await db.execute(
                update(Execution)
                .where(Execution.id == execution_id, Execution.status.in_(_ACTIVE))
                .values(status=status.value, summary=summary, finished_at=utcnow(), error_code=error_code,
                        updated_at=utcnow())
            )
            await db.commit()
        log.info("execution.finished", status=status.value, summary=summary, error_code=error_code)


def _attempts(exc: BaseException) -> int:
    return int(getattr(exc, "attempts", 1) or 0)


def _outcomes(orders: list[TrackedOrder]) -> list[OrderOutcome]:
    return [OrderOutcome(o.symbol, o.side.value, o.status, o.quantity, o.filled_quantity) for o in orders]


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, (datetime, Decimal)):
            out[k] = str(v)
        else:
            out[k] = v
    return out
