"""Embedding an uploaded file in the body, exactly as Apple Mail or Outlook do.

The MIME layer can already build ``multipart/related`` with a Content-ID, and
the upload slots can already carry bytes to the server without them crossing the
conversation. These tests drive the whole path an AI client actually walks:
reserve a slot, PUT the bytes, reference ``cid:<content_id>`` in ``body_html``,
and name the slot in ``inline_upload_ids``.

Everything is asserted against the real MIME tree parsed back out of the
produced bytes -- never against a substring, which cannot tell a header from a
payload and cannot see whether ``related`` really wraps the body.
"""

import email
import email.message
import json
from email import policy
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.email.draft_builder import parse_content_id
from src.models.base import Base
from src.models.outbound_upload import OutboundUpload
from src.models.smtp_config import SMTPConfig
from src.models.user import User

PNG = b"\x89PNG\r\n\x1a\n\x00\xff\xfe binary payload \x00\x01\x02"
PDF = b"%PDF-1.4\x00\xff trailer"
GIF = b"GIF89a\x01\x00\x01\x00\x00\xff\x00,"


# --------------------------------------------------------------------------
# Fixtures: the real ASGI app, an isolated database, a scratch data volume
# --------------------------------------------------------------------------


class _CapturedSMTP:
    """Stands in for the socket, so the real MIME builder still runs."""

    def __init__(self):
        self.messages = []

    def send_message(self, message, to_addrs=None):
        self.messages.append(message)
        return {}


@pytest.fixture
def service(tmp_path, monkeypatch):
    from src import mcp_tools
    from src.database import connection
    from src.database.connection import get_db
    from src.handlers import email_handler
    from src.server import api_app, final_app

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    engine = create_engine(
        f"sqlite:///{tmp_path}/inline.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(connection, "engine", engine)
    monkeypatch.setattr(connection, "SessionLocal", Session)
    monkeypatch.setattr(mcp_tools, "SessionLocal", Session)

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    # api_app is mounted inside final_app and resolves its own dependencies, so
    # an override registered on final_app would silently do nothing.
    api_app.dependency_overrides[get_db] = override_db

    sent: list = []

    class _Manager:
        """Routes a send through the real MIME construction, stopping at the wire."""

        result = {"success": True, "delivery_state": "sent"}

        async def send_email_via_config(self, *, smtp_config_id, owner_user_id, **kwargs):
            from src.email.smtp_sender import EmailSender

            if not self.result.get("success"):
                return self.result
            sender = EmailSender(
                SimpleNamespace(
                    name="test-account",
                    account_name="owner@example.com",
                    username="owner@example.com",
                    auth_type="password",
                )
            )
            server = _CapturedSMTP()
            sender._server = server
            outcome = await sender.send_email(**kwargs)
            sent.extend(server.messages)
            return outcome

        async def invalidate(self, smtp_config_id):
            return None

    manager = _Manager()
    monkeypatch.setattr(email_handler, "email_sender_manager", manager)

    with TestClient(final_app) as client:
        yield SimpleNamespace(
            client=client,
            Session=Session,
            root=tmp_path / "outbound-uploads",
            sent=sent,
            manager=manager,
        )
    api_app.dependency_overrides.clear()


def _owner(service) -> User:
    from src.security.auth import local_owner_user

    with service.Session() as db:
        return local_owner_user(db)


def _account(service, owner_id: int) -> SMTPConfig:
    with service.Session() as db:
        account = SMTPConfig(
            owner_user_id=owner_id,
            provider="imap",
            auth_type="password",
            name="Outbound",
            account_name="owner@example.com",
            host="imap.example.com",
            port=993,
            smtp_host="smtp.example.com",
            smtp_port=465,
            username="owner@example.com",
            credential_ciphertext="enc:test",
            imap_use_ssl=True,
            smtp_use_ssl=True,
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        db.expunge(account)
        return account


async def _tool(name: str):
    from src.server import mcp

    return (await mcp.get_tool(name)).fn


def _error_code(error: ToolError) -> str:
    return json.loads(str(error))["code"]


def _error_message(error: ToolError) -> str:
    return json.loads(str(error))["message"]


def _put(service, url: str, body: bytes):
    parts = urlsplit(url)
    return service.client.put(
        f"{parts.path}?{parts.query}" if parts.query else parts.path,
        content=body,
    )


async def _slot(service, *, filename: str, payload: bytes) -> dict:
    """Reserve a slot and fill it, exactly as an AI client would."""
    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename=filename, size_bytes=len(payload))
    response = _put(service, slot["upload_url"], payload)
    assert response.status_code == 200, response.text
    return slot


def _states(service) -> dict[str, str]:
    with service.Session() as db:
        return {row.upload_id: row.state for row in db.query(OutboundUpload).all()}


# --------------------------------------------------------------------------
# Reading the produced bytes back as a real MIME tree
# --------------------------------------------------------------------------


def parse(raw: bytes) -> email.message.EmailMessage:
    return email.message_from_bytes(raw, policy=policy.default)


def shape(msg):
    """The structural skeleton: content type plus children, nothing else."""
    if msg.is_multipart():
        return (msg.get_content_type(), [shape(part) for part in msg.iter_parts()])
    return msg.get_content_type()


def leaves(msg):
    if msg.is_multipart():
        for part in msg.iter_parts():
            yield from leaves(part)
    else:
        yield msg


def describe(part):
    """What a mail client renders from, ignoring the Content-ID's own value.

    The id is unique per slot by construction, so two equivalent messages built
    from two different uploads can never share one. Whether each part carries
    the *right* id is asserted separately.
    """
    payload = part.get_payload(decode=True)
    if part.get_content_maintype() == "text":
        # The two builders pick different (equally valid) transfer encodings for
        # text and one appends a newline; neither is visible to a reader.
        payload = payload.rstrip(b"\r\n")
    return (
        part.get_content_type(),
        part.get_content_disposition(),
        part.get_filename(),
        payload,
    )


def sent_message(service):
    assert len(service.sent) == 1, f"expected exactly one send, got {len(service.sent)}"
    return parse(service.sent[0].as_bytes())


def part_with_type(message, content_type: str):
    matches = [part for part in leaves(message) if part.get_content_type() == content_type]
    assert len(matches) == 1, f"expected one {content_type} part, got {len(matches)}"
    return matches[0]


# --------------------------------------------------------------------------
# The server hands out the Content-ID
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_begin_attachment_upload_returns_a_content_id_the_mime_layer_accepts(service):
    from src.services.upload_service import content_id_for

    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="logo.png")

    content_id = slot["content_id"]
    # Bare: the MIME layer adds the angle brackets itself, and an id that
    # arrived already bracketed is one it refuses.
    assert parse_content_id(content_id) == content_id
    assert not content_id.startswith("<") and not content_id.endswith(">")
    # Derived from the slot, so it is the same answer every time it is asked
    # for -- nothing has to be stored, and no client text ever reaches a header.
    assert content_id_for(slot["upload_id"]) == content_id
    assert content_id_for(slot["upload_id"]) == content_id
    assert slot["upload_id"] in content_id


