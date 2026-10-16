"""Deleting a mail account: what goes, what stays, and who may do it."""

import json

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.models.attachment import EmailAttachment
from src.models.base import Base
from src.models.email import EmailLog
from src.models.participant import MailParticipant
from src.models.placement import MessagePlacement
from src.models.send_audit import SendAudit
from src.models.smtp_config import SMTPConfig
from src.models.sync_cursor import MailSyncCursor
from src.models.user import User
from tests.test_inline_attachments import (  # noqa: F401  (service is a fixture)
    _account,
    _owner,
    _slot,
    _states,
    service,
)


@pytest.fixture
def Session(tmp_path, monkeypatch):
    from src import mcp_tools
    from src.database import connection

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    engine = create_engine(f"sqlite:///{tmp_path}/deletion.db", connect_args={"check_same_thread": False})

    # Enforce every foreign key, as PostgreSQL does: a deletion order that
    # leaves a dangling reference must fail here, not first in production.
    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(connection, "engine", engine)
    monkeypatch.setattr(connection, "SessionLocal", factory)
    monkeypatch.setattr(mcp_tools, "SessionLocal", factory)
    return factory


async def _tool(name: str):
    from src.server import mcp

    return (await mcp.get_tool(name)).fn


def _owner_id(Session) -> int:
    from src.security.auth import local_owner_user

    with Session() as db:
        return local_owner_user(db).id


def _mailbox(db, owner_id: int, name: str, address: str) -> SMTPConfig:
    account = SMTPConfig(
        owner_user_id=owner_id,
        provider="imap",
        auth_type="password",
        name=name,
        account_name=address,
        host="outlook.office365.com",
        port=993,
        smtp_host="smtp.office365.com",
        smtp_port=587,
        username=address,
        credential_ciphertext="enc:test",
        imap_use_ssl=True,
        smtp_use_tls=True,
    )
    db.add(account)
    db.flush()
    return account


def _message(db, account: SMTPConfig, n: int) -> EmailLog:
    message = EmailLog(
        smtp_config_id=account.id,
        sender="someone@example.com",
        recipient=account.account_name,
        subject=f"Message {n}",
        provider_message_id=f"INBOX:1:{n}",
        message_id=f"<m{n}.{account.id}@example.com>",
        folder="INBOX",
        imap_uid=n,
        uid_validity=1,
        body_plain="Body",
        attachment_count=1,
    )
    db.add(message)
    db.flush()
    db.add_all(
        [
            EmailAttachment(email_log_id=message.id, filename="a.pdf", size=3, extraction_state="complete"),
            MailParticipant(email_log_id=message.id, role="from", email="someone@example.com", domain="example.com"),
            MessagePlacement(email_log_id=message.id, folder="INBOX", uid=n, uid_validity=1),
        ]
    )
    db.flush()
    return message


def _audit(db, owner_id: int, account: SMTPConfig, key: str, reply_to: EmailLog | None = None) -> SendAudit:
    audit = SendAudit(
        owner_user_id=owner_id,
        smtp_config_id=account.id,
        reply_to_email_id=reply_to.id if reply_to else None,
        idempotency_key=key,
        recipients_json=json.dumps(["to@example.com"]),
        subject="Sent",
        request_hash="0" * 64,
        status="sent",
    )
    db.add(audit)
    db.flush()
    return audit


@pytest.fixture
def mailboxes(Session):
    """Three accounts: one to delete, one beside it, and one another user owns.

    The first two share a mailbox address on purpose, the way an old password
    account and a newer OAuth connection to the same mailbox do.
    """
    owner = _owner_id(Session)
    with Session() as db:
        stranger = User(google_sub="someone-else", email="else@example.com")
        db.add(stranger)
        db.flush()
        doomed = _mailbox(db, owner, "progress.works", "owner@contoso.com")
        kept = _mailbox(db, owner, "Microsoft - owner@contoso.com", "owner@contoso.com")
        foreign = _mailbox(db, stranger.id, "Theirs", "else@example.com")
        first = _message(db, doomed, 1)
        _message(db, doomed, 2)
        _message(db, kept, 3)
        _message(db, foreign, 4)
        db.add(MailSyncCursor(smtp_config_id=doomed.id, folder="INBOX", uid_validity=1, last_uid=2))
        db.add(MailSyncCursor(smtp_config_id=kept.id, folder="INBOX", uid_validity=1, last_uid=3))
        _audit(db, owner, doomed, "sent-from-doomed", reply_to=first)
        _audit(db, owner, kept, "sent-from-kept")
        # A reply sent from the surviving account to a message only the doomed one indexed.
        _audit(db, owner, kept, "kept-replying-to-doomed", reply_to=first)
        db.commit()
        return {"owner": owner, "doomed": doomed.id, "kept": kept.id, "foreign": foreign.id, "stranger": stranger.id}


