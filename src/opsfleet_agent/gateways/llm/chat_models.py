from collections.abc import Iterator
from contextlib import contextmanager

import openai
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from opsfleet_agent.core.config import Settings
from opsfleet_agent.core.errors import LlmTimeoutError, LlmUnavailableError
from opsfleet_agent.core.models import ROSTERS, AgentRole

_CREDENTIAL_FAILURES = frozenset({401, 402, 403})


def build_chat_model(settings: Settings, role: AgentRole) -> ChatOpenAI:
    choice = ROSTERS[settings.model_profile][role]
    return ChatOpenAI(
        model=choice.primary,
        base_url=settings.llm_base_url,
        api_key=SecretStr(settings.llm_api_key.get_secret_value()),
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
        temperature=0,
        extra_body={"fallbacks": list(choice.fallbacks)},
    )


@contextmanager
def llm_errors() -> Iterator[None]:
    try:
        yield
    except openai.APITimeoutError as error:
        raise LlmTimeoutError(str(error)) from error
    except openai.APIStatusError as error:
        detail = f"gateway status {error.status_code}: {error.message}"
        if error.status_code in _CREDENTIAL_FAILURES or "credentials_exhausted" in str(error.body):
            detail = f"credentials or billing failure, fallback skipped ({detail})"
        raise LlmUnavailableError(detail) from error
    except openai.APIConnectionError as error:
        raise LlmUnavailableError(f"gateway unreachable: {error}") from error
