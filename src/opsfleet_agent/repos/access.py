from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.models.tables import AppUser, Entitlement
from opsfleet_agent.ports.access import AccessRepo


class SqlAccessRepo(AccessRepo):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_scope(self, user_id: str) -> AccessScope:
        async with self._sessions() as session:
            user = await session.get(AppUser, user_id)
            if user is None:
                raise NotFoundError(f"unknown user {user_id}")
            brands = await session.scalars(
                select(Entitlement.brand).where(Entitlement.user_id == user_id)
            )
            return AccessScope(user_id=user.id, role=Role(user.role), brands=frozenset(brands))

    async def upsert_user(
        self, user_id: str, role: Role, display_name: str, brands: Iterable[str]
    ) -> None:
        async with self._sessions.begin() as session:
            await session.merge(AppUser(id=user_id, role=role.value, display_name=display_name))
            await session.execute(delete(Entitlement).where(Entitlement.user_id == user_id))
            session.add_all(Entitlement(user_id=user_id, brand=brand) for brand in brands)