@pytest.mark.asyncio
async def test_each_slot_gets_its_own_content_id(service):
    begin = await _tool("begin_attachment_upload")

    first = await begin(filename="one.png")
    second = await begin(filename="two.png")

    assert first["content_id"] != second["content_id"]


@pytest.mark.asyncio
async def test_a_content_id_does_not_depend_on_deployment_configuration(service, monkeypatch):
    """The id the client was handed must still be the id the send emits.

    A slot lives 900 seconds and the client writes the id into its HTML in
    between, so anything that can change underneath it -- a restart onto a new
    public_base_url, a reverse proxy moved to another host -- would turn the
    caller's cid: reference into a dangling one. The part is then demoted to an
    ordinary attachment and the image silently vanishes from the body, with
    nothing anywhere reporting an error. So the id is derived from the slot and
    from nothing else.
    """
    from src.services.upload_service import content_id_for

    begin = await _tool("begin_attachment_upload")
    slot = await begin(filename="logo.png")

    monkeypatch.setattr(settings, "public_base_url", "https://somewhere-else.example.net:8443/mail")

    assert content_id_for(slot["upload_id"]) == slot["content_id"]
    assert parse_content_id(content_id_for(slot["upload_id"])) is not None


@pytest.mark.asyncio
async def test_the_content_id_header_is_never_folded(service):
    """One line, like every real mail client emits.

    ``Content-ID: <id>`` past 78 columns is folded onto a continuation line by
    the generator. A compliant parser unfolds it and both builders here round
    trip it correctly -- but Apple Mail and Outlook emit it unfolded, matching
    them is the point, and a folded header is the kind of difference a picky
    downstream client notices. The id's length is fixed (a 43 character
    token_urlsafe(32) plus the host), so this is a real budget: it broke once
    already when the host was four characters longer.
    """
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Unfolded",
        body_text="Body",
        body_html=f'<img src="cid:{slot["content_id"]}">',
        inline_upload_ids=[slot["upload_id"]],
    )

    raw = service.sent[0].as_bytes()
    header = f"Content-ID: <{slot['content_id']}>".encode()
    assert header in raw.replace(b"\r\n", b"\n")
    # And the parser agrees, with no leading fold whitespace in the value.
    assert part_with_type(sent_message(service), "image/png").get("Content-ID") == (
        f"<{slot['content_id']}>"
    )


