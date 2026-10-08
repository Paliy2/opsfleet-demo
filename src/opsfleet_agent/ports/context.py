from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from opsfleet_agent.entities.context import AnalysisState, UserPreferences
from opsfleet_agent.entities.conversation import ChatMessage


class PreferenceRepo(Protocol):
    async def get(self, user_id: str) -> UserPreferences: ...

    async def save(self, user_id: str, preferences: UserPreferences) -> None: ...


class SessionMemory(Protocol):
    async def summary(self, session_id: UUID) -> str: ...

    async def unsummarized_before(
        self, session_id: UUID, first_kept_id: int
    ) -> tuple[ChatMessage, ...]:
        """Messages older than the history window that the summary does not cover yet."""

    async def save_summary(self, session_id: UUID, summary: str, through_id: int) -> None: ...

    async def state(self, session_id: UUID) -> AnalysisState: ...

    async def save_state(self, session_id: UUID, state: AnalysisState) -> None: ...


class ConversationSummarizer(Protocol):
    async def summarize(self, previous: str, messages: Sequence[ChatMessage]) -> str: ...
