"""Tracks fire-and-forget asyncio tasks so they can all be cancelled at shutdown.

Lifted out of the Director so any orchestration component can use the
same supervised primitive without re-implementing the bookkeeping that
makes ``shutdown()`` confident no background work survives a torn-down
room. Project convention: never use bare ``asyncio.create_task()`` for
fire-and-forget — exceptions get silently swallowed and tasks leak past
the room they were spawned for.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("podcast-commentary.tasks")


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Fire-and-forget task %r failed: %s", task.get_name(), exc, exc_info=exc)


class TaskSupervisor:
    """Tracks a set of background tasks and cancels them on shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        self._closed: bool = False

    def fire_and_forget(self, coro: Any, *, name: str = "") -> asyncio.Task:
        """Schedule a task that surfaces exceptions and is cancellable on shutdown.

        Tasks started after shutdown has begun are created (so the coroutine
        is closed cleanly without the "never awaited" warning) and
        immediately cancelled — the logic never runs.
        """
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(_log_task_exception)
        task.add_done_callback(self._tasks.discard)
        self._tasks.add(task)
        if self._closed:
            task.cancel()
        return task

    async def shutdown(self) -> None:
        """Cancel all tracked tasks and await their completion. Idempotent."""
        self._closed = True
        pending = [t for t in self._tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


__all__ = ["TaskSupervisor"]
