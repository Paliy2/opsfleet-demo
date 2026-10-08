import re
from collections.abc import Sequence
from typing import Literal

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from opsfleet_agent.core.errors import InjectionDetectedError, OffTopicError
from opsfleet_agent.entities.prompts import PromptName
from opsfleet_agent.gateways.llm.chat_models import llm_errors
from opsfleet_agent.ports.agent import InputGuard
from opsfleet_agent.ports.prompts import PromptSource

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
    def __init__(
        self,
        model: BaseChatModel,
        prompts: PromptSource,
        callbacks: Sequence[BaseCallbackHandler] = (),
    ) -> None:
        self._classifier = model.with_structured_output(GuardVerdict, method="function_calling")
        self._prompts = prompts
        self._callbacks = list(callbacks)

    async def check(self, text: str) -> None:
        if _INJECTION_PATTERNS.search(text):
            raise InjectionDetectedError("matched injection pattern")
        instructions = await self._prompts.get(PromptName.GUARD)
        with llm_errors():
            raw = await self._classifier.ainvoke(
                [SystemMessage(instructions.text), HumanMessage(text)],
                config={"run_name": "guard", "callbacks": self._callbacks},
            )
        verdict = GuardVerdict.model_validate(raw)
        if verdict.category == "injection":
            raise InjectionDetectedError(verdict.reason)
        if verdict.category == "off_topic":
            raise OffTopicError(verdict.reason)
