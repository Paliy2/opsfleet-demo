from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from opsfleet_agent.agents.prompting import render
from opsfleet_agent.entities.conversation import ChatMessage
from opsfleet_agent.entities.prompts import PromptName
from opsfleet_agent.gateways.llm.chat_models import llm_errors
from opsfleet_agent.ports.context import ConversationSummarizer
from opsfleet_agent.ports.prompts import PromptSource


class LlmSummarizer(ConversationSummarizer):
    def __init__(self, model: BaseChatModel, prompts: PromptSource, max_chars: int) -> None:
        self._model = model
        self._prompts = prompts
        self._max_chars = max_chars

    async def summarize(self, previous: str, messages: Sequence[ChatMessage]) -> str:
        prompt = await self._prompts.get(PromptName.SUMMARIZER)
        transcript = "\n\n".join(f"{m.role}: {m.content}" for m in messages)
        brief = f"# Previous summary\n{previous or '(none)'}\n\n# New messages\n{transcript}"
        with llm_errors():
            reply = await self._model.ainvoke(
                [
                    SystemMessage(render(prompt.text, max_chars=str(self._max_chars))),
                    HumanMessage(brief),
                ],
                config={"run_name": "summarizer"},
            )
        return str(reply.content)[: self._max_chars]
