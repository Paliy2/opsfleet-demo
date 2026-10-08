from typing import Protocol

from opsfleet_agent.entities.privacy import Redaction


class PiiRedactor(Protocol):
    async def redact(self, text: str) -> Redaction: ...
