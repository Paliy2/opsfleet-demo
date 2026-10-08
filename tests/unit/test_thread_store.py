from uuid import UUID

import httpx
import pytest
from chainlit.types import Pagination, ThreadFilter
from chainlit.user import User

from opsfleet_agent.core.errors import UnauthenticatedError
from opsfleet_agent.gateways.analyst.client import ApiError
from opsfleet_agent.ui.threads import ApiThreadStore, api_headers, run_id, trace_of
from tests.fakes import MemoryConversations
from tests.unit.test_session_api import RecordingFeedback, seeded_store, session_api

TRACE = "b" * 32


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


def test_headers_prefer_the_users_bearer_token() -> None:
    user = User(identifier="ceo", metadata={"access_token": "tok"})

    assert api_headers(user) == {"Authorization": "Bearer tok"}
    assert api_headers(User(identifier="ceo")) == {"X-Demo-User": "ceo"}


async def test_users_are_never_persisted_but_keep_their_metadata(
    threads: ApiThreadStore,
) -> None:
    user = User(identifier="mgr_a", metadata={"access_token": "tok"})

    persisted = await threads.create_user(user)

    assert await threads.get_user("mgr_a") is None
    assert persisted.id == "mgr_a"
    assert persisted.metadata == {"access_token": "tok"}


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
