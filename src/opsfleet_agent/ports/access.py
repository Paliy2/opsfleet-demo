from collections.abc import Iterable
from typing import Protocol

from opsfleet_agent.entities.access import AccessScope, Role


class AccessRepo(Protocol):
    async def get_scope(self, user_id: str) -> AccessScope: ...

    async def upsert_user(
        self, user_id: str, role: Role, display_name: str, brands: Iterable[str]
    ) -> None: ...
