import smtplib
import ssl
from email.message import EmailMessage
from typing import override

import anyio
from pydantic import BaseModel, ConfigDict, SecretStr

from opsfleet_agent.core.errors import MailDeliveryError
from opsfleet_agent.entities.mail import MailReceipt, OutgoingMail
from opsfleet_agent.gateways.mail.message import build_message
from opsfleet_agent.ports.mail import ReportMailer


class SmtpServer(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str
    port: int
    starttls: bool
    user: str | None
    password: SecretStr | None
    timeout_seconds: float


class SmtpMailer(ReportMailer):
    def __init__(self, server: SmtpServer, sender: str) -> None:
        self._server = server
        self._sender = sender

    @override
    async def send(self, mail: OutgoingMail) -> MailReceipt:
        message = build_message(mail, self._sender)
        try:
            await anyio.to_thread.run_sync(self._deliver, message)
        except (smtplib.SMTPException, OSError) as error:
            raise MailDeliveryError(f"smtp delivery failed: {type(error).__name__}") from error
        return MailReceipt(
            message_id=str(message["Message-ID"]),
            location=f"smtp://{self._server.host}:{self._server.port}",
        )

    def _deliver(self, message: EmailMessage) -> None:
        server = self._server
        with smtplib.SMTP(server.host, server.port, timeout=server.timeout_seconds) as client:
            if server.starttls:
                client.starttls(context=ssl.create_default_context())
            if server.user and server.password:
                client.login(server.user, server.password.get_secret_value())
            client.send_message(message)
