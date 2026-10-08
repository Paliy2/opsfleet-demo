import json
import time
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK
from jwt.algorithms import RSAAlgorithm

from opsfleet_agent.api.streaming import TurnRunner, stream_turn
from opsfleet_agent.core.errors import InjectionDetectedError, UnauthenticatedError
from opsfleet_agent.entities.access import Role
from opsfleet_agent.entities.conversation import (
    AgentReply,
    ConfirmationRequest,
    ProgressEvent,
    TurnOutcome,
)
from opsfleet_agent.entities.reports import ReportSummary
from opsfleet_agent.gateways.auth.jwt_verifier import JwtVerifier
from opsfleet_agent.ports.agent import ProgressSink
from tests.fakes import NOW

ISSUER = "https://auth.example/realms/opsfleet"
SESSION = uuid4()


async def collect(runner: TurnRunner) -> list[tuple[str, dict[str, object]]]:
    events = []
    async for chunk in stream_turn(runner):
        name, data = chunk.strip().split("\n")
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def outcome(confirmation: ConfirmationRequest | None = None) -> TurnOutcome:
    return TurnOutcome(
        session_id=SESSION,
        reply=AgentReply(text="done", turn_id=uuid4(), llm_calls=3, confirmation=confirmation),
        trace_id="trace-1",
    )


async def test_stream_emits_progress_then_answer() -> None:
    async def runner(sink: ProgressSink) -> TurnOutcome:
        sink.emit(ProgressEvent(stage="query", detail="revenue"))
        return outcome()

    events = await collect(runner)

    assert [name for name, _ in events] == ["progress", "answer"]
    assert events[0][1]["detail"] == "revenue"
    assert events[1][1] == {
        "session_id": str(SESSION),
        "text": "done",
        "chart_ids": [],
        "trace_id": "trace-1",
        "llm_calls": 3,
    }


async def test_stream_emits_confirmation_with_preview_items() -> None:
    item = ReportSummary(id=uuid4(), title="Q3 review", created_at=NOW)
    request = ConfirmationRequest(turn_id=uuid4(), plan_id=uuid4(), items=(item,), expires_at=NOW)

    async def runner(sink: ProgressSink) -> TurnOutcome:
        return outcome(confirmation=request)

    [(name, data)] = await collect(runner)

    assert name == "confirmation"
    assert data["items"] == [
        {
            "id": str(item.id),
            "title": "Q3 review",
            "created_at": NOW.isoformat().replace("+00:00", "Z"),
        }
    ]


async def test_stream_maps_agent_errors_to_safe_messages() -> None:
    async def runner(sink: ProgressSink) -> TurnOutcome:
        raise InjectionDetectedError("matched injection pattern")

    [(name, data)] = await collect(runner)

    assert name == "error"
    assert data["code"] == "E_INJECTION_DETECTED"
    assert "pattern" not in str(data["message"])


class StaticKeys:
    def __init__(self, signer: rsa.RSAPrivateKey) -> None:
        public = signer.public_key()
        self._key = PyJWK.from_dict(json.loads(RSAAlgorithm.to_jwk(public)), algorithm="RS256")

    def get_signing_key_from_jwt(self, token: str) -> PyJWK:
        return self._key


@pytest.fixture
def signer() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def verifier(signer: rsa.RSAPrivateKey) -> JwtVerifier:
    return JwtVerifier(issuer=ISSUER, audience=None, keys=StaticKeys(signer))


def token(signer: rsa.RSAPrivateKey, **claims: object) -> str:
    body = {"iss": ISSUER, "exp": int(time.time()) + 60, **claims}
    return jwt.encode(body, signer, algorithm="RS256")


async def test_valid_token_yields_username_and_known_roles(signer: rsa.RSAPrivateKey) -> None:
    encoded = token(
        signer,
        preferred_username="mgr_a",
        realm_access={"roles": ["manager", "offline_access"]},
    )

    principal = await verifier(signer).verify(encoded)

    assert principal.user_id == "mgr_a"
    assert principal.roles == frozenset({Role.MANAGER})


@pytest.mark.parametrize(
    "claims",
    [
        {"preferred_username": "ceo", "iss": "https://evil/realms/opsfleet"},
        {"preferred_username": "ceo", "exp": int(time.time()) - 10},
        {},
    ],
)
async def test_invalid_tokens_are_rejected(
    signer: rsa.RSAPrivateKey, claims: dict[str, object]
) -> None:
    with pytest.raises(UnauthenticatedError):
        await verifier(signer).verify(token(signer, **claims))


async def test_token_signed_by_another_key_is_rejected(signer: rsa.RSAPrivateKey) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(UnauthenticatedError):
        await verifier(signer).verify(token(other, preferred_username="ceo"))
