"""Shared request models used by HTTP and MCP adapters."""

from pydantic import BaseModel, EmailStr, Field


class SendMailInput(BaseModel):
    account_id: int
    to_addresses: list[EmailStr]
    subject: str = Field(max_length=998)
    reply_to_email_id: int | None = Field(default=None, ge=1)
    body_text: str | None = None
    body_html: str | None = None
    cc_addresses: list[EmailStr] = Field(default_factory=list)
    bcc_addresses: list[EmailStr] = Field(default_factory=list)
    reply_to: EmailStr | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)
    # Slots filled by PUT /api/v1/uploads/{id}. The bytes live on the data
    # volume, so only the ids travel in the request that sends them.
    upload_ids: list[str] = Field(default_factory=list)
    # The same kind of slot, embedded in the body instead of appended to it: the
    # part is given the Content-ID begin_attachment_upload handed out, so a
    # ``cid:`` URL in body_html resolves to it. One id may appear in one list or
    # the other, never both -- a slot holds one payload and is spent once.
    inline_upload_ids: list[str] = Field(default_factory=list)
