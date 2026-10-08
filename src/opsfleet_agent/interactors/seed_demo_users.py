from pydantic import BaseModel, ConfigDict

from opsfleet_agent.entities.access import Role
from opsfleet_agent.ports.access import AccessRepo
from opsfleet_agent.ports.catalog import CatalogRepo

_LOW_DATA_MAX_ITEMS = 5


class DemoUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    role: Role
    display_name: str
    brands: tuple[str, ...]


class SeedDemoUsers:
    def __init__(self, catalog: CatalogRepo, access: AccessRepo) -> None:
        self._catalog = catalog
        self._access = access

    async def __call__(self) -> tuple[DemoUser, ...]:
        ranking = await self._catalog.brands_by_revenue()
        top = [brand.brand for brand in ranking]
        low_data = next(b.brand for b in reversed(ranking) if b.items <= _LOW_DATA_MAX_ITEMS)
        users = (
            DemoUser(user_id="ceo", role=Role.CEO, display_name="Chief Executive", brands=()),
            DemoUser(
                user_id="mgr_a", role=Role.MANAGER, display_name="Manager A", brands=tuple(top[0:3])
            ),
            DemoUser(
                user_id="mgr_b", role=Role.MANAGER, display_name="Manager B", brands=tuple(top[2:5])
            ),
            DemoUser(
                user_id="mgr_empty",
                role=Role.MANAGER,
                display_name="Manager (low data)",
                brands=(low_data,),
            ),
        )
        for user in users:
            await self._access.upsert_user(user.user_id, user.role, user.display_name, user.brands)
        return users
