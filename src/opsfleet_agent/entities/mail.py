from pydantic import BaseModel, ConfigDict


class OutgoingMail(BaseModel):
    model_config = ConfigDict(frozen=True)

    recipient: str
    subject: str
    body: str


class MailReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    location: str


class EmailedReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    receipt: MailReceipt
