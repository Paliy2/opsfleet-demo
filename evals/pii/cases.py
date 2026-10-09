from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InfoType(StrEnum):
    PERSON_NAME = "PERSON_NAME"
    EMAIL_ADDRESS = "EMAIL_ADDRESS"
    PHONE_NUMBER = "PHONE_NUMBER"
    CREDIT_CARD_NUMBER = "CREDIT_CARD_NUMBER"
    IBAN_CODE = "IBAN_CODE"
    US_SOCIAL_SECURITY_NUMBER = "US_SOCIAL_SECURITY_NUMBER"
    PASSPORT = "PASSPORT"
    IP_ADDRESS = "IP_ADDRESS"
    STREET_ADDRESS = "STREET_ADDRESS"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    LOCATION_COORDINATES = "LOCATION_COORDINATES"


class Keep(StrEnum):
    BRAND = "brand"
    MISSPELLED_BRAND = "misspelled_brand"
    PSEUDONYM = "pseudonym"
    PRODUCT = "product"
    NUMBER = "number"
    ORDER_ID = "order_id"
    DATE = "date"
    LOCATION = "location"


BRAND_KINDS = frozenset({Keep.BRAND, Keep.MISSPELLED_BRAND})


class Preserved(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1)
    kind: Keep


class Sensitive(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1)
    info_type: InfoType
    fragments: tuple[str, ...] = Field(min_length=1)


class PiiCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    category: str
    text: str
    contains_pii: bool
    expected_info_types: tuple[InfoType, ...]
    sensitive: tuple[Sensitive, ...]
    preserve: tuple[Preserved, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.contains_pii != bool(self.sensitive):
            raise ValueError(f"{self.id}: contains_pii must match sensitive values")
        if set(self.expected_info_types) != {item.info_type for item in self.sensitive}:
            raise ValueError(f"{self.id}: expected_info_types must match sensitive values")
        expected = [i.value for i in self.sensitive] + [i.value for i in self.preserve]
        missing = [value for value in expected if value not in self.text]
        if missing:
            raise ValueError(f"{self.id}: not in text: {missing}")
        return self
