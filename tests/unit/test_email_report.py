import smtplib
from dataclasses import dataclass, field
from email import message_from_bytes
from pathlib import Path
from uuid import uuid4

import pytest

from opsfleet_agent.core.errors import (
    MailDeliveryError,
    NotFoundError,
    PiiDetectedError,
    RecipientDeniedError,
)
from opsfleet_agent.entities.mail import MailReceipt, OutgoingMail
from opsfleet_agent.gateways.mail.outbox import OutboxMailer
from opsfleet_agent.gateways.mail.smtp import SmtpMailer, SmtpServer
from opsfleet_agent.interactors.email_report import EmailReport
from opsfleet_agent.interactors.reports import GetReport
from tests.fakes import FakeReports, StoredReport


@dataclass
class FakeMailer:
    sent: list[OutgoingMail] = field(default_factory=list)

    async def send(self, mail: OutgoingMail) -> MailReceipt:
        self.sent.append(mail)
        return MailReceipt(message_id=f"<{len(self.sent)}@test>", location="memory")


def address_of(user_id: str) -> str:
    return f"{user_id}@opsfleet.demo"


def email_report(*rows: StoredReport) -> tuple[EmailReport, FakeMailer]:
    mailer = FakeMailer()
    return EmailReport(GetReport(FakeReports(rows=list(rows))), mailer, address_of), mailer


async def test_owner_receives_own_report_at_registered_address() -> None:
    mine = StoredReport("mgr_a", uuid4(), "Q3 review", "Revenue grew 12%.")
    send, mailer = email_report(mine)

    sent = await send("mgr_a", mine.id)

    assert sent.title == "Q3 review"
    [mail] = mailer.sent
    assert mail.recipient == "mgr_a@opsfleet.demo"
    assert mail.subject == "OpsFleet report: Q3 review"
    assert mail.body == "Revenue grew 12%."


async def test_explicit_own_address_is_accepted_case_insensitively() -> None:
    mine = StoredReport("mgr_a", uuid4(), "Q3 review", "body")
    send, mailer = email_report(mine)

    await send("mgr_a", mine.id, " MGR_A@opsfleet.demo ")

    assert [mail.recipient for mail in mailer.sent] == ["mgr_a@opsfleet.demo"]


async def test_another_users_report_is_not_found_and_not_sent() -> None:
    theirs = StoredReport("mgr_b", uuid4(), "Secret plan", "confidential")
    send, mailer = email_report(theirs)

    with pytest.raises(NotFoundError):
        await send("mgr_a", theirs.id)

    assert mailer.sent == []


@pytest.mark.parametrize(
    "recipient", ["rival@competitor.com", "mgr_b@opsfleet.demo", "mgr_a@opsfleet.demo.evil.io"]
)
async def test_external_recipients_are_denied(recipient: str) -> None:
    mine = StoredReport("mgr_a", uuid4(), "Q3 review", "body")
    send, mailer = email_report(mine)

    with pytest.raises(RecipientDeniedError):
        await send("mgr_a", mine.id, recipient)

    assert mailer.sent == []


async def test_report_containing_pii_is_blocked() -> None:
    leaky = StoredReport("mgr_a", uuid4(), "Top buyers", "Call jane.doe@gmail.com today.")
    send, mailer = email_report(leaky)

    with pytest.raises(PiiDetectedError):
        await send("mgr_a", leaky.id)

    assert mailer.sent == []


async def test_outbox_writes_one_eml_file(tmp_path: Path) -> None:
    mailer = OutboxMailer(tmp_path / "outbox", "analyst@opsfleet.demo")
    mail = OutgoingMail(recipient="mgr_a@opsfleet.demo", subject="OpsFleet report: Q3", body="Hi")

    receipt = await mailer.send(mail)

    [written] = list((tmp_path / "outbox").glob("*.eml"))
    assert receipt.location == str(written)
    parsed = message_from_bytes(written.read_bytes())
    assert parsed["To"] == "mgr_a@opsfleet.demo"
    assert parsed["From"] == "analyst@opsfleet.demo"
    assert parsed["Subject"] == "OpsFleet report: Q3"
    assert parsed["Message-ID"] == receipt.message_id


class RefusingSmtp:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise smtplib.SMTPConnectError(421, b"busy")


async def test_smtp_failure_becomes_typed_delivery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", RefusingSmtp)
    server = SmtpServer(
        host="mail.test", port=25, starttls=False, user=None, password=None, timeout_seconds=1
    )
    mail = OutgoingMail(recipient="mgr_a@opsfleet.demo", subject="s", body="b")

    with pytest.raises(MailDeliveryError):
        await SmtpMailer(server, "analyst@opsfleet.demo").send(mail)
