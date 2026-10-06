"""Owner-checked ephemeral attachment retrieval."""

import hashlib
from email import message_from_bytes, policy

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.email.attachment_handler import AttachmentHandler
from src.email.gmail_api_client import GmailApiClient
from src.email.smtp_client import SMTPClient
from src.models.attachment import EmailAttachment
from src.models.email import EmailLog
from src.models.placement import MessagePlacement
from src.models.smtp_config import SMTPConfig


def owned_attachment(db: Session, user_id: int, attachment_id: int) -> EmailAttachment:
    attachment = (
        db.query(EmailAttachment)
        .join(EmailLog)
        .join(SMTPConfig)
        .filter(EmailAttachment.id == attachment_id, SMTPConfig.owner_user_id == user_id)
        .first()
    )
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return attachment



def current_placement(db, email_log_id: int):
    """Return a folder the message is filed in, preferring one that is not Trash."""
    from src.config import settings
    from src.services.mail_service import is_excluded_folder

    placements = (
        db.query(MessagePlacement)
        .filter(MessagePlacement.email_log_id == email_log_id)
        .order_by(MessagePlacement.seen_at.desc(), MessagePlacement.id.desc())
        .all()
    )
    if not placements:
        return None
    for placement in placements:
        if not is_excluded_folder(placement.folder, settings.excluded_folder_suffixes):
            return placement
    return placements[0]


async def refetch_attachment_bytes(
    db: Session, user_id: int, attachment_id: int
) -> tuple[EmailAttachment, bytes]:
    attachment = owned_attachment(db, user_id, attachment_id)
    message = attachment.email_log
    account = SMTPConfig.create_detached(message.mail_account)
    if account.provider == "gmail" and account.auth_type == "oauth2":
        gmail_client = GmailApiClient(account)
        try:
            raw_email = await gmail_client.get_raw_message(message.provider_message_id)
        finally:
            await gmail_client.close()
        if raw_email is None:
            raise HTTPException(status_code=404, detail="Provider message no longer exists")
    else:
        client = SMTPClient(account)
        try:
            # The message may have moved since it was indexed, so prefer a
            # recorded placement over the denormalised columns.
            placement = current_placement(db, message.id)
            folder = placement.folder if placement else message.folder
            uid = placement.uid if placement else message.imap_uid
            uid_validity = placement.uid_validity if placement else message.uid_validity
            if folder and uid is not None:
                raw_email = await client.fetch_raw_email(folder, uid, uid_validity)
            else:
                raw_email = await client.fetch_raw_by_message_id(message.message_id)
        finally:
            await client.disconnect()

    parsed = message_from_bytes(raw_email, policy=policy.default)
    candidates = []
    handler = AttachmentHandler()
    part_index = 0
    for part in parsed.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if handler._is_attachment(part):
            candidates.append((part_index, part))
        part_index += 1

    selected = next((part for index, part in candidates if index == attachment.part_index), None)
    if selected is None and attachment.content_id:
        selected = next(
            (
                part
                for _, part in candidates
                if str(part.get("Content-ID", "")).strip("<>") == attachment.content_id
            ),
            None,
        )
    if selected is None:
        selected = next(
            (part for _, part in candidates if part.get_filename() == attachment.filename),
            None,
        )
    if selected is None:
        raise HTTPException(status_code=404, detail="Attachment no longer exists in the provider message")
    payload = selected.get_payload(decode=True)
    if payload is None:
        raise HTTPException(status_code=422, detail="Provider returned an undecodable attachment")
    digest = hashlib.sha256(payload).hexdigest()
    if attachment.sha256 and attachment.sha256 != digest:
        # The recorded position names the wrong part. One message can be filed in
        # several folders, and a provider need not store every copy alike:
        # Exchange keeps the sender's original MIME in Sent Items but rebuilds the
        # copy it delivers, dropping the text alternative and renumbering every
        # part after it. The bytes are what identify an attachment, so look for
        # them before concluding the provider changed the file.
        for _, part in candidates:
            other = part.get_payload(decode=True)
            if other is not None and hashlib.sha256(other).hexdigest() == attachment.sha256:
                selected, payload, digest = part, other, attachment.sha256
                break
        else:
            raise HTTPException(
                status_code=409, detail="Provider attachment no longer matches its recorded checksum"
            )
    if not attachment.sha256:
        attachment.sha256 = digest
        attachment.detected_content_type = AttachmentHandler._detect_content_type(payload, attachment.filename)
        attachment.size = len(payload)
        db.commit()
    return attachment, payload