# --------------------------------------------------------------------------
# Embedding, through the send path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_inline_upload_is_embedded_in_the_body(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Embedded",
        body_text="Our logo.",
        body_html=f'<p>Our logo <img src="cid:{slot["content_id"]}"></p>',
        inline_upload_ids=[slot["upload_id"]],
    )

    message = sent_message(service)
    # related wraps the alternative body: that is what makes a cid: URL in the
    # HTML resolvable at all. No ordinary attachment, so no mixed level.
    assert shape(message) == (
        "multipart/related",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png"],
    )
    image = part_with_type(message, "image/png")
    assert image.get("Content-ID") == f"<{slot['content_id']}>"
    assert image.get_content_disposition() == "inline"
    assert image.get_filename() == "logo.png"
    assert image.get_payload(decode=True) == PNG
    html = part_with_type(message, "text/html")
    assert f"cid:{slot['content_id']}" in html.get_content()
    assert _states(service) == {slot["upload_id"]: "consumed"}


@pytest.mark.asyncio
async def test_a_single_quoted_cid_reference_still_embeds_the_image(service):
    """The AI client writes the HTML, and it picks its own quoting form.

    ``<img src='cid:ID'>`` is one of the two standard forms. It used to fail the
    composer's reference test, so the image was demoted to an ordinary
    attachment -- gone from the body, with ``send_mail`` returning success and
    no warning anywhere for the client to act on.
    """
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)

    send = await _tool("send_mail")
    result = await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Single quoted",
        body_text="Our logo.",
        body_html=f"<p>Our logo <img src='cid:{slot['content_id']}'></p>",
        inline_upload_ids=[slot["upload_id"]],
    )

    assert result["success"] is True
    message = sent_message(service)
    assert shape(message) == (
        "multipart/related",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png"],
    )
    image = part_with_type(message, "image/png")
    assert image.get_content_disposition() == "inline"
    assert image.get("Content-ID") == f"<{slot['content_id']}>"


@pytest.mark.asyncio
async def test_one_inline_and_one_ordinary_produce_the_full_tree(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    image = await _slot(service, filename="logo.png", payload=PNG)
    document = await _slot(service, filename="report.pdf", payload=PDF)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Both",
        body_text="Logo above, report attached.",
        body_html=f'<p><img src="cid:{image["content_id"]}"></p>',
        upload_ids=[document["upload_id"]],
        inline_upload_ids=[image["upload_id"]],
    )

    message = sent_message(service)
    assert shape(message) == (
        "multipart/mixed",
        [
            (
                "multipart/related",
                [("multipart/alternative", ["text/plain", "text/html"]), "image/png"],
            ),
            "application/pdf",
        ],
    )
    embedded = part_with_type(message, "image/png")
    attached = part_with_type(message, "application/pdf")
    assert embedded.get_content_disposition() == "inline"
    assert embedded.get("Content-ID") == f"<{image['content_id']}>"
    assert embedded.get_payload(decode=True) == PNG
    assert attached.get_content_disposition() == "attachment"
    assert attached.get("Content-ID") is None
    assert attached.get_filename() == "report.pdf"
    assert attached.get_payload(decode=True) == PDF
    assert set(_states(service).values()) == {"consumed"}


@pytest.mark.asyncio
async def test_an_ordinary_only_send_keeps_todays_tree_and_lifecycle(service):
    """Regression guard on the path that already ships.

    Structure, disposition, filename, bytes and the slot's lifecycle are exactly
    what they were before inline parts existed. The one deliberate difference is
    the Content-Type: the row already recorded what the sniffer saw at PUT time,
    and labelling a PDF ``application/octet-stream`` made clients show a generic
    file rather than a document.
    """
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="report.pdf", payload=PDF)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Report",
        body_text="Attached.",
        body_html="<p>Attached.</p>",
        upload_ids=[slot["upload_id"]],
    )

    message = sent_message(service)
    assert shape(message) == (
        "multipart/mixed",
        [("multipart/alternative", ["text/plain", "text/html"]), "application/pdf"],
    )
    part = list(leaves(message))[-1]
    assert part.get_content_disposition() == "attachment"
    assert part.get_filename() == "report.pdf"
    assert part.get("Content-ID") is None
    assert part.get_payload(decode=True) == PDF
    assert _states(service) == {slot["upload_id"]: "consumed"}


