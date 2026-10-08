import re
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from opsfleet_agent.agents.prompts import load_prompt
from opsfleet_agent.core.errors import InjectionDetectedError, OffTopicError
from opsfleet_agent.gateways.llm.chat_models import llm_errors
from opsfleet_agent.ports.agent import InputGuard

_INJECTION_PATTERNS = re.compile(
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)"
    r"|(reveal|show|print|repeat) (me )?(your|the) (system )?(prompt|instructions)"
    r"|you are now|developer mode|jailbreak",
    re.IGNORECASE,
)


class GuardVerdict(BaseModel):
    category: Literal["analysis", "meta", "conversation", "off_topic", "injection"]
    reason: str


class LlmInputGuard(InputGuard):
    def __init__(self, model: BaseChatModel) -> None:
        self._classifier = model.with_structured_output(GuardVerdict, method="function_calling")
        self._instructions = load_prompt("guard")

    async def check(self, text: str) -> None:
        if _INJECTION_PATTERNS.search(text):
            raise InjectionDetectedError("matched injection pattern")
        with llm_errors():
            raw = await self._classifier.ainvoke(
                [SystemMessage(self._instructions), HumanMessage(text)]
            )
        verdict = GuardVerdict.model_validate(raw)
        if verdict.category == "injection":
            raise InjectionDetectedError(verdict.reason)
        if verdict.category == "off_topic":
            raise OffTopicError(verdict.reason)
