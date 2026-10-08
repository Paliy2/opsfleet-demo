from collections.abc import Callable
from uuid import UUID

from opsfleet_agent.core.errors import RecipientDeniedError
from opsfleet_agent.entities.mail import EmailedReport, OutgoingMail
from opsfleet_agent.interactors.pii_scan import ensure_no_pii
from opsfleet_agent.interactors.reports import GetReport
from opsfleet_agent.ports.mail import ReportMailer


class EmailReport:
    def __init__(
        self, get_report: GetReport, mailer: ReportMailer, address_of: Callable[[str], str]
    ) -> None:
        self._get_report = get_report
        self._mailer = mailer
        self._address_of = address_of

    async def __call__(
        self, owner_id: str, report_id: UUID, recipient: str | None = None
    ) -> EmailedReport:
        own_address = self._address_of(owner_id)
        if recipient is not None and recipient.strip().casefold() != own_address.casefold():
            raise RecipientDeniedError("reports may only be emailed to the owner's own address")
        report = await self._get_report(owner_id, report_id)
        ensure_no_pii(f"{report.title}\n{report.body}")
        receipt = await self._mailer.send(
            OutgoingMail(
                recipient=own_address,
                subject=f"OpsFleet report: {report.title}",
                body=report.body,
            )
        )
        return EmailedReport(title=report.title, receipt=receipt)
