import asyncio
import logging

import pytest

from opsfleet_agent.gateways.background.tasks import AsyncioBackgroundTasks


async def test_work_with_a_running_key_is_skipped() -> None:
    tasks, release, calls = AsyncioBackgroundTasks(), asyncio.Event(), []

    async def work() -> None:
        calls.append(1)
        await release.wait()

    tasks.spawn("summary:a", work)
    tasks.spawn("summary:a", work)
    await asyncio.sleep(0)
    release.set()
    await tasks.drain(grace_seconds=1)

    assert calls == [1]


async def test_a_key_can_run_again_after_it_finished() -> None:
    tasks, calls = AsyncioBackgroundTasks(), []

    async def work() -> None:
        calls.append(1)

    for _ in range(2):
        tasks.spawn("summary:a", work)
        await tasks.drain(grace_seconds=1)

    assert calls == [1, 1]


async def test_failures_are_logged_and_free_the_key(caplog: pytest.LogCaptureFixture) -> None:
    tasks = AsyncioBackgroundTasks()

    async def broken() -> None:
        raise RuntimeError("llm down")

    tasks.spawn("summary:a", broken)
    await tasks.drain(grace_seconds=1)
    await asyncio.sleep(0)

    assert "summary:a failed: RuntimeError" in caplog.text
    assert caplog.records[0].levelno == logging.WARNING
    tasks.spawn("summary:a", broken)
    await tasks.drain(grace_seconds=1)


async def test_drain_cancels_work_past_the_grace_period() -> None:
    tasks, cancelled = AsyncioBackgroundTasks(), []

    async def stuck() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    tasks.spawn("summary:a", stuck)
    await asyncio.sleep(0)
    await tasks.drain(grace_seconds=0.01)

    assert cancelled == [1]
