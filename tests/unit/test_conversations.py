from uuid import uuid4

import pytest

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.conversation import MessageRole
from opsfleet_agent.gateways.observability.telemetry import TracingTelemetry
from opsfleet_agent.interactors.chat_turn import ChatTurn
from opsfleet_agent.interactors.conversations import (
    TITLE_MAX_CHARS,
    UNTITLED,
    DeleteConversation,
    GetConversation,
    ListConversations,
    headline,
)
from opsfleet_agent.interactors.pii_redaction import RegexRedactor
from tests.fakes import (
    FakeMemory,
    FakePreferences,
    FakeSummarizer,
    MemoryConversations,
    MemoryPiiTokenVault,
    RecordingProgress,
    privacy_tokens,
    value_vault,
)
from tests.unit.test_chat_turn import FakeAccess, FakeAgent, FakeGuard


def chat(conversations: MemoryConversations, tokens: MemoryPiiTokenVault | None = None) -> ChatTurn:
    return ChatTurn(
        access=FakeAccess(),
        conversations=conversations,
        redactor=RegexRedactor(),
        tokens=privacy_tokens(tokens),
        values=value_vault(),
        guard=FakeGuard(),
        agent=FakeAgent(),
        telemetry=TracingTelemetry(
            None, salt="test-salt-1234567", environment="test", turn_token_budget=1_000
        ),
        memory=FakeMemory(),
        preferences=FakePreferences(),
        summarizer=FakeSummarizer(),
        history_max_turns=20,
        history_max_chars=20_000,
        state_max_steps=5,
    )


async def seeded() -> MemoryConversations:
    store = MemoryConversations()
    for owner, question in (("mgr_a", "Top brands by revenue"), ("mgr_b", "Their secret plan")):
        session = await store.create_session(owner)
        await store.append(session.id, MessageRole.USER, question)
        await store.append(session.id, MessageRole.ASSISTANT, "Answer.", "a" * 32)
    await store.create_session("mgr_a")
    return store


def test_headline_collapses_whitespace_and_truncates() -> None:
    long = "word " * 40

    assert headline("  Top\n brands   by revenue ") == "Top brands by revenue"
    assert len(headline(long)) <= TITLE_MAX_CHARS
    assert headline(long).endswith("…")
    assert headline("   ") == UNTITLED


async def test_list_shows_only_own_conversations_with_messages() -> None:
    store = await seeded()

    listed = await ListConversations(store)("mgr_a", limit=20)

    assert [item.title for item in listed] == ["Top brands by revenue"]


async def test_owner_reads_transcript_in_order() -> None:
    store = await seeded()
    mine = (await ListConversations(store)("mgr_a", limit=20))[0]

    messages = await GetConversation(store)("mgr_a", mine.id)

    assert [message.role for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert messages[1].trace_id == "a" * 32


async def test_red_team_cannot_read_another_users_transcript() -> None:
    store = await seeded()
    theirs = (await ListConversations(store)("mgr_b", limit=20))[0]

    with pytest.raises(NotFoundError):
        await GetConversation(store)("mgr_a", theirs.id)


async def test_red_team_cannot_delete_another_users_conversation() -> None:
    store = await seeded()
    theirs = (await ListConversations(store)("mgr_b", limit=20))[0]

    with pytest.raises(NotFoundError):
        await DeleteConversation(store, value_vault())("mgr_a", theirs.id)
    assert store.deleted == []
    assert await ListConversations(store)("mgr_b", limit=20)


async def test_owner_deletes_conversation() -> None:
    store = await seeded()
    mine = (await ListConversations(store)("mgr_a", limit=20))[0]

    await DeleteConversation(store, value_vault())("mgr_a", mine.id)

    assert store.deleted == [mine.id]
    assert await ListConversations(store)("mgr_a", limit=20) == ()


async def test_open_session_uses_the_requested_id_and_resumes_it() -> None:
    store = MemoryConversations()
    requested = uuid4()

    created = await chat(store).open_session("mgr_a", requested)
    resumed = await chat(store).open_session("mgr_a", requested)

    assert created == resumed == requested
    assert list(store.owners) == [requested]


async def test_red_team_cannot_open_another_users_session_id() -> None:
    store = await seeded()
    theirs = (await ListConversations(store)("mgr_b", limit=20))[0]

    with pytest.raises(NotFoundError):
        await chat(store).open_session("mgr_a", theirs.id)
    assert store.owners[theirs.id] == "mgr_b"


async def test_red_team_numbering_never_writes_into_another_users_session() -> None:
    store, tokens = MemoryConversations(), MemoryPiiTokenVault()
    theirs = (await store.create_session("mgr_b")).id

    with pytest.raises(NotFoundError):
        await chat(store, tokens)("mgr_a", theirs, "mail jane.roe@example.org", RecordingProgress())

    assert tokens.books == {}
