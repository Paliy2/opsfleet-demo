import logging
from functools import partial

import anyio
from langfuse import Langfuse
from langfuse.api import NotFoundError
from langfuse.model import TextPromptClient
from pydantic import ValidationError

from opsfleet_agent.entities.prompts import (
    AgentConfig,
    PromptName,
    PromptOrigin,
    PromptVersion,
    PublishedPrompt,
)
from opsfleet_agent.gateways.observability import metrics
from opsfleet_agent.gateways.observability.turn_context import note_prompt
from opsfleet_agent.gateways.prompts.local import read_local_prompt
from opsfleet_agent.ports.prompts import PromptSource

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SECONDS = 3


def _text_prompt(prompt: object, name: PromptName) -> TextPromptClient:
    if not isinstance(prompt, TextPromptClient):
        raise TypeError(f"Langfuse prompt '{name}' must be a text prompt")
    return prompt


class LangfusePromptSource(PromptSource):
    def __init__(self, client: Langfuse, *, label: str, cache_ttl_seconds: int) -> None:
        self._client = client
        self._label = label
        self._ttl = cache_ttl_seconds

    async def get(self, name: PromptName) -> PromptVersion:
        fetch = partial(
            self._client.get_prompt,
            name.value,
            label=self._label,
            cache_ttl_seconds=self._ttl,
            fallback=read_local_prompt(name),
            max_retries=1,
            fetch_timeout_seconds=_FETCH_TIMEOUT_SECONDS,
        )
        prompt = _text_prompt(await anyio.to_thread.run_sync(fetch), name)
        version = (
            PromptVersion(name=name, text=prompt.prompt, origin=PromptOrigin.LOCAL)
            if prompt.is_fallback
            else PromptVersion(
                name=name,
                text=prompt.prompt,
                origin=PromptOrigin.LANGFUSE,
                version=prompt.version,
                config=self._config(name, prompt.config),
            )
        )
        metrics.PROMPT_FETCHES.labels(name.value, version.origin.value).inc()
        note_prompt(version)
        return version

    def _config(self, name: PromptName, raw: object) -> AgentConfig:
        try:
            return AgentConfig.model_validate(raw or {})
        except ValidationError as error:
            metrics.PROMPT_CONFIG_INVALID.labels(name.value).inc()
            logger.warning("invalid agent config for %s, using defaults: %s", name, error)
            return AgentConfig()

    async def publish_local(self, label: str) -> list[PublishedPrompt]:
        return [await anyio.to_thread.run_sync(self._publish, name, label) for name in PromptName]

    def _publish(self, name: PromptName, label: str) -> PublishedPrompt:
        text = read_local_prompt(name)
        try:
            current = _text_prompt(
                self._client.get_prompt(name.value, label=label, cache_ttl_seconds=0), name
            )
        except NotFoundError:
            current = None
        if current is not None and current.prompt == text:
            return PublishedPrompt(name=name, version=current.version, changed=False)
        created = self._client.create_prompt(
            name=name.value,
            prompt=text,
            labels=[label],
            type="text",
            config=current.config if current else {},
            commit_message="sync from repository",
        )
        return PublishedPrompt(name=name, version=created.version, changed=True)
