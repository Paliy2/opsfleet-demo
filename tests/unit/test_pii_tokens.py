from uuid import uuid4

import pytest
from pydantic import SecretStr

from opsfleet_agent.core.errors import MaskedValueError, PiiServiceUnavailableError
from opsfleet_agent.entities.pii_tokens import TokenBook, is_token_term, token_label
from opsfleet_agent.entities.privacy import RedactedSpan, Redaction, is_masked, substitute
from opsfleet_agent.interactors.pii_redaction import RegexRedactor
from opsfleet_agent.interactors.pii_tokens import (
    PrivacyTokens,
    fingerprint,
    numbering_key,
    tokenize,
)
from opsfleet_agent.interactors.sql_guard import ensure_safe_sql_text
from tests.fakes import MemoryPiiTokenVault

KEY = b"spike-test-key-not-a-secret"
COMPARE = "Compare orders of John Doe (john.doe@example.com) and Jane Roe (jane.roe@example.org)"


def spans(text: str, *pairs: tuple[str, str]) -> tuple[RedactedSpan, ...]:
    found = []
    for value, info_type in pairs:
        start = text.index(value)
        found.append(
            RedactedSpan(
                start=start, end=start + len(value), info_type=info_type, value=SecretStr(value)
            )
        )
    return tuple(found)


def redaction(text: str, *pairs: tuple[str, str]) -> Redaction:
    found = spans(text, *pairs)
    return Redaction(text=substitute(text, found), spans=found)


def compare() -> Redaction:
    return redaction(
        COMPARE,
        ("John Doe", "PERSON_NAME"),
        ("john.doe@example.com", "EMAIL_ADDRESS"),
        ("Jane Roe", "PERSON_NAME"),
        ("jane.roe@example.org", "EMAIL_ADDRESS"),
    )


def test_distinct_people_get_distinct_numbers() -> None:
    result = tokenize(COMPARE, compare().spans, TokenBook(), KEY)

    assert result.text == (
        "Compare orders of [PERSON_NAME_1] ([EMAIL_ADDRESS_1]) "
        "and [PERSON_NAME_2] ([EMAIL_ADDRESS_2])"
    )
    assert result.labels == ("PERSON_NAME_1", "EMAIL_ADDRESS_1", "PERSON_NAME_2", "EMAIL_ADDRESS_2")


def test_repeated_value_in_one_message_reuses_the_token() -> None:
    text = "John Doe vs john  DOE"
    found = (
        RedactedSpan(start=0, end=8, info_type="PERSON_NAME", value=SecretStr("John Doe")),
        RedactedSpan(start=12, end=21, info_type="PERSON_NAME", value=SecretStr("john  DOE")),
    )

    result = tokenize(text, found, TokenBook(), KEY)

    assert result.text == "[PERSON_NAME_1] vs [PERSON_NAME_1]"
    assert len(result.new_entries) == 1


async def test_numbers_stay_stable_across_turns_in_a_conversation() -> None:
    vault = MemoryPiiTokenVault()
    tokens = PrivacyTokens(vault, KEY)
    session = uuid4()
    follow_up = "And JANE ROE alone, then Max Mustermann"

    await tokens.number(session, COMPARE, compare())
    second = await tokens.number(
        session,
        follow_up,
        redaction(follow_up, ("JANE ROE", "PERSON_NAME"), ("Max Mustermann", "PERSON_NAME")),
    )

    assert second.text == "And [PERSON_NAME_2] alone, then [PERSON_NAME_3]"
    assert [entry.label for entry in vault.books[session].entries][-1] == "PERSON_NAME_3"


async def test_conversations_number_independently_with_unlinkable_fingerprints() -> None:
    vault = MemoryPiiTokenVault()
    tokens = PrivacyTokens(vault, KEY)
    first, second = uuid4(), uuid4()
    text = "Jane Roe only"

    await tokens.number(first, COMPARE, compare())
    other = await tokens.number(second, text, redaction(text, ("Jane Roe", "PERSON_NAME")))

    assert other.text == "[PERSON_NAME_1] only"
    jane_in_first = vault.books[first].entries[2]
    assert jane_in_first.label == "PERSON_NAME_2"
    assert jane_in_first.fingerprint != vault.books[second].entries[0].fingerprint


async def test_the_vault_stores_keyed_hashes_never_values() -> None:
    vault = MemoryPiiTokenVault()
    session = uuid4()

    tokenized = await PrivacyTokens(vault, KEY).number(session, COMPARE, compare())

    stored = vault.books[session].model_dump_json()
    for raw in ("john", "doe", "jane", "roe", "example"):
        assert raw not in stored.lower()
    assert "john" not in repr(tokenized).lower()
    assert "john" not in tokenized.model_dump_json().lower()


async def test_a_numbering_race_retries_then_fails_closed() -> None:
    racing = MemoryPiiTokenVault(conflicts=1)
    session = uuid4()

    retried = await PrivacyTokens(racing, KEY).number(session, COMPARE, compare())

    assert retried.labels[0] == "PERSON_NAME_1"
    with pytest.raises(PiiServiceUnavailableError):
        await PrivacyTokens(MemoryPiiTokenVault(conflicts=9), KEY).number(
            uuid4(), COMPARE, compare()
        )


async def test_redaction_whose_text_disagrees_with_its_spans_fails_fast() -> None:
    lying = Redaction(text="Compare orders of [PERSON_NAME]", spans=())

    with pytest.raises(ValueError, match="does not match"):
        await PrivacyTokens(MemoryPiiTokenVault(), KEY).number(uuid4(), COMPARE, lying)


async def test_numbering_works_on_real_regex_redaction() -> None:
    text = "mail jane.roe@example.org or jane.roe@example.org, call +1 415 555 0134"

    tokenized = await PrivacyTokens(MemoryPiiTokenVault(), KEY).number(
        uuid4(), text, await RegexRedactor().redact(text)
    )

    assert tokenized.text == "mail [EMAIL_ADDRESS_1] or [EMAIL_ADDRESS_1], call [PHONE_NUMBER_1]"


def test_fingerprint_depends_on_type_key_and_normalisation() -> None:
    value = "4155550134"

    assert fingerprint(KEY, "PHONE_NUMBER", "415-555-0134") == fingerprint(
        KEY, "PHONE_NUMBER", value
    )
    assert fingerprint(KEY, "PHONE_NUMBER", value) != fingerprint(KEY, "IBAN_CODE", value)
    assert fingerprint(KEY, "PHONE_NUMBER", value) != fingerprint(b"other", "PHONE_NUMBER", value)
    assert numbering_key("salt-one-123456789") != numbering_key("salt-two-123456789")


@pytest.mark.parametrize(
    ("term", "label"),
    [("PERSON_NAME_1", "PERSON_NAME_1"), (" [email_address_12] ", "EMAIL_ADDRESS_12")],
)
def test_token_terms_are_recognised(term: str, label: str) -> None:
    assert is_token_term(term)
    assert token_label(term) == label


@pytest.mark.parametrize(
    "term", ["levis_2", "Q3_REPORT_1", "about [PERSON_NAME_1]", "[PERSON_NAME]", "PERSON_NAME_0"]
)
def test_other_terms_are_not_tokens(term: str) -> None:
    assert not is_token_term(term)


def test_numbered_placeholders_still_fail_fast_in_sql_and_count_as_masked() -> None:
    assert is_masked("orders of [PERSON_NAME_3]")
    with pytest.raises(MaskedValueError):
        ensure_safe_sql_text("SELECT * FROM users WHERE first_name = '[PERSON_NAME_1]'")
