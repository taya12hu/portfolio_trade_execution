"""Startup recovery: resume every execution a previous process left ACCEPTED or RUNNING.

The runner's recover mode never sends an order that was not already sent:
PENDING -> SKIPPED, SUBMITTING/RECONCILING -> reconcile by tag, SUBMITTED/OPEN -> resume polling.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import get_logger
from app.db.models import Execution
from app.domain.models import ACTIVE_EXECUTION_STATUSES
from app.execution.runner import ExecutionRunner

log = get_logger(__name__)


async def recover_interrupted_executions(sessionmaker: async_sessionmaker, runner: ExecutionRunner) -> list[str]:
    async with sessionmaker() as db:
        ids = list(
            (
                await db.execute(
                    select(Execution.id)
                    .where(Execution.status.in_([s.value for s in ACTIVE_EXECUTION_STATUSES]))
                    .order_by(Execution.created_at)
                )
            ).scalars()
        )
    for execution_id in ids:
        log.warning("recovery.scheduled", execution_id=execution_id)
        runner.schedule(execution_id, recover=True)
    return ids
