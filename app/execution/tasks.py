"""Registry of in-process execution tasks, so shutdown can drain them and tests can await them."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def spawn(self, key: str, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        if key in self._tasks:
            coro.close()
            return self._tasks[key]
        task = asyncio.create_task(coro, name=f"execution:{key}")
        self._tasks[key] = task
        task.add_done_callback(lambda _t, k=key: self._tasks.pop(k, None))
        return task

    def is_running(self, key: str) -> bool:
        return key in self._tasks

    async def wait(self, key: str, timeout: float | None = None) -> None:
        task = self._tasks.get(key)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout)

    async def wait_all(self, timeout: float | None = None) -> None:
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def shutdown(self, grace_s: float) -> None:
        """Let running executions finish for `grace_s`, then cancel. Anything interrupted is picked
        up by startup recovery (SUBMITTING orders are reconciled, never resent)."""
        tasks = list(self._tasks.values())
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=grace_s)
        for task in pending:
            log.warning("execution.shutdown_cancel", task=task.get_name())
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
