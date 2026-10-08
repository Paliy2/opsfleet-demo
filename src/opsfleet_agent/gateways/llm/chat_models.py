from collections.abc import Iterator
from contextlib import contextmanager

import openai
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from opsfleet_agent.core.config import Settings
from opsfleet_agent.core.errors import (
    AgentError,
    LlmBudgetExceededError,
    LlmRateLimitedError,
    LlmTimeoutError,
    LlmUnavailableError,
)
from opsfleet_agent.core.models import ROSTERS, ModelChoice
from opsfleet_agent.core.roles import AgentRole

_CREDENTIAL_FAILURES = frozenset({401, 402, 403})
_BUDGET_EXCEEDED = "budget_exceeded"
_RATE_LIMITED = 429


def model_choice(settings: Settings, role: AgentRole) -> ModelChoice:
    choice = ROSTERS[settings.model_profile][role]
    if role in settings.reasoning_effort:
        return choice.thinking(settings.reasoning_effort[role])
    return choice


def build_chat_model(settings: Settings, role: AgentRole) -> ChatOpenAI:
    choice = model_choice(settings, role)
    return ChatOpenAI(
        model=choice.primary,
        base_url=settings.llm_base_url,
        api_key=SecretStr(settings.llm_api_key.get_secret_value()),
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
        temperature=0,
        reasoning_effort=choice.reasoning_effort,
        extra_body={"fallbacks": list(choice.fallbacks)},
    )


@contextmanager
def llm_errors() -> Iterator[None]:
    try:
        yield
    except openai.APITimeoutError as error:
        raise LlmTimeoutError(str(error)) from error
    except openai.APIStatusError as error:
        raise _status_error(error) from error
    except openai.APIConnectionError as error:
        raise LlmUnavailableError(f"gateway unreachable: {error}") from error


def _status_error(error: openai.APIStatusError) -> AgentError:
    detail = f"gateway status {error.status_code}: {error.message}"
    if _BUDGET_EXCEEDED in error.response.text:
        return LlmBudgetExceededError(detail)
    if error.status_code == _RATE_LIMITED:
        return LlmRateLimitedError(detail)
    if error.status_code in _CREDENTIAL_FAILURES or "credentials_exhausted" in str(error.body):
        return LlmUnavailableError(f"credentials or billing failure, fallback skipped ({detail})")
    return LlmUnavailableError(detail)