@pytest.mark.asyncio
async def test_a_send_with_no_attachments_at_all_is_untouched(service):
    owner = _owner(service)
    account = _account(service, owner.id)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Plain",
        body_text="Body",
        body_html="<p>Body</p>",
    )

    assert shape(sent_message(service)) == (
        "multipart/mixed",
        [("multipart/alternative", ["text/plain", "text/html"])],
    )


@pytest.fixture
def indexed_attachment(monkeypatch):
    """A re-sendable attachment already on a message in this mailbox."""
    from src import mcp_tools
    from src.models.attachment import EmailAttachment

    row = EmailAttachment(
        id=4242,
        filename="prior.pdf",
        content_type="application/pdf",
        claimed_content_type="application/octet-stream",
        detected_content_type="application/pdf",
    )

    async def fake_refetch(db, user_id, attachment_id):
        assert attachment_id == row.id
        return row, PDF

    monkeypatch.setattr(mcp_tools, "refetch_attachment_bytes", fake_refetch)
    return row


@pytest.mark.asyncio
async def test_a_resent_indexed_attachment_carries_its_real_content_type(
    service, indexed_attachment
):
    """The row already knows what the bytes are; say so rather than 'octet-stream'.

    detected_content_type is sniffed at index time from the bytes themselves,
    not taken from the original sender's declaration, so it is at least as
    trustworthy as the claimed type -- and a PDF announced as an opaque blob is
    a generic file icon in the recipient's client.
    """
    owner = _owner(service)
    account = _account(service, owner.id)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Again",
        body_text="Body",
        body_html="<p>Body</p>",
        attachment_ids=[indexed_attachment.id],
    )

    message = sent_message(service)
    assert shape(message) == (
        "multipart/mixed",
        [("multipart/alternative", ["text/plain", "text/html"]), "application/pdf"],
    )
    part = part_with_type(message, "application/pdf")
    assert part.get_content_disposition() == "attachment"
    assert part.get_filename() == "prior.pdf"
    assert part.get_payload(decode=True) == PDF


@pytest.mark.asyncio
async def test_an_indexed_attachment_that_was_never_sniffed_is_unchanged(
    service, indexed_attachment
):
    """Nothing was detected for rows stored before sniffing existed.

    The composer answers a missing type with application/octet-stream, which is
    exactly what every one of these parts used to get -- so the old rows keep
    the old behaviour rather than acquiring a guess.
    """
    indexed_attachment.detected_content_type = None
    owner = _owner(service)
    account = _account(service, owner.id)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Legacy",
        body_text="Body",
        body_html="<p>Body</p>",
        attachment_ids=[indexed_attachment.id],
    )

    assert shape(sent_message(service)) == (
        "multipart/mixed",
        [("multipart/alternative", ["text/plain", "text/html"]), "application/octet-stream"],
    )


@pytest.mark.asyncio
async def test_an_indexed_attachment_shares_the_cap_with_both_upload_lists(
    service, indexed_attachment, monkeypatch
):
    owner = _owner(service)
    account = _account(service, owner.id)
    document = await _slot(service, filename="report.pdf", payload=PDF)
    image = await _slot(service, filename="logo.png", payload=PNG)
    monkeypatch.setattr(settings, "max_outbound_attachments", 2)

    send = await _tool("send_mail")
    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Three parts, cap of two",
            body_html=f'<img src="cid:{image["content_id"]}">',
            attachment_ids=[indexed_attachment.id],
            upload_ids=[document["upload_id"]],
            inline_upload_ids=[image["upload_id"]],
        )

    assert _error_code(error.value) == "INVALID_ARGUMENT"
    assert service.sent == []
    assert set(_states(service).values()) == {"stored"}


@pytest.mark.asyncio
async def test_the_http_send_route_accepts_inline_upload_ids_too(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)

    response = service.client.post(
        "/api/v1/send",
        json={
            "account_id": account.id,
            "to_addresses": ["recipient@example.com"],
            "subject": "Embedded over HTTP",
            "body_text": "Logo.",
            "body_html": f'<img src="cid:{slot["content_id"]}">',
            "inline_upload_ids": [slot["upload_id"]],
            "idempotency_key": "http-inline",
        },
    )

    assert response.status_code == 200, response.text
    message = sent_message(service)
    assert shape(message) == (
        "multipart/related",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png"],
    )
    assert part_with_type(message, "image/png").get("Content-ID") == f"<{slot['content_id']}>"


# --------------------------------------------------------------------------
# Drafts render the same way, on both branches
# --------------------------------------------------------------------------


