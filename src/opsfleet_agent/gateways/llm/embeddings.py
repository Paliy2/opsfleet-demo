from collections.abc import Sequence

from openai import AsyncOpenAI

from opsfleet_agent.core.config import Settings
from opsfleet_agent.core.models import EMBEDDING_DIMENSIONS
from opsfleet_agent.gateways.llm.chat_models import llm_errors
from opsfleet_agent.ports.golden import Embedder

_BATCH_SIZE = 64


class GatewayEmbedder(Embedder):
    def __init__(self, settings: Settings) -> None:
        self._client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=1,
        )
        self._model = settings.embedding_model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            with llm_errors():
                response = await self._client.embeddings.create(
                    model=self._model,
                    input=list(texts[start : start + _BATCH_SIZE]),
                    dimensions=EMBEDDING_DIMENSIONS,
                )
            vectors.extend(item.embedding for item in response.data)
        return vectors