def _inventory(Session, account_id: int) -> dict:
    with Session() as db:
        message_ids = [row.id for row in db.query(EmailLog.id).filter(EmailLog.smtp_config_id == account_id)]
        return {
            "account": db.query(SMTPConfig).filter(SMTPConfig.id == account_id).count(),
            "messages": len(message_ids),
            "attachments": db.query(EmailAttachment).filter(EmailAttachment.email_log_id.in_(message_ids)).count(),
            "participants": db.query(MailParticipant).filter(MailParticipant.email_log_id.in_(message_ids)).count(),
            "placements": db.query(MessagePlacement).filter(MessagePlacement.email_log_id.in_(message_ids)).count(),
            "cursors": db.query(MailSyncCursor).filter(MailSyncCursor.smtp_config_id == account_id).count(),
        }


def _code(error: ToolError) -> str:
    return json.loads(str(error))["code"]


@pytest.mark.asyncio
async def test_without_confirmation_it_only_reports_what_would_go(Session, mailboxes):
    delete = await _tool("delete_mail_account")
    before = _inventory(Session, mailboxes["doomed"])

    result = await delete(account_id=mailboxes["doomed"])

    assert result["deleted"] is False
    assert result["name"] == "progress.works"
    assert result["address"] == "owner@contoso.com"
    assert result["would_remove"] == {"messages": 2, "attachments": 2}
    assert result["send_audits_kept"] == 1
    assert _inventory(Session, mailboxes["doomed"]) == before


@pytest.mark.asyncio
async def test_a_wrong_name_deletes_nothing(Session, mailboxes):
    delete = await _tool("delete_mail_account")
    before = _inventory(Session, mailboxes["doomed"])

    # The sibling's name, which is exactly the slip the confirmation exists for.
    with pytest.raises(ToolError) as refused:
        await delete(account_id=mailboxes["doomed"], confirm_name="Microsoft - owner@contoso.com")

    assert _code(refused.value) == "INVALID_ARGUMENT"
    assert _inventory(Session, mailboxes["doomed"]) == before


@pytest.mark.asyncio
async def test_confirmed_deletion_removes_the_account_and_its_index_and_keeps_the_audit(Session, mailboxes):
    delete = await _tool("delete_mail_account")
    kept_before = _inventory(Session, mailboxes["kept"])
    foreign_before = _inventory(Session, mailboxes["foreign"])

    result = await delete(account_id=mailboxes["doomed"], confirm_name="progress.works")

    assert result["deleted"] is True
    assert result["removed"] == {"messages": 2, "attachments": 2}
    assert result["send_audits_kept"] == 1
    assert _inventory(Session, mailboxes["doomed"]) == {
        "account": 0, "messages": 0, "attachments": 0, "participants": 0, "placements": 0, "cursors": 0,
    }
    # Nothing beside it moves: not the account sharing its address, not another user's.
    assert _inventory(Session, mailboxes["kept"]) == kept_before
    assert _inventory(Session, mailboxes["foreign"]) == foreign_before
    with Session() as db:
        audits = {audit.idempotency_key: audit for audit in db.query(SendAudit).all()}
        assert audits["sent-from-doomed"].smtp_config_id is None
        assert audits["sent-from-doomed"].reply_to_email_id is None
        assert audits["sent-from-doomed"].subject == "Sent"
        assert audits["sent-from-kept"].smtp_config_id == mailboxes["kept"]
        assert audits["kept-replying-to-doomed"].smtp_config_id == mailboxes["kept"]
        assert audits["kept-replying-to-doomed"].reply_to_email_id is None


@pytest.mark.asyncio
async def test_a_deleted_account_is_gone_from_every_tool(Session, mailboxes):
    delete = await _tool("delete_mail_account")
    await delete(account_id=mailboxes["doomed"], confirm_name="progress.works")

    summary = await (await _tool("list_mail_accounts"))()
    assert [account["id"] for account in summary["accounts"]] == [mailboxes["kept"]]
    assert summary["total_message_count"] == 1
    with pytest.raises(ToolError) as again:
        await delete(account_id=mailboxes["doomed"], confirm_name="progress.works")
    assert _code(again.value) == "NOT_FOUND"
    with pytest.raises(ToolError) as send:
        await (await _tool("send_mail"))(
            account_id=mailboxes["doomed"], to_addresses=["to@example.com"], subject="x", body_text="x"
        )
    assert _code(send.value) == "NOT_FOUND"


