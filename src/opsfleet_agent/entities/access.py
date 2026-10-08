from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator


class Role(StrEnum):
    CEO = "ceo"
    MANAGER = "manager"


class AccessScope(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    role: Role
    brands: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _manager_has_brands(self) -> Self:
        if self.role is Role.MANAGER and not self.brands:
            raise ValueError("manager scope requires at least one brand")
        return self

    @property
    def sees_all_brands(self) -> bool:
        return self.role is Role.CEO
