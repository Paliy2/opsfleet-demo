from typing import Protocol

from opsfleet_agent.entities.mail import MailReceipt, OutgoingMail


class ReportMailer(Protocol):
    async def send(self, mail: OutgoingMail) -> MailReceipt: ...
