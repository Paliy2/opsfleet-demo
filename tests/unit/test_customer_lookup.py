from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from opsfleet_agent.core.errors import (
    AgentError,
    MaskedSearchError,
    PiiLookupDeniedError,
    PiiValueExpiredError,
)
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.entities.pii_tokens import BlindOperator, LookupPurpose, TurnValue
from opsfleet_agent.gateways.pii_vault.vaults import SealedPiiValueVault
from opsfleet_agent.interactors.customer_lookup import LookupPolicy
from tests.fakes import (
    NOW,
    FakeCustomerDirectory,
    MemoryLookupLog,
    customer,
    customer_lookup,
    value_vault,
)

MANAGER = AccessScope(user_id="mgr_a", role=Role.MANAGER, brands=frozenset({"Diesel"}))
CEO = AccessScope(user_id="ceo", role=Role.CEO)
MORGAN = customer("1a2b3c4d5e6f7a8b")


def typed(label: str, info_type: str, value: str) -> TurnValue:
    return TurnValue(label=label, info_type=info_type, value=SecretStr(value))


async def held(*values: TurnValue, owner: str = "mgr_a") -> tuple[SealedPiiValueVault, UUID]:
    vault, session = value_vault(), uuid4()
    await vault.put(owner, session, values)
    return vault, session


async def test_a_typed_email_resolves_to_the_masked_customer_and_is_audited() -> None:
    vault, session = await held(typed("EMAIL_ADDRESS_1", "EMAIL_ADDRESS", "jane.roe@example.org"))
    directory = FakeCustomerDirectory({"jane.roe@example.org": (MORGAN,)})
    log = MemoryLookupLog()

    resolution = await customer_lookup(vault, directory, log)(
        MANAGER, session, "EMAIL_ADDRESS_1", LookupPurpose.DELETE
    )

    assert resolution.customers == (MORGAN,)
    assert directory.terms[0].operator is BlindOperator.EXACT
    assert log.rows == [("mgr_a", session, "EMAIL_ADDRESS_1", LookupPurpose.DELETE, 1, NOW)]
    assert "jane" not in repr(log.rows)


@pytest.mark.parametrize(
    "label",
    ["EMAIL_ADDRESS_7", "PERSON_NAME_1"],
)
async def test_forged_or_unknown_tokens_ask_the_user_to_retype(label: str) -> None:
    vault, session = await held(typed("EMAIL_ADDRESS_1", "EMAIL_ADDRESS", "jane.roe@example.org"))
    directory = FakeCustomerDirectory()

    with pytest.raises(PiiValueExpiredError) as raised:
        await customer_lookup(vault, directory)(MANAGER, session, label, LookupPurpose.SEARCH)

    assert directory.terms == []
    assert_no_leak(raised.value)


async def test_red_team_a_token_from_another_conversation_or_user_does_not_resolve() -> None:
    vault, session = await held(typed("PERSON_NAME_1", "PERSON_NAME", "jane roe"), owner="mgr_b")
    lookup = customer_lookup(vault, FakeCustomerDirectory({"jane roe": (MORGAN,)}))

    with pytest.raises(PiiValueExpiredError):
        await lookup(MANAGER, session, "PERSON_NAME_1", LookupPurpose.SEARCH)
    with pytest.raises(PiiValueExpiredError):
        await lookup(
            AccessScope(user_id="mgr_b", role=Role.MANAGER, brands=frozenset({"Diesel"})),
            uuid4(),
            "PERSON_NAME_1",
            LookupPurpose.SEARCH,
        )


async def test_types_reports_never_hold_are_refused() -> None:
    vault, session = await held(typed("PHONE_NUMBER_1", "PHONE_NUMBER", "4155550134"))

    with pytest.raises(MaskedSearchError):
        await customer_lookup(vault)(MANAGER, session, "PHONE_NUMBER_1", LookupPurpose.SEARCH)


async def test_roles_outside_the_policy_are_denied_before_any_lookup() -> None:
    vault, session = await held(typed("PERSON_NAME_1", "PERSON_NAME", "jane roe"))
    directory = FakeCustomerDirectory()
    ceo_only = LookupPolicy(allowed_roles=frozenset({Role.CEO}))

    with pytest.raises(PiiLookupDeniedError) as raised:
        await customer_lookup(vault, directory, policy=ceo_only)(
            MANAGER, session, "PERSON_NAME_1", LookupPurpose.SEARCH
        )

    assert directory.terms == []
    assert_no_leak(raised.value)


async def test_the_conversation_cap_is_enforced() -> None:
    vault, session = await held(typed("PERSON_NAME_1", "PERSON_NAME", "jane roe"))
    lookup = customer_lookup(vault, policy=LookupPolicy(max_per_conversation=2))

    for _ in range(2):
        await lookup(MANAGER, session, "PERSON_NAME_1", LookupPurpose.SEARCH)

    with pytest.raises(PiiLookupDeniedError, match="conversation"):
        await lookup(MANAGER, session, "PERSON_NAME_1", LookupPurpose.SEARCH)


async def test_the_daily_cap_spans_conversations_and_rolls_over() -> None:
    log = MemoryLookupLog()
    for _ in range(3):
        await log.record("mgr_a", uuid4(), "PERSON_NAME_1", LookupPurpose.SEARCH, 0)
    vault, session = await held(typed("PERSON_NAME_1", "PERSON_NAME", "jane roe"))
    capped = LookupPolicy(max_per_day=3)

    with pytest.raises(PiiLookupDeniedError, match="daily"):
        await customer_lookup(vault, log=log, policy=capped)(
            MANAGER, session, "PERSON_NAME_1", LookupPurpose.SEARCH
        )

    log.rows = [(*row[:5], NOW - timedelta(days=2)) for row in log.rows]
    await customer_lookup(vault, log=log, policy=capped)(
        MANAGER, session, "PERSON_NAME_1", LookupPurpose.SEARCH
    )


async def test_ceo_lookups_are_allowed_by_default() -> None:
    vault, session = await held(typed("PERSON_NAME_1", "PERSON_NAME", "jane roe"), owner="ceo")

    resolution = await customer_lookup(vault, FakeCustomerDirectory({"jane roe": (MORGAN,)}))(
        CEO, session, "PERSON_NAME_1", LookupPurpose.SEARCH
    )

    assert resolution.customers == (MORGAN,)


def assert_no_leak(error: AgentError) -> None:
    for fragment in ("jane", "roe", "example.org"):
        assert fragment not in error.detail.lower()
        assert fragment not in error.user_message.lower()
