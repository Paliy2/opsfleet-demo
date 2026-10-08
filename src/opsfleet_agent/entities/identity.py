from pydantic import BaseModel, ConfigDict

from opsfleet_agent.entities.access import Role


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    roles: frozenset[Role]
