import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import BaseModel

from opsfleet_agent.api.schemas import (
    AnswerEvent,
    ConfirmationEvent,
    ConfirmationItem,
    ErrorEvent,
)
from opsfleet_agent.core.errors import AgentError
from opsfleet_agent.entities.conversation import ProgressEvent, TurnOutcome
from opsfleet_agent.ports.agent import ProgressSink

type TurnRunner = Callable[[ProgressSink], Awaitable[TurnOutcome]]

_INTERNAL = ErrorEvent(code="E_INTERNAL", message="Something went wrong. Please try again.")


@dataclass
class QueueProgress:
    queue: asyncio.Queue[tuple[str, BaseModel]] = field(default_factory=asyncio.Queue)

    def emit(self, event: ProgressEvent) -> None:
        self.queue.put_nowait(("progress", event))


def sse(event: str, payload: BaseModel) -> str:
    return f"event: {event}\ndata: {payload.model_dump_json()}\n\n"


def _final(outcome: TurnOutcome) -> tuple[str, BaseModel]:
    reply = outcome.reply
    if reply.confirmation is not None:
        request = reply.confirmation
        return "confirmation", ConfirmationEvent(
            turn_id=request.turn_id,
            plan_id=request.plan_id,
            items=tuple(
                ConfirmationItem(id=item.id, title=item.title, created_at=item.created_at)
                for item in request.items
            ),
            expires_at=request.expires_at,
        )
    return "answer", AnswerEvent(
        session_id=outcome.session_id,
        text=reply.text,
        chart_ids=reply.chart_ids,
        report_ids=reply.report_ids,
        trace_id=outcome.trace_id,
        llm_calls=reply.llm_calls,
    )


async def _run(runner: TurnRunner, progress: QueueProgress) -> tuple[str, BaseModel]:
    try:
        return _final(await runner(progress))
    except AgentError as error:
        return "error", ErrorEvent(
            code=error.code, message=error.user_message, trace_id=error.trace_id
        )


async def stream_turn(runner: TurnRunner) -> AsyncIterator[str]:
    progress = QueueProgress()
    task = asyncio.create_task(_run(runner, progress))
    while not task.done() or not progress.queue.empty():
        getter = asyncio.ensure_future(progress.queue.get())
        done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            name, payload = getter.result()
            yield sse(name, payload)
        else:
            getter.cancel()
    failure = task.exception()
    if failure is not None:
        yield sse("error", _INTERNAL)
        raise failure
    name, payload = task.result()
    yield sse(name, payload)