@pytest.fixture
def imap_drafts(monkeypatch):
    """Capture what an IMAP APPEND would have filed."""
    from src.services import write_service

    appended: dict = {}

    async def fake_with_mailbox(db, config, operation):
        return await operation(object())

    async def fake_list_folders(client):
        return [{"name": "Drafts", "special_use": "drafts"}]

    async def fake_append(client, folder, raw, *, flags):
        appended.update(folder=folder, raw=raw, flags=flags)
        return 7

    monkeypatch.setattr(write_service, "_with_mailbox", fake_with_mailbox)
    monkeypatch.setattr(write_service, "list_folders", fake_list_folders)
    monkeypatch.setattr(write_service, "append_message", fake_append)
    return appended


@pytest.fixture
def gmail_drafts(monkeypatch):
    """Capture what the Gmail API branch would have created."""
    from src.services import write_service

    created: dict = {}

    async def fake_gmail(db, config, operation):
        class _Client:
            async def create_draft(self, raw, thread_id=None):
                created.update(raw=raw, thread_id=thread_id)
                return {"id": "draft-1"}

        return await operation(_Client())

    monkeypatch.setattr(write_service, "_gmail", fake_gmail)
    return created


def _make_gmail(service, account_id: int) -> None:
    with service.Session() as db:
        row = db.query(SMTPConfig).filter_by(id=account_id).one()
        row.provider = "gmail"
        row.auth_type = "oauth2"
        db.commit()


@pytest.mark.asyncio
async def test_a_draft_embeds_the_image_on_the_imap_branch(service, imap_drafts):
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)

    save = await _tool("save_draft")
    result = await save(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Embedded draft",
        body_text="Our logo.",
        body_html=f'<p><img src="cid:{slot["content_id"]}"></p>',
        inline_upload_ids=[slot["upload_id"]],
    )

    assert result["success"] is True
    assert imap_drafts["folder"] == "Drafts"
    draft = parse(imap_drafts["raw"])
    assert shape(draft) == (
        "multipart/related",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png"],
    )
    image = part_with_type(draft, "image/png")
    assert image.get("Content-ID") == f"<{slot['content_id']}>"
    assert image.get_content_disposition() == "inline"
    assert image.get_payload(decode=True) == PNG
    assert _states(service) == {slot["upload_id"]: "consumed"}


@pytest.mark.asyncio
async def test_a_draft_embeds_the_image_on_the_gmail_branch(service, gmail_drafts):
    owner = _owner(service)
    account = _account(service, owner.id)
    _make_gmail(service, account.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)

    save = await _tool("save_draft")
    result = await save(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Embedded gmail draft",
        body_text="Our logo.",
        body_html=f'<p><img src="cid:{slot["content_id"]}"></p>',
        inline_upload_ids=[slot["upload_id"]],
    )

    assert result["draft_id"] == "draft-1"
    draft = parse(gmail_drafts["raw"])
    assert shape(draft) == (
        "multipart/related",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png"],
    )
    image = part_with_type(draft, "image/png")
    assert image.get("Content-ID") == f"<{slot['content_id']}>"
    assert image.get_content_disposition() == "inline"
    assert image.get_payload(decode=True) == PNG
    assert _states(service) == {slot["upload_id"]: "consumed"}


@pytest.mark.parametrize(
    "body_text",
    [
        pytest.param("Body", id="text_and_html"),
        # An html-only body is the natural shape for a message built around an
        # embedded image, and it is exactly the case this test used never to
        # cover: both copies were always given body_text='Body', so a draft
        # carrying an extra empty text/plain leaf went unnoticed.
        pytest.param("", id="html_only"),
    ],
)
@pytest.mark.asyncio
async def test_the_draft_and_the_sent_copy_render_identically(service, imap_drafts, body_text):
    """A message composed once must look the same saved as it does sent.

    Two slots, because a slot is single use -- so the two Content-IDs differ by
    construction. Everything a reader sees is compared part for part, and each
    copy is then checked to reference its own id.
    """
    owner = _owner(service)
    account = _account(service, owner.id)
    for_sending = await _slot(service, filename="logo.png", payload=PNG)
    for_drafting = await _slot(service, filename="logo.png", payload=PNG)
    document = await _slot(service, filename="report.pdf", payload=PDF)
    second_document = await _slot(service, filename="report.pdf", payload=PDF)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Twice",
        body_text=body_text,
        body_html=f'<p><img src="cid:{for_sending["content_id"]}"></p>',
        upload_ids=[document["upload_id"]],
        inline_upload_ids=[for_sending["upload_id"]],
    )
    save = await _tool("save_draft")
    await save(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Twice",
        body_text=body_text,
        body_html=f'<p><img src="cid:{for_drafting["content_id"]}"></p>',
        upload_ids=[second_document["upload_id"]],
        inline_upload_ids=[for_drafting["upload_id"]],
    )

    posted = sent_message(service)
    drafted = parse(imap_drafts["raw"])

    assert shape(posted) == shape(drafted)
    # The HTML differs only in the id it points at, so compare it separately.
    assert [describe(part) for part in leaves(posted) if part.get_content_type() != "text/html"] == [
        describe(part) for part in leaves(drafted) if part.get_content_type() != "text/html"
    ]
    assert part_with_type(posted, "image/png").get("Content-ID") == f"<{for_sending['content_id']}>"
    assert part_with_type(drafted, "image/png").get("Content-ID") == f"<{for_drafting['content_id']}>"
    assert f"cid:{for_sending['content_id']}" in part_with_type(posted, "text/html").get_content()
    assert f"cid:{for_drafting['content_id']}" in part_with_type(drafted, "text/html").get_content()


