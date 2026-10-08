from functools import cache
from importlib.resources import files

from opsfleet_agent.entities.prompts import PromptName, PromptOrigin, PromptVersion
from opsfleet_agent.gateways.observability.turn_context import note_prompt
from opsfleet_agent.ports.prompts import PromptSource


@cache
def read_local_prompt(name: PromptName) -> str:
    return files("opsfleet_agent.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


class LocalPromptSource(PromptSource):
    async def get(self, name: PromptName) -> PromptVersion:
        version = PromptVersion(name=name, text=read_local_prompt(name), origin=PromptOrigin.LOCAL)
        note_prompt(version)
        return version
