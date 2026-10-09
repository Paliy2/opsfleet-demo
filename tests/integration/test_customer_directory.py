import hashlib
from dataclasses import dataclass

import pytest
from google.cloud import bigquery
from pydantic import SecretStr

from opsfleet_agent.core.config import get_settings
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.entities.pii_tokens import BlindOperator, BlindTerm
from opsfleet_agent.gateways.bigquery.customers import BigQueryCustomerDirectory
from opsfleet_agent.interactors.pii_tokens import normalized_value

pytestmark = pytest.mark.bq

CEO = AccessScope(user_id="ceo", role=Role.CEO)
NOBODY = "no.such.person.4f1c@example.invalid"


def exact(info_type: str, value: str) -> BlindTerm:
    return BlindTerm(
        operator=BlindOperator.EXACT,
        info_type=info_type,
        label=f"{info_type}_1",
        values=(SecretStr(normalized_value(info_type, value)),),
    )


@pytest.fixture(scope="module")
def client() -> bigquery.Client:
    return bigquery.Client(project=get_settings().gcp_project)


@pytest.fixture(scope="module")
def directory(client: bigquery.Client) -> BigQueryCustomerDirectory:
    return BigQueryCustomerDirectory(client, get_settings())


@dataclass(frozen=True)
class SyntheticCustomer:
    id: int
    email: str
    first_name: str
    last_name: str


@pytest.fixture(scope="module")
def synthetic_customer(client: bigquery.Client) -> SyntheticCustomer:
    rows = client.query(
        "SELECT id, email, first_name, last_name "
        "FROM `bigquery-public-data.thelook_ecommerce.users` WHERE id = 1"
    ).result()
    row = next(iter(rows))
    return SyntheticCustomer(
        int(row["id"]), str(row["email"]), str(row["first_name"]), str(row["last_name"])
    )


def expected_pseudonym(user_id: int) -> str:
    salt = get_settings().pii_hash_salt.get_secret_value()
    return hashlib.sha256(f"{salt}{user_id}".encode()).hexdigest()[:16]


async def test_an_unknown_email_matches_nobody(directory: BigQueryCustomerDirectory) -> None:
    assert await directory.find(exact("EMAIL_ADDRESS", NOBODY), CEO, 25) == ()


async def test_an_email_resolves_to_the_pseudonym_the_masking_produces(
    directory: BigQueryCustomerDirectory, synthetic_customer: SyntheticCustomer
) -> None:
    typed = synthetic_customer.email.upper()

    found = await directory.find(exact("EMAIL_ADDRESS", typed), CEO, 25)

    pseudonym = expected_pseudonym(synthetic_customer.id)
    assert [match.id for match in found] == [pseudonym]
    assert found[0].email.startswith(f"user_{pseudonym[:8]}@")
    assert found[0].email != synthetic_customer.email


async def test_a_full_name_resolves_to_every_customer_with_that_name(
    directory: BigQueryCustomerDirectory, synthetic_customer: SyntheticCustomer
) -> None:
    typed = f"  {synthetic_customer.first_name}   {synthetic_customer.last_name} "

    found = await directory.find(exact("PERSON_NAME", typed), CEO, 25)

    assert expected_pseudonym(synthetic_customer.id) in {match.id for match in found}