# --------------------------------------------------------------------------
# One message, one budget -- across both lists
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_and_ordinary_uploads_share_the_per_message_count_cap(service, monkeypatch):
    owner = _owner(service)
    account = _account(service, owner.id)
    first = await _slot(service, filename="one.pdf", payload=PDF)
    second = await _slot(service, filename="two.pdf", payload=PDF)
    image = await _slot(service, filename="logo.png", payload=PNG)
    monkeypatch.setattr(settings, "max_outbound_attachments", 2)

    send = await _tool("send_mail")
    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Too many",
            body_html=f'<img src="cid:{image["content_id"]}">',
            upload_ids=[first["upload_id"], second["upload_id"]],
            inline_upload_ids=[image["upload_id"]],
        )

    assert _error_code(error.value) == "INVALID_ARGUMENT"
    assert service.sent == []
    # Refused before anything was taken, so every slot is still sendable.
    assert set(_states(service).values()) == {"stored"}


@pytest.mark.asyncio
async def test_inline_and_ordinary_uploads_share_the_byte_cap_and_claim_nothing(
    service, monkeypatch
):
    from src.services import mail_service

    owner = _owner(service)
    account = _account(service, owner.id)
    document = await _slot(service, filename="report.pdf", payload=PDF)
    image = await _slot(service, filename="logo.png", payload=PNG)
    # Lowered only now: the PUTs above had to fit, the send must not.
    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", len(PDF) + len(PNG) - 1)

    measured: list[list[str]] = []
    real_measure = mail_service.measure_uploads

    def spy_measure(db, user_id, upload_ids):
        ids = list(upload_ids)
        measured.append(ids)
        return real_measure(db, user_id, ids)

    claims: list = []

    def forbidden_claim(db, user_id, upload_ids):
        claims.append(list(upload_ids))
        raise AssertionError("a refused message must not claim a slot")

    monkeypatch.setattr(mail_service, "measure_uploads", spy_measure)
    monkeypatch.setattr(mail_service, "claim_uploads_for_send", forbidden_claim)

    send = await _tool("send_mail")
    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Too large",
            body_html=f'<img src="cid:{image["content_id"]}">',
            upload_ids=[document["upload_id"]],
            inline_upload_ids=[image["upload_id"]],
        )

    assert _error_code(error.value) == "INVALID_ARGUMENT"
    assert claims == []
    # Both lists were weighed together, from the recorded sizes, before any
    # payload was opened and before any slot moved.
    assert measured and set(measured[0]) == {document["upload_id"], image["upload_id"]}
    assert set(_states(service).values()) == {"stored"}
    assert service.sent == []


@pytest.mark.asyncio
async def test_a_draft_shares_the_same_caps_across_both_lists(service, monkeypatch, imap_drafts):
    from src.services import write_service

    owner = _owner(service)
    account = _account(service, owner.id)
    document = await _slot(service, filename="report.pdf", payload=PDF)
    image = await _slot(service, filename="logo.png", payload=PNG)
    monkeypatch.setattr(settings, "max_outbound_attachment_bytes", len(PDF) + len(PNG) - 1)

    claims: list = []

    def forbidden_claim(db, user_id, upload_ids):
        claims.append(list(upload_ids))
        raise AssertionError("a refused draft must not claim a slot")

    monkeypatch.setattr(write_service, "claim_uploads_for_send", forbidden_claim)

    save = await _tool("save_draft")
    with pytest.raises(ToolError) as error:
        await save(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Too large",
            body_html=f'<img src="cid:{image["content_id"]}">',
            upload_ids=[document["upload_id"]],
            inline_upload_ids=[image["upload_id"]],
        )

    assert _error_code(error.value) == "INVALID_ARGUMENT"
    assert claims == []
    assert imap_drafts == {}
    assert set(_states(service).values()) == {"stored"}


