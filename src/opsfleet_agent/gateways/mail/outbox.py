from pathlib import Path
from typing import override
from uuid import uuid4

import anyio

from opsfleet_agent.entities.mail import MailReceipt, OutgoingMail
from opsfleet_agent.gateways.mail.message import build_message
from opsfleet_agent.ports.mail import ReportMailer


class OutboxMailer(ReportMailer):
    def __init__(self, directory: Path, sender: str) -> None:
        self._directory = directory
        self._sender = sender

    @override
    async def send(self, mail: OutgoingMail) -> MailReceipt:
        message = build_message(mail, self._sender)
        path = anyio.Path(self._directory) / f"{uuid4().hex}.eml"
        await path.parent.mkdir(parents=True, exist_ok=True)
        await path.write_bytes(message.as_bytes())
        return MailReceipt(message_id=str(message["Message-ID"]), location=str(path))
