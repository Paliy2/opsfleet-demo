import pytest
from pydantic import ValidationError

from opsfleet_agent.core.config import ClientSettings, Environment, ModelProfile, Settings
from opsfleet_agent.core.roles import AgentRole, ReasoningEffort
from opsfleet_agent.gateways.llm.chat_models import model_choice

REQUIRED = {
    "OPSFLEET_ENV": "local",
    "OPSFLEET_GCP_PROJECT": "demo-project",
    "OPSFLEET_LLM_API_KEY": "sk-test",
    "OPSFLEET_DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "OPSFLEET_PII_HASH_SALT": "0123456789abcdef",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.chdir("/")
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


@pytest.mark.usefixtures("env")
def test_defaults_are_safe_for_local() -> None:
    settings = Settings()

    assert settings.env is Environment.LOCAL
    assert settings.model_profile is ModelProfile.FREE_TIER
    assert settings.bq_max_bytes_billed > 0


def test_short_salt_fails_fast(env: pytest.MonkeyPatch) -> None:
    env.setenv("OPSFLEET_PII_HASH_SALT", "short")

    with pytest.raises(ValidationError):
        Settings()


def test_missing_environment_fails_fast(env: pytest.MonkeyPatch) -> None:
    env.delenv("OPSFLEET_ENV")

    with pytest.raises(ValidationError):
        Settings()
    with pytest.raises(ValidationError):
        ClientSettings()


def test_missing_required_fails_fast(env: pytest.MonkeyPatch) -> None:
    env.delenv("OPSFLEET_LLM_API_KEY")

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.usefixtures("env")
def test_secrets_are_not_rendered() -> None:
    settings = Settings()

    assert "sk-test" not in repr(settings)


def test_reasoning_effort_overrides_the_roster(env: pytest.MonkeyPatch) -> None:
    env.setenv("OPSFLEET_REASONING_EFFORT__ANALYST", "medium")
    settings = Settings()

    assert model_choice(settings, AgentRole.ANALYST).reasoning_effort is ReasoningEffort.MEDIUM
    assert model_choice(settings, AgentRole.GUARD).reasoning_effort is ReasoningEffort.LOW


def test_unknown_reasoning_role_fails_fast(env: pytest.MonkeyPatch) -> None:
    env.setenv("OPSFLEET_REASONING_EFFORT__PLANNER", "low")

    with pytest.raises(ValidationError):
        Settings()