@pytest.mark.asyncio
async def test_a_draft_shares_the_per_message_count_cap(service, monkeypatch, imap_drafts):
    owner = _owner(service)
    account = _account(service, owner.id)
    first = await _slot(service, filename="one.pdf", payload=PDF)
    second = await _slot(service, filename="two.pdf", payload=PDF)
    image = await _slot(service, filename="logo.png", payload=PNG)
    monkeypatch.setattr(settings, "max_outbound_attachments", 2)

    save = await _tool("save_draft")
    with pytest.raises(ToolError) as error:
        await save(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Too many",
            body_html=f'<img src="cid:{image["content_id"]}">',
            upload_ids=[first["upload_id"], second["upload_id"]],
            inline_upload_ids=[image["upload_id"]],
        )

    assert _error_code(error.value) == "INVALID_ARGUMENT"
    assert imap_drafts == {}
    assert set(_states(service).values()) == {"stored"}


# --------------------------------------------------------------------------
# One slot, one disposition, one send
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_id_in_both_lists_is_refused_rather_than_deduplicated(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)

    send = await _tool("send_mail")
    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Ambiguous",
            body_html=f'<img src="cid:{slot["content_id"]}">',
            upload_ids=[slot["upload_id"]],
            inline_upload_ids=[slot["upload_id"]],
        )

    assert _error_code(error.value) == "INVALID_ARGUMENT"
    assert "both" in _error_message(error.value).lower()
    assert service.sent == []
    assert _states(service) == {slot["upload_id"]: "stored"}


@pytest.mark.asyncio
async def test_an_id_in_both_lists_is_refused_on_the_draft_path_too(service, imap_drafts):
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)

    save = await _tool("save_draft")
    with pytest.raises(ToolError) as error:
        await save(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Ambiguous",
            body_html=f'<img src="cid:{slot["content_id"]}">',
            upload_ids=[slot["upload_id"]],
            inline_upload_ids=[slot["upload_id"]],
        )

    assert _error_code(error.value) == "INVALID_ARGUMENT"
    assert imap_drafts == {}
    assert _states(service) == {slot["upload_id"]: "stored"}


@pytest.mark.asyncio
async def test_another_owners_slot_is_never_embeddable(service):
    from src.services.upload_service import begin_upload

    owner = _owner(service)
    account = _account(service, owner.id)
    with service.Session() as db:
        stranger = User(google_sub="stranger", email="stranger@example.com")
        db.add(stranger)
        db.commit()
        foreign = begin_upload(db, stranger.id, filename="theirs.png")
        foreign_id = foreign.upload_id

    send = await _tool("send_mail")
    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Theirs",
            body_html='<img src="cid:whatever">',
            inline_upload_ids=[foreign_id],
        )

    # Not "forbidden": an id that is not yours must not be confirmed to exist.
    assert _error_code(error.value) == "NOT_FOUND"
    assert service.sent == []
    assert _states(service) == {foreign_id: "pending"}


@pytest.mark.asyncio
async def test_a_slot_spent_inline_cannot_be_re_sent_from_either_list(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)
    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="First",
        body_html=f'<img src="cid:{slot["content_id"]}">',
        inline_upload_ids=[slot["upload_id"]],
        idempotency_key="inline-first",
    )

    with pytest.raises(ToolError) as as_ordinary:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Again as an attachment",
            upload_ids=[slot["upload_id"]],
            idempotency_key="inline-again-ordinary",
        )
    with pytest.raises(ToolError) as as_inline:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Again embedded",
            body_html=f'<img src="cid:{slot["content_id"]}">',
            inline_upload_ids=[slot["upload_id"]],
            idempotency_key="inline-again-inline",
        )

    assert _error_code(as_ordinary.value) == "CONFLICT"
    assert _error_code(as_inline.value) == "CONFLICT"
    assert len(service.sent) == 1


@pytest.mark.asyncio
async def test_a_slot_spent_as_an_attachment_cannot_be_embedded_afterwards(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)
    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="First",
        upload_ids=[slot["upload_id"]],
        idempotency_key="ordinary-first",
    )

    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Now embedded",
            body_html=f'<img src="cid:{slot["content_id"]}">',
            inline_upload_ids=[slot["upload_id"]],
            idempotency_key="ordinary-then-inline",
        )

    assert _error_code(error.value) == "CONFLICT"
    assert len(service.sent) == 1


