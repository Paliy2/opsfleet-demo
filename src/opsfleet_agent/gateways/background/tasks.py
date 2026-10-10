import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable

from opsfleet_agent.ports.background import BackgroundTasks

logger = logging.getLogger(__name__)


class AsyncioBackgroundTasks(BackgroundTasks):
    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task[None]] = {}

    def spawn(self, key: str, work: Callable[[], Awaitable[None]]) -> None:
        if key in self._running:
            return
        task = asyncio.create_task(_run(work), name=key, context=contextvars.Context())
        self._running[key] = task
        task.add_done_callback(lambda done: self._finished(key, done))

    async def drain(self, grace_seconds: float) -> None:
        pending = set(self._running.values())
        if not pending:
            return
        _, unfinished = await asyncio.wait(pending, timeout=grace_seconds)
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*unfinished, return_exceptions=True)

    def _finished(self, key: str, task: asyncio.Task[None]) -> None:
        self._running.pop(key, None)
        if task.cancelled():
            logger.warning("background task %s cancelled", key)
            return
        error = task.exception()
        if error is not None:
            logger.warning("background task %s failed: %s", key, type(error).__name__)


async def _run(work: Callable[[], Awaitable[None]]) -> None:
    await work()
