from typing import Protocol

from opsfleet_agent.entities.prompts import PromptName, PromptVersion


class PromptSource(Protocol):
    async def get(self, name: PromptName) -> PromptVersion: ...
