from dataclasses import dataclass, field
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opsfleet_agent.api.app import Services, _agent_error, _routes
from opsfleet_agent.core.config import Environment
from opsfleet_agent.core.errors import AgentError
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.entities.conversation import MessageRole
from opsfleet_agent.gateways.ratelimit.redis_limiter import UnlimitedRateLimiter
from opsfleet_agent.interactors.conversations import (
    DeleteConversation,
    GetConversation,
    ListConversations,
)
from opsfleet_agent.wiring import App
from tests.fakes import MemoryConversations, value_vault


@dataclass(frozen=True)
class SessionChat:
    store: MemoryConversations

    async def scope(self, user_id: str) -> AccessScope:
        return AccessScope(user_id=user_id, role=Role.MANAGER, brands=frozenset({"Diesel"}))

    async def open_session(self, user_id: str, session_id: UUID | None = None) -> UUID:
        return (await self.store.create_session(user_id, session_id)).id


@dataclass
class RecordingFeedback:
    votes: list[tuple[str, UUID, str, str, str | None]] = field(default_factory=list)

    async def __call__(
        self, user_id: str, session_id: UUID, trace_id: str, rating: str, comment: str | None
    ) -> None:
        self.votes.append((user_id, session_id, trace_id, rating, comment))


@dataclass(frozen=True)
class SessionApp:
    chat: SessionChat
    list_conversations: ListConversations
    get_conversation: GetConversation
    delete_conversation: DeleteConversation
    feedback: RecordingFeedback = field(default_factory=RecordingFeedback)


def session_api(store: MemoryConversations, feedback: RecordingFeedback) -> FastAPI:
    api = FastAPI()
    api.add_exception_handler(AgentError, _agent_error)
    _routes(api)
    stub = SessionApp(
        SessionChat(store),
        ListConversations(store),
        GetConversation(store),
        DeleteConversation(store, value_vault()),
        feedback,
    )
    api.state.services = Services(
        app=cast(App, stub),
        verifier=None,
        limiter=UnlimitedRateLimiter(),
        environment=Environment.LOCAL,
        auth=None,
    )
    return api


async def seeded_store() -> MemoryConversations:
    conversations = MemoryConversations()
    for owner in ("mgr_a", "mgr_b"):
        session = await conversations.create_session(owner)
        await conversations.append(session.id, MessageRole.USER, f"question from {owner}")
        await conversations.append(session.id, MessageRole.ASSISTANT, "answer", "b" * 32)
    return conversations


@pytest.fixture
async def store() -> MemoryConversations:
    return await seeded_store()


@pytest.fixture
def client(store: MemoryConversations) -> TestClient:
    return TestClient(session_api(store, RecordingFeedback()))


def as_user(user_id: str) -> dict[str, str]:
    return {"X-Demo-User": user_id}


def owned_by(store: MemoryConversations, user_id: str) -> UUID:
    return next(key for key, owner in store.owners.items() if owner == user_id)


def test_list_shows_only_own_sessions(client: TestClient) -> None:
    response = client.get("/v1/sessions", headers=as_user("mgr_a"))

    assert response.status_code == 200
    assert [item["title"] for item in response.json()["sessions"]] == ["question from mgr_a"]


def test_list_rejects_oversized_page(client: TestClient) -> None:
    response = client.get("/v1/sessions?limit=500", headers=as_user("mgr_a"))

    assert response.status_code == 422


def test_owner_reads_messages(client: TestClient, store: MemoryConversations) -> None:
    mine = owned_by(store, "mgr_a")

    response = client.get(f"/v1/sessions/{mine}/messages", headers=as_user("mgr_a"))

    assert response.status_code == 200
    assert [m["role"] for m in response.json()["messages"]] == ["user", "assistant"]
    assert response.json()["messages"][1]["trace_id"] == "b" * 32


def test_red_team_other_user_cannot_read_or_delete(
    client: TestClient, store: MemoryConversations
) -> None:
    theirs = owned_by(store, "mgr_b")

    read = client.get(f"/v1/sessions/{theirs}/messages", headers=as_user("mgr_a"))
    removed = client.delete(f"/v1/sessions/{theirs}", headers=as_user("mgr_a"))

    assert read.status_code == 404
    assert removed.status_code == 404
    assert "question from mgr_b" not in read.text
    assert theirs in store.owners


def test_owner_deletes_session(client: TestClient, store: MemoryConversations) -> None:
    mine = owned_by(store, "mgr_a")

    response = client.delete(f"/v1/sessions/{mine}", headers=as_user("mgr_a"))

    assert response.status_code == 204
    assert store.deleted == [mine]


def test_open_session_accepts_client_id_and_works_without_body(client: TestClient) -> None:
    requested = uuid4()

    chosen = client.post("/v1/sessions", json={"session_id": str(requested)}, headers=as_user("u"))
    generated = client.post("/v1/sessions", headers=as_user("u"))

    assert chosen.status_code == 201
    assert chosen.json()["session_id"] == str(requested)
    assert generated.status_code == 201


def test_red_team_cannot_claim_another_users_session_id(
    client: TestClient, store: MemoryConversations
) -> None:
    theirs = owned_by(store, "mgr_b")

    response = client.post(
        "/v1/sessions", json={"session_id": str(theirs)}, headers=as_user("mgr_a")
    )

    assert response.status_code == 404
    assert store.owners[theirs] == "mgr_b"
