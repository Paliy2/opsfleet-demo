import asyncio
from dataclasses import dataclass
from functools import partial
from uuid import UUID

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.context import ConversationContext
from opsfleet_agent.entities.conversation import (
    AgentReply,
    AgentRequest,
    MessageRole,
    ProgressEvent,
    TurnOutcome,
)
from opsfleet_agent.entities.pii_tokens import TurnValue
from opsfleet_agent.entities.telemetry import TurnKind
from opsfleet_agent.interactors.context import advance_state
from opsfleet_agent.interactors.pii_scan import ensure_no_pii
from opsfleet_agent.interactors.pii_tokens import PrivacyTokens
from opsfleet_agent.ports.access import AccessRepo
from opsfleet_agent.ports.agent import AnalysisAgent, InputGuard, ProgressSink
from opsfleet_agent.ports.background import BackgroundTasks
from opsfleet_agent.ports.context import ConversationSummarizer, PreferenceRepo, SessionMemory
from opsfleet_agent.ports.conversations import ConversationRepo
from opsfleet_agent.ports.pii_token_vault import PiiValueVault
from opsfleet_agent.ports.privacy import PiiRedactor
from opsfleet_agent.ports.telemetry import Telemetry, TurnTrace


@dataclass(frozen=True)
class _TracedProgress:
    progress: ProgressSink
    trace: TurnTrace

    def emit(self, event: ProgressEvent) -> None:
        self.trace.emit(event)
        self.progress.emit(event)


def _describe(values: tuple[TurnValue, ...]) -> str:
    labels = dict.fromkeys((value.info_type, value.label) for value in values)
    return ", ".join(f"{kind.lower().replace('_', ' ')} [{label}]" for kind, label in labels)


class ChatTurn:
    def __init__(
        self,
        *,
        access: AccessRepo,
        conversations: ConversationRepo,
        redactor: PiiRedactor,
        tokens: PrivacyTokens,
        values: PiiValueVault,
        guard: InputGuard,
        agent: AnalysisAgent,
        telemetry: Telemetry,
        memory: SessionMemory,
        preferences: PreferenceRepo,
        summarizer: ConversationSummarizer,
        background: BackgroundTasks,
        history_max_turns: int,
        history_max_chars: int,
        state_max_steps: int,
    ) -> None:
        self._memory = memory
        self._preferences = preferences
        self._summarizer = summarizer
        self._background = background
        self._state_max_steps = state_max_steps
        self._access = access
        self._conversations = conversations
        self._redactor = redactor
        self._tokens = tokens
        self._values = values
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
        redaction, session = await asyncio.gather(
            self._redactor.redact(question), self._conversations.get_session(session_id, user_id)
        )
        tokenized = await self._tokens.number(session.id, question, redaction)
        question = tokenized.text
        with self._telemetry.turn(TurnKind.QUESTION, user_id, session_id, question) as trace:
            sink = _TracedProgress(progress, trace)
            scope = await self._access.get_scope(user_id)
            if tokenized.values:
                sink.emit(ProgressEvent(stage="privacy", detail=_describe(tokenized.values)))
            sink.emit(ProgressEvent(stage="guard"))
            await self._guard.check(question)
            await self._values.put(user_id, session.id, tokenized.values)
            history = await self._conversations.recent(
                session.id, self._history_max_turns, self._history_max_chars
            )
            context = await self._context(session.id, user_id)
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

    async def _context(self, session_id: UUID, user_id: str) -> ConversationContext:
        summary, state, preferences = await asyncio.gather(
            self._memory.summary(session_id),
            self._memory.state(session_id),
            self._preferences.get(user_id),
        )
        return ConversationContext(summary=summary, state=state, preferences=preferences)

    async def _summarize_overflow(self, session_id: UUID) -> None:
        history = await self._conversations.recent(
            session_id, self._history_max_turns, self._history_max_chars
        )
        if not history:
            return
        older = await self._memory.unsummarized_before(session_id, history[0].id)
        if not older:
            return
        summary = await self._summarizer.summarize(await self._memory.summary(session_id), older)
        await self._memory.save_summary(session_id, summary, older[-1].id)

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
        self._background.spawn(
            f"summary:{session_id}", partial(self._summarize_overflow, session_id)
        )
        return TurnOutcome(session_id=session_id, reply=reply, trace_id=trace.trace_id)
