import json
from uuid import UUID

import httpx
import jwt
import pytest
from chainlit.auth.jwt import create_jwt
from chainlit.types import Pagination, ThreadFilter
from chainlit.user import User

from opsfleet_agent.core.errors import UnauthenticatedError
from opsfleet_agent.gateways.analyst.client import ApiError
from opsfleet_agent.ui.threads import (
    ApiThreadStore,
    api_headers,
    keycloak_user,
    run_id,
    trace_of,
)
from opsfleet_agent.ui.tokens import SIGN_IN_HANDLE, TokenVault
from tests.fakes import MemoryConversations
from tests.unit.test_session_api import RecordingFeedback, seeded_store, session_api

TRACE = "b" * 32
SECRET = "keycloak-access-token"


@pytest.fixture
async def store() -> MemoryConversations:
    return await seeded_store()


@pytest.fixture
def votes() -> RecordingFeedback:
    return RecordingFeedback()


@pytest.fixture
def threads(store: MemoryConversations, votes: RecordingFeedback) -> ApiThreadStore:
    transport = httpx.ASGITransport(app=session_api(store, votes))
    return ApiThreadStore("http://api", transport)


def owned_by(store: MemoryConversations, user_id: str) -> str:
    return str(next(key for key, owner in store.owners.items() if owner == user_id))


async def signed_in(threads: ApiThreadStore, user_id: str) -> None:
    await threads.create_user(User(identifier=user_id, metadata={"provider": "local"}))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_headers_resolve_the_bearer_token_server_side() -> None:
    vault = TokenVault(3600)
    user = keycloak_user("ceo", SECRET, vault)

    assert api_headers(user, vault) == {"Authorization": f"Bearer {SECRET}"}
    assert api_headers(User(identifier="ceo"), vault) == {"X-Demo-User": "ceo"}


def test_keycloak_user_exposes_no_token_to_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "s" * 32)
    user = keycloak_user("ceo", SECRET, TokenVault(3600))

    cookie = jwt.decode(create_jwt(user), options={"verify_signature": False})

    assert SECRET not in json.dumps(user.to_dict())
    assert SECRET not in json.dumps(cookie)
    assert set(user.metadata) == {SIGN_IN_HANDLE, "provider"}


def test_a_token_missing_after_a_restart_requires_sign_in() -> None:
    user = keycloak_user("ceo", SECRET, TokenVault(3600))

    with pytest.raises(UnauthenticatedError):
        api_headers(user, TokenVault(3600))


def test_an_expired_token_requires_sign_in() -> None:
    clock = Clock()
    vault = TokenVault(3600, clock)
    user = keycloak_user("ceo", SECRET, vault)
    clock.now = 3600

    with pytest.raises(UnauthenticatedError):
        api_headers(user, vault)


def test_a_handle_works_only_for_its_owner() -> None:
    vault = TokenVault(3600)
    handle = keycloak_user("ceo", SECRET, vault).metadata[SIGN_IN_HANDLE]
    impostor = User(identifier="mgr_a", metadata={SIGN_IN_HANDLE: handle})

    with pytest.raises(UnauthenticatedError):
        api_headers(impostor, vault)


def test_expired_tokens_are_dropped_on_the_next_sign_in() -> None:
    clock = Clock()
    vault = TokenVault(3600, clock)
    stale = keycloak_user("ceo", SECRET, vault)
    clock.now = 3600
    fresh = keycloak_user("ceo", "renewed", vault)
    clock.now = 0

    assert api_headers(fresh, vault) == {"Authorization": "Bearer renewed"}
    with pytest.raises(UnauthenticatedError):
        api_headers(stale, vault)


async def test_users_are_never_persisted_and_carry_no_token(threads: ApiThreadStore) -> None:
    persisted = await threads.create_user(keycloak_user("mgr_a", SECRET, TokenVault(3600)))

    assert await threads.get_user("mgr_a") is None
    assert persisted.id == "mgr_a"
    assert SECRET not in json.dumps(persisted.to_dict())


async def test_the_sidebar_signs_out_when_the_server_lost_the_token(
    threads: ApiThreadStore,
) -> None:
    await threads.create_user(keycloak_user("mgr_a", SECRET, TokenVault(3600)))

    with pytest.raises(UnauthenticatedError):
        await threads.list_threads(Pagination(first=20), ThreadFilter())


async def test_list_threads_shows_the_callers_conversations(threads: ApiThreadStore) -> None:
    await signed_in(threads, "mgr_a")

    page = await threads.list_threads(Pagination(first=20), ThreadFilter())

    assert [thread["name"] for thread in page.data] == ["question from mgr_a"]
    assert page.pageInfo.hasNextPage is False


async def test_search_filters_titles(threads: ApiThreadStore) -> None:
    await signed_in(threads, "mgr_a")

    page = await threads.list_threads(Pagination(first=20), ThreadFilter(search="nothing"))

    assert page.data == []


async def test_get_thread_rebuilds_steps_with_trace_ids(
    threads: ApiThreadStore, store: MemoryConversations
) -> None:
    await signed_in(threads, "mgr_a")
    mine = owned_by(store, "mgr_a")

    thread = await threads.get_thread(mine)

    assert thread is not None
    assert thread["userIdentifier"] == "mgr_a"
    user, run, answer = thread["steps"]
    assert [user["type"], run["type"], answer["type"]] == [
        "user_message",
        "run",
        "assistant_message",
    ]
    assert run["id"] == run_id(TRACE)
    assert run["parentId"] == user["id"]
    assert answer["parentId"] == run["id"]
    assert await threads.get_thread_author(mine) == "mgr_a"


async def test_red_team_other_users_thread_is_invisible(
    threads: ApiThreadStore, store: MemoryConversations
) -> None:
    await signed_in(threads, "mgr_a")
    theirs = owned_by(store, "mgr_b")

    assert await threads.get_thread(theirs) is None
    assert await threads.get_thread_author(theirs) == ""
    with pytest.raises(ApiError):
        await threads.delete_thread(theirs)
    assert UUID(theirs) in store.owners


async def test_owner_deletes_thread(threads: ApiThreadStore, store: MemoryConversations) -> None:
    await signed_in(threads, "mgr_a")
    mine = owned_by(store, "mgr_a")

    await threads.delete_thread(mine)

    assert store.deleted == [UUID(mine)]


def test_feedback_run_maps_back_to_its_trace() -> None:
    live = {"run-1": "c" * 32}

    assert trace_of("run-1", live) == "c" * 32
    assert trace_of(run_id(TRACE), live) == TRACE


async def test_without_a_signed_in_user_the_store_refuses(threads: ApiThreadStore) -> None:
    with pytest.raises(UnauthenticatedError):
        await threads.list_threads(Pagination(first=20), ThreadFilter())


def expired_token(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        401, json={"code": "E_UNAUTHENTICATED", "message": "Please sign in again."}
    )


async def test_an_expired_token_surfaces_as_unauthenticated() -> None:
    threads = ApiThreadStore("http://api", httpx.MockTransport(expired_token))
    await signed_in(threads, "mgr_a")

    with pytest.raises(UnauthenticatedError):
        await threads.list_threads(Pagination(first=20), ThreadFilter())
    with pytest.raises(UnauthenticatedError):
        await threads.get_thread(str(UUID(int=1)))
