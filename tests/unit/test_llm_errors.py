import httpx2
import openai
import pytest

from opsfleet_agent.core.errors import (
    AgentError,
    LlmBudgetExceededError,
    LlmRateLimitedError,
    LlmUnavailableError,
)
from opsfleet_agent.gateways.llm.chat_models import llm_errors

BUDGET_BODY = {
    "type": "budget_exceeded",
    "status_code": 402,
    "error": {"message": "Budget exceeded: vk data-agent budget exceeded: 6.0 >= 5.0 dollars"},
}
RATE_BODY = {
    "type": "rate_limited",
    "status_code": 429,
    "error": {"message": "Rate limit exceeded: rate limit violated for vk data-agent"},
}
BILLING_BODY = {"error": {"message": "provider billing account disabled"}}


def gateway_error(status: int, body: dict[str, object]) -> openai.APIStatusError:
    request = httpx2.Request("POST", "http://bifrost/v1/chat/completions")
    response = httpx2.Response(status, json=body, request=request)
    return openai.APIStatusError(
        f"Error code: {status}", response=response, body=body.get("error", body)
    )


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (402, BUDGET_BODY, LlmBudgetExceededError),
        (429, RATE_BODY, LlmRateLimitedError),
        (402, BILLING_BODY, LlmUnavailableError),
        (503, BILLING_BODY, LlmUnavailableError),
    ],
)
def test_gateway_status_maps_to_typed_error(
    status: int, body: dict[str, object], expected: type[AgentError]
) -> None:
    with pytest.raises(expected), llm_errors():
        raise gateway_error(status, body)


def test_budget_exhaustion_has_a_user_safe_message() -> None:
    with pytest.raises(LlmBudgetExceededError) as raised, llm_errors():
        raise gateway_error(402, BUDGET_BODY)

    assert "daily AI budget" in raised.value.user_message
    assert "data-agent" not in raised.value.user_message