@pytest.mark.asyncio
async def test_a_failed_send_leaves_both_lists_sendable_again(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    image = await _slot(service, filename="logo.png", payload=PNG)
    document = await _slot(service, filename="report.pdf", payload=PDF)
    service.manager.result = {
        "success": False,
        "message": "SMTP refused",
        "delivery_state": "failed",
    }

    send = await _tool("send_mail")
    with pytest.raises(ToolError) as error:
        await send(
            account_id=account.id,
            to_addresses=["recipient@example.com"],
            subject="Fails",
            body_html=f'<img src="cid:{image["content_id"]}">',
            upload_ids=[document["upload_id"]],
            inline_upload_ids=[image["upload_id"]],
            idempotency_key="doomed",
        )

    assert _error_code(error.value) == "PROVIDER_ERROR"
    assert set(_states(service).values()) == {"stored"}

    service.manager.result = {"success": True, "delivery_state": "sent"}
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Succeeds",
        body_text="Body",
        body_html=f'<img src="cid:{image["content_id"]}">',
        upload_ids=[document["upload_id"]],
        inline_upload_ids=[image["upload_id"]],
        idempotency_key="the-retry",
    )

    message = sent_message(service)
    assert part_with_type(message, "image/png").get_content_disposition() == "inline"
    assert part_with_type(message, "application/pdf").get_content_disposition() == "attachment"
    assert set(_states(service).values()) == {"consumed"}


# --------------------------------------------------------------------------
# Edges of the inline contract
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreferenced_inline_upload_is_still_delivered(service):
    """Nothing points at it, so it cannot render in the body -- but it is sent.

    A related part no cid: URL names is invisible in most clients, and silently
    dropping a file the user attached is worse than putting it at the bottom.
    """
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Forgot the img tag",
        body_text="Body",
        body_html="<p>no image here</p>",
        inline_upload_ids=[slot["upload_id"]],
    )

    message = sent_message(service)
    assert shape(message) == (
        "multipart/mixed",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png"],
    )
    part = part_with_type(message, "image/png")
    assert part.get_content_disposition() == "attachment"
    assert part.get("Content-ID") is None
    assert part.get_payload(decode=True) == PNG
    assert _states(service) == {slot["upload_id"]: "consumed"}


@pytest.mark.asyncio
async def test_an_inline_upload_with_no_html_body_is_still_delivered(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="logo.png", payload=PNG)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Text only",
        body_text="Body",
        inline_upload_ids=[slot["upload_id"]],
    )

    message = sent_message(service)
    part = part_with_type(message, "image/png")
    assert part.get_content_disposition() == "attachment"
    assert part.get_payload(decode=True) == PNG


@pytest.mark.asyncio
async def test_a_non_image_inline_upload_behaves_sanely(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    slot = await _slot(service, filename="terms.pdf", payload=PDF)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Inline document",
        body_text="Terms below.",
        body_html=f'<p><object data="cid:{slot["content_id"]}"></object></p>',
        inline_upload_ids=[slot["upload_id"]],
    )

    message = sent_message(service)
    assert shape(message) == (
        "multipart/related",
        [("multipart/alternative", ["text/plain", "text/html"]), "application/pdf"],
    )
    part = part_with_type(message, "application/pdf")
    assert part.get_content_disposition() == "inline"
    assert part.get("Content-ID") == f"<{slot['content_id']}>"
    assert part.get_filename() == "terms.pdf"
    assert part.get_payload(decode=True) == PDF


@pytest.mark.asyncio
async def test_several_inline_uploads_each_keep_their_own_id(service):
    owner = _owner(service)
    account = _account(service, owner.id)
    first = await _slot(service, filename="logo.png", payload=PNG)
    second = await _slot(service, filename="sig.gif", payload=GIF)

    send = await _tool("send_mail")
    await send(
        account_id=account.id,
        to_addresses=["recipient@example.com"],
        subject="Two images",
        body_text="Body",
        body_html=(
            f'<img src="cid:{first["content_id"]}">'
            f'<img src="cid:{second["content_id"]}">'
        ),
        inline_upload_ids=[first["upload_id"], second["upload_id"]],
    )

    message = sent_message(service)
    assert shape(message) == (
        "multipart/related",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png", "image/gif"],
    )
    assert [part.get("Content-ID") for part in leaves(message) if part.get("Content-ID")] == [
        f"<{first['content_id']}>",
        f"<{second['content_id']}>",
    ]
    assert part_with_type(message, "image/png").get_payload(decode=True) == PNG
    assert part_with_type(message, "image/gif").get_payload(decode=True) == GIF
