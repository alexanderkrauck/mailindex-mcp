"""Refetching an attachment finds it by its bytes when the provider's copy is laid out differently."""

import hashlib
from email.message import EmailMessage
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.attachment import EmailAttachment
from src.models.base import Base
from src.models.email import EmailLog
from src.models.placement import MessagePlacement
from src.models.smtp_config import SMTPConfig
from src.models.user import User
from src.services import attachment_service

REPORT = bytes(range(256)) * 40
LOGO = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def sent_copy() -> bytes:
    """Sent Items keeps the sender's own MIME: a text alternative, then the HTML, the image, the file."""
    message = EmailMessage()
    message["Subject"] = "rich"
    message["From"] = "owner@example.com"
    message["To"] = "owner@example.com"
    message.set_content("text alternative")
    message.add_alternative('<p>hi <img src="cid:logo@x.invalid"></p>', subtype="html")
    message.get_payload()[1].add_related(LOGO, "image", "png", cid="<logo@x.invalid>", filename="logo.png", disposition="inline")
    message.add_attachment(REPORT, maintype="application", subtype="octet-stream", filename="report.bin")
    return message.as_bytes()


def delivered_copy() -> bytes:
    """What Exchange hands out for the INBOX copy: HTML only, so every later part has moved up one place."""
    message = EmailMessage()
    message["Subject"] = "rich"
    message["From"] = "owner@example.com"
    message["To"] = "owner@example.com"
    message.set_content('<p>hi <img src="cid:logo@x.invalid"></p>', subtype="html")
    message.add_related(LOGO, "image", "png", cid="<logo@x.invalid>", filename="logo.png", disposition="inline")
    message.add_attachment(
        REPORT, maintype="application", subtype="octet-stream", filename="report.bin", cid="<8D55B632@AUTP296.PROD.OUTLOOK.COM>"
    )
    return message.as_bytes()


def leaf_index(raw: bytes, filename: str) -> int:
    from email import message_from_bytes, policy

    leaves = [part for part in message_from_bytes(raw, policy=policy.default).walk() if part.get_content_maintype() != "multipart"]
    return next(index for index, part in enumerate(leaves) if part.get_filename() == filename)


@pytest.fixture(params=["indexed from Sent Items, refetched from INBOX", "indexed from INBOX, refetched from Sent Items"])
def indexed(request, monkeypatch):
    """One message filed twice, the two copies laid out differently.

    Either copy may be the one the attachment rows were recorded from, and the
    other the one a later refetch lands on -- which depends on which was synced
    first and which placement was seen last, so both directions are real.
    """
    from_sent = request.param.startswith("indexed from Sent")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    owner = User(google_sub="owner", email="owner@example.com")
    db.add(owner)
    db.flush()
    account = SMTPConfig(
        owner_user_id=owner.id, provider="microsoft", auth_type="oauth2", name="m", host="outlook.office365.com", username="owner@example.com"
    )
    db.add(account)
    db.flush()
    message = EmailLog(
        smtp_config_id=account.id,
        sender="owner@example.com",
        recipient="owner@example.com",
        subject="rich",
        message_id="<m@x>",
        provider_message_id="Sent Items:1",
    )
    db.add(message)
    db.flush()
    db.add(MessagePlacement(email_log_id=message.id, folder="INBOX", uid=5, uid_validity=1))
    recorded_from, refetched = (sent_copy(), delivered_copy()) if from_sent else (delivered_copy(), sent_copy())
    rows = {}
    for name, payload in (("report.bin", REPORT), ("logo.png", LOGO)):
        row = EmailAttachment(
            email_log_id=message.id,
            filename=name,
            part_index=leaf_index(recorded_from, name),
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            content_id="logo@x.invalid" if name == "logo.png" else None,
        )
        db.add(row)
        rows[name] = row
    db.commit()

    class FakeClient:
        def __init__(self, config):
            pass

        async def fetch_raw_email(self, folder, uid, uid_validity):
            return refetched

        async def disconnect(self):
            pass

    monkeypatch.setattr(attachment_service, "SMTPClient", FakeClient)
    yield SimpleNamespace(db=db, owner=owner, rows=rows)
    db.close()


@pytest.mark.asyncio
async def test_the_two_copies_really_do_disagree_about_where_the_file_is():
    # The premise of the cases below, so a change to the fixtures cannot quietly make them vacuous.
    assert leaf_index(sent_copy(), "report.bin") != leaf_index(delivered_copy(), "report.bin")


@pytest.mark.asyncio
async def test_an_attachment_is_found_by_its_bytes_when_the_recorded_position_is_another_part(indexed):
    row, payload = await attachment_service.refetch_attachment_bytes(indexed.db, indexed.owner.id, indexed.rows["logo.png"].id)

    assert payload == LOGO
    assert row.filename == "logo.png"


@pytest.mark.asyncio
async def test_the_file_whose_slot_the_other_copy_gives_to_the_image_is_still_the_file(indexed):
    row, payload = await attachment_service.refetch_attachment_bytes(indexed.db, indexed.owner.id, indexed.rows["report.bin"].id)

    assert payload == REPORT
    assert row.filename == "report.bin"


@pytest.mark.asyncio
async def test_a_file_the_provider_really_changed_is_still_refused(indexed):
    indexed.rows["report.bin"].sha256 = hashlib.sha256(b"what it used to be").hexdigest()
    indexed.db.commit()

    with pytest.raises(HTTPException) as refused:
        await attachment_service.refetch_attachment_bytes(indexed.db, indexed.owner.id, indexed.rows["report.bin"].id)

    assert refused.value.status_code == 409
    assert "checksum" in refused.value.detail
