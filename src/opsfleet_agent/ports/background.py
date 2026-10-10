from collections.abc import Awaitable, Callable
from typing import Protocol


class BackgroundTasks(Protocol):
    def spawn(self, key: str, work: Callable[[], Awaitable[None]]) -> None:
        """Run work after the caller returns; skipped while work with the same key is running."""
