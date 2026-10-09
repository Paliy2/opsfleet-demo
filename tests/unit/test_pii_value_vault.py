from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from opsfleet_agent.core.errors import PiiValueExpiredError
from opsfleet_agent.entities.pii_tokens import TurnValue
from opsfleet_agent.gateways.pii_vault.sealer import ValueSealer, parse_key
from opsfleet_agent.gateways.pii_vault.vaults import (
    HashStore,
    InProcessHashStore,
    SealedPiiValueVault,
)
from tests.fakes import NOW, TEST_PII_KEY

EMAIL = TurnValue(
    label="EMAIL_ADDRESS_1", info_type="EMAIL_ADDRESS", value=SecretStr("jane.roe@example.org")
)
NAME = TurnValue(label="PERSON_NAME_1", info_type="PERSON_NAME", value=SecretStr("jane roe"))


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class RecordingStore:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, bytes]] = {}

    async def put(self, key: str, field: str, value: bytes, ttl_seconds: int) -> None:
        self.hashes.setdefault(key, {})[field] = value

    async def get(self, key: str, field: str) -> bytes | None:
        return self.hashes.get(key, {}).get(field)

    async def remove(self, key: str, fields: Sequence[str]) -> None:
        for field in fields:
            self.hashes.get(key, {}).pop(field, None)

    async def drop(self, key: str) -> None:
        self.hashes.pop(key, None)


def vault(store: HashStore, key: bytes = TEST_PII_KEY) -> SealedPiiValueVault:
    return SealedPiiValueVault(
        store, ValueSealer(key), owner_salt="test-salt-1234567", ttl=timedelta(minutes=15)
    )


async def test_a_value_written_by_one_replica_is_read_by_another() -> None:
    shared = RecordingStore()
    session = uuid4()

    await vault(shared).put("mgr_a", session, (EMAIL, NAME))
    read = await vault(shared).get("mgr_a", session, "EMAIL_ADDRESS_1")

    assert read.value.get_secret_value() == "jane.roe@example.org"
    assert read.info_type == "EMAIL_ADDRESS"


async def test_expiry_fails_closed_with_the_retype_error() -> None:
    clock = Clock()
    store = InProcessHashStore(clock)
    session = uuid4()
    await vault(store).put("mgr_a", session, (EMAIL,))

    clock.now = NOW + timedelta(minutes=16)

    with pytest.raises(PiiValueExpiredError):
        await vault(store).get("mgr_a", session, "EMAIL_ADDRESS_1")


async def test_eviction_fails_closed_with_the_retype_error() -> None:
    store = InProcessHashStore(Clock())
    session = uuid4()
    await vault(store).put("mgr_a", session, (EMAIL,))

    store.evict_all()

    with pytest.raises(PiiValueExpiredError):
        await vault(store).get("mgr_a", session, "EMAIL_ADDRESS_1")


async def test_stored_bytes_never_contain_the_plaintext() -> None:
    store = RecordingStore()
    await vault(store).put("mgr_a", uuid4(), (EMAIL, NAME))

    stored = b"".join(blob for fields in store.hashes.values() for blob in fields.values())
    assert stored
    assert all("mgr_a" not in key for key in store.hashes)

    assert b"jane" not in stored
    assert b"example.org" not in stored
    assert b"roe" not in stored


async def test_values_are_bound_to_owner_and_conversation() -> None:
    store = InProcessHashStore(Clock())
    session = uuid4()
    await vault(store).put("mgr_a", session, (EMAIL,))

    with pytest.raises(PiiValueExpiredError):
        await vault(store).get("mgr_b", session, "EMAIL_ADDRESS_1")
    with pytest.raises(PiiValueExpiredError):
        await vault(store).get("mgr_a", uuid4(), "EMAIL_ADDRESS_1")


async def test_a_wrong_key_or_moved_ciphertext_cannot_be_opened() -> None:
    store = RecordingStore()
    session = uuid4()
    await vault(store).put("mgr_a", session, (EMAIL, NAME))

    with pytest.raises(PiiValueExpiredError):
        await vault(store, bytes(32)).get("mgr_a", session, "EMAIL_ADDRESS_1")

    fields = next(iter(store.hashes.values()))
    fields["PERSON_NAME_1"] = fields["EMAIL_ADDRESS_1"]
    with pytest.raises(PiiValueExpiredError):
        await vault(store).get("mgr_a", session, "PERSON_NAME_1")


async def test_forgetting_drops_labels_or_the_whole_conversation() -> None:
    store = InProcessHashStore(Clock())
    session = uuid4()
    values = vault(store)
    await values.put("mgr_a", session, (EMAIL, NAME))

    await values.forget("mgr_a", session, ["EMAIL_ADDRESS_1"])

    with pytest.raises(PiiValueExpiredError):
        await values.get("mgr_a", session, "EMAIL_ADDRESS_1")
    assert (await values.get("mgr_a", session, "PERSON_NAME_1")).label == "PERSON_NAME_1"

    await values.forget_session("mgr_a", session)

    with pytest.raises(PiiValueExpiredError):
        await values.get("mgr_a", session, "PERSON_NAME_1")


@pytest.mark.parametrize("hex_key", ["00" * 16, "zz" * 32])
def test_malformed_keys_fail_fast(hex_key: str) -> None:
    with pytest.raises(ValueError, match=r"32 bytes|non-hexadecimal"):
        parse_key(hex_key)
