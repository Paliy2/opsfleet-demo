import pytest
from pydantic import ValidationError

from opsfleet_agent.core.config import Environment, ModelProfile, Settings

REQUIRED = {
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


def test_missing_required_fails_fast(env: pytest.MonkeyPatch) -> None:
    env.delenv("OPSFLEET_LLM_API_KEY")

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.usefixtures("env")
def test_secrets_are_not_rendered() -> None:
    settings = Settings()

    assert "sk-test" not in repr(settings)
