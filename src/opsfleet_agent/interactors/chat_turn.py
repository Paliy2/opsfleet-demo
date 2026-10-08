from dataclasses import dataclass
from uuid import UUID

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.context import ConversationContext
from opsfleet_agent.entities.conversation import (
    AgentReply,
    AgentRequest,
    ChatMessage,
    MessageRole,
    ProgressEvent,
    TurnOutcome,
)
from opsfleet_agent.entities.telemetry import TurnKind
from opsfleet_agent.interactors.context import advance_state
from opsfleet_agent.interactors.pii_scan import ensure_no_pii
from opsfleet_agent.ports.access import AccessRepo
from opsfleet_agent.ports.agent import AnalysisAgent, InputGuard, ProgressSink
from opsfleet_agent.ports.context import ConversationSummarizer, PreferenceRepo, SessionMemory
from opsfleet_agent.ports.conversations import ConversationRepo
from opsfleet_agent.ports.privacy import PiiRedactor
from opsfleet_agent.ports.telemetry import Telemetry, TurnTrace


@dataclass(frozen=True)
class _TracedProgress:
    progress: ProgressSink
    trace: TurnTrace

    def emit(self, event: ProgressEvent) -> None:
        self.trace.emit(event)
        self.progress.emit(event)


def _describe(findings: tuple[str, ...]) -> str:
    return ", ".join(finding.lower().replace("_", " ") for finding in findings)


class ChatTurn:
    def __init__(
        self,
        *,
        access: AccessRepo,
        conversations: ConversationRepo,
        redactor: PiiRedactor,
        guard: InputGuard,
        agent: AnalysisAgent,
        telemetry: Telemetry,
        memory: SessionMemory,
        preferences: PreferenceRepo,
        summarizer: ConversationSummarizer,
        history_max_turns: int,
        history_max_chars: int,
        state_max_steps: int,
    ) -> None:
        self._memory = memory
        self._preferences = preferences
        self._summarizer = summarizer
        self._state_max_steps = state_max_steps
        self._access = access
        self._conversations = conversations
        self._redactor = redactor
        self._guard = guard
        self._agent = agent
        self._telemetry = telemetry
        self._history_max_turns = history_max_turns
        self._history_max_chars = history_max_chars

    async def scope(self, user_id: str) -> AccessScope:
        return await self._access.get_scope(user_id)

    async def require_session(self, user_id: str, session_id: UUID) -> UUID:
        return (await self._conversations.get_session(session_id, user_id)).id

    async def open_session(self, user_id: str, session_id: UUID | None = None) -> UUID:
        await self._access.get_scope(user_id)
        if session_id is None:
            return (await self._conversations.create_session(user_id)).id
        try:
            return (await self._conversations.get_session(session_id, user_id)).id
        except NotFoundError:
            return (await self._conversations.create_session(user_id, session_id)).id

    async def __call__(
        self, user_id: str, session_id: UUID, question: str, progress: ProgressSink
    ) -> TurnOutcome:
        redaction = await self._redactor.redact(question)
        question = redaction.text
        with self._telemetry.turn(TurnKind.QUESTION, user_id, session_id, question) as trace:
            sink = _TracedProgress(progress, trace)
            scope = await self._access.get_scope(user_id)
            session = await self._conversations.get_session(session_id, user_id)
            if redaction.findings:
                sink.emit(ProgressEvent(stage="privacy", detail=_describe(redaction.findings)))
            sink.emit(ProgressEvent(stage="guard"))
            await self._guard.check(question)
            history = await self._conversations.recent(
                session.id, self._history_max_turns, self._history_max_chars
            )
            context = await self._context(session.id, user_id, history, sink)
            await self._conversations.append(session.id, MessageRole.USER, question)
            reply = await self._agent.respond(
                AgentRequest(
                    scope=scope,
                    session_id=session.id,
                    question=question,
                    history=history,
                    context=context,
                ),
                sink,
            )
            return await self._finish(session.id, reply, trace)

    async def confirm(
        self,
        user_id: str,
        session_id: UUID,
        turn_id: UUID,
        approved: bool,
        progress: ProgressSink,
    ) -> TurnOutcome:
        decision = "approved" if approved else "declined"
        with self._telemetry.turn(TurnKind.CONFIRMATION, user_id, session_id, decision) as trace:
            scope = await self._access.get_scope(user_id)
            session = await self._conversations.get_session(session_id, user_id)
            reply = await self._agent.resume(
                scope, session.id, turn_id, approved, _TracedProgress(progress, trace)
            )
            return await self._finish(session.id, reply, trace)

    async def _context(
        self, session_id: UUID, user_id: str, history: tuple[ChatMessage, ...], sink: ProgressSink
    ) -> ConversationContext:
        summary = await self._memory.summary(session_id)
        if history:
            older = await self._memory.unsummarized_before(session_id, history[0].id)
            if older:
                sink.emit(
                    ProgressEvent(stage="memory", detail=f"summarizing {len(older)} messages")
                )
                summary = await self._summarizer.summarize(summary, older)
                await self._memory.save_summary(session_id, summary, older[-1].id)
        return ConversationContext(
            summary=summary,
            state=await self._memory.state(session_id),
            preferences=await self._preferences.get(user_id),
        )

    async def _finish(self, session_id: UUID, reply: AgentReply, trace: TurnTrace) -> TurnOutcome:
        if reply.analysis:
            state = await self._memory.state(session_id)
            await self._memory.save_state(
                session_id, advance_state(state, reply.analysis, self._state_max_steps)
            )
        if reply.confirmation is None:
            ensure_no_pii(reply.text)
            await self._conversations.append(
                session_id, MessageRole.ASSISTANT, reply.text, trace.trace_id
            )
        trace.complete(reply)
        return TurnOutcome(session_id=session_id, reply=reply, trace_id=trace.trace_id)