@pytest.mark.asyncio
async def test_another_users_account_is_not_found_and_untouched(Session, mailboxes):
    delete = await _tool("delete_mail_account")
    before = _inventory(Session, mailboxes["foreign"])

    for confirm in (None, "Theirs"):
        with pytest.raises(ToolError) as refused:
            await delete(account_id=mailboxes["foreign"], confirm_name=confirm)
        assert _code(refused.value) == "NOT_FOUND"

    assert _inventory(Session, mailboxes["foreign"]) == before


@pytest.mark.asyncio
async def test_an_account_that_never_sent_or_indexed_anything_deletes_too(Session, mailboxes):
    """The case that prompted this: a password account Exchange never let in."""
    with Session() as db:
        empty = _mailbox(db, mailboxes["owner"], "Never connected", "never@contoso.com")
        db.commit()
        empty_id = empty.id
    delete = await _tool("delete_mail_account")

    preview = await delete(account_id=empty_id)
    result = await delete(account_id=empty_id, confirm_name="Never connected")

    assert preview["would_remove"] == {"messages": 0, "attachments": 0}
    assert result["deleted"] is True
    assert _inventory(Session, empty_id)["account"] == 0


# --------------------------------------------------------------------------
# Sends racing a deletion
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_account_with_a_send_in_flight_is_not_deleted_until_it_settles(Session, mailboxes):
    from datetime import datetime, timedelta, timezone

    delete = await _tool("delete_mail_account")
    with Session() as db:
        pending = _audit(db, mailboxes["owner"], db.get(SMTPConfig, mailboxes["doomed"]), "in-flight")
        pending.status = "pending"
        db.commit()
        pending_id = pending.id
    before = _inventory(Session, mailboxes["doomed"])

    with pytest.raises(ToolError) as busy:
        await delete(account_id=mailboxes["doomed"], confirm_name="progress.works")

    payload = json.loads(str(busy.value))
    assert payload["code"] == "CONFLICT"
    assert payload["details"] == {"retry_after": 30}
    assert _inventory(Session, mailboxes["doomed"]) == before

    # A 'pending' left behind by a process that died mid-send must not block it forever.
    with Session() as db:
        db.get(SendAudit, pending_id).created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        db.commit()
    result = await delete(account_id=mailboxes["doomed"], confirm_name="progress.works")
    assert result["deleted"] is True


@pytest.mark.asyncio
async def test_a_send_whose_account_is_deleted_before_its_claim_keeps_its_slots(service, monkeypatch):  # noqa: F811
    """Nothing was sent, so the slot goes back to 'stored' -- not stranded in 'sending'."""
    from src.services import mail_service
    from src.services.account_deletion import delete_mail_account

    engine = service.Session.kw["bind"]
    event.listen(engine, "connect", lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"))
    engine.dispose()
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="a.txt", payload=b"hello")
    real_load = mail_service.load_uploads

    def load_after_the_account_is_gone(db, claimed):
        with service.Session() as other:
            delete_mail_account(other, owner.id, account_id=account.id, confirm_name=account.name)
        return real_load(db, claimed)

    monkeypatch.setattr(mail_service, "load_uploads", load_after_the_account_is_gone)
    send = await _tool("send_mail")

    # The claim is refused by the account reference; the MCP layer reports it as a failed call.
    with pytest.raises(IntegrityError):
        await send(
            account_id=account.id, to_addresses=["to@example.com"], subject="x", body_text="x",
            upload_ids=[slot["upload_id"]],
        )

    assert service.sent == []
    assert _states(service)[slot["upload_id"]] == "stored"


@pytest.mark.asyncio
async def test_a_delivered_reply_is_reported_as_delivered_when_its_account_goes_right_after(service, monkeypatch):  # noqa: F811
    from src.services import mail_service
    from src.services.account_deletion import delete_mail_account

    owner = _owner(service)
    account = _account(service, owner.id)
    with service.Session() as db:
        original = EmailLog(
            smtp_config_id=account.id, sender="a@example.com", recipient="owner@example.com", subject="Hi",
            provider_message_id="INBOX:1:1", message_id="<original@example.com>", folder="INBOX",
            imap_uid=1, uid_validity=1,
        )
        db.add(original)
        db.commit()
        original_id = original.id
    real_settle = mail_service.settle_uploads

    def settle_then_delete(db, claimed, *, outcome):
        real_settle(db, claimed, outcome=outcome)
        if outcome == "sent":
            with service.Session() as other:
                delete_mail_account(other, owner.id, account_id=account.id, confirm_name=account.name)

    monkeypatch.setattr(mail_service, "settle_uploads", settle_then_delete)
    send = await _tool("send_mail")

    result = await send(
        account_id=account.id, to_addresses=["a@example.com"], subject="Re: Hi", body_text="x",
        reply_to_email_id=original_id,
    )

    assert result["success"] is True
    assert result["threaded_reply"] is True
    assert result["reply_to_email_id"] == original_id
    assert len(service.sent) == 1
