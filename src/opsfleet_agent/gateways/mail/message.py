from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from opsfleet_agent.entities.mail import OutgoingMail


def build_message(mail: OutgoingMail, sender: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = mail.recipient
    message["Subject"] = mail.subject
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
    message.set_content(mail.body)
    return message
