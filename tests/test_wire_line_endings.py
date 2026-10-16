"""Every message handed to a provider ends its lines with CRLF.

Reproduced against Exchange Online on 16 October 2026. A draft whose APPEND literal
used bare LF line endings was stored with "=" in place of the first character after
every quoted-printable soft line break ("länger" became "läng=r", "</span>" became
"<=span>"). UTF-8 sequences split by a break were destroyed, and a cid: URL that
crossed one no longer matched its image. The same bytes with CRLF line endings were
stored intact. RFC 2045 defines the soft break as "=" followed by CRLF, and RFC 3501
requires CRLF in an APPEND literal. aioimaplib, unlike imaplib, sends the literal
exactly as it is given.

Python's own parser forgives a bare LF, so checking with it proves nothing: the
old drafts parsed perfectly here. Every wire representation below is decoded the
way Exchange decoded it instead, on each path that hands a message to a provider,
captured where it leaves for the provider: both draft branches (the IMAP literal
and the Gmail API request), Microsoft Graph and SMTP. The send paths encode text
as base64, so they never had soft breaks to lose; there the line endings and the
content equality with the draft are what is checked.
"""

import base64
import email
import hashlib
import json
from email import policy
from email.utils import getaddresses
from types import SimpleNamespace

import httpx
import pytest

from src.email import sanitize_filename
from src.email.draft_builder import build_draft, with_crlf_line_endings
from src.email.graph_sender import GraphMailSender
from src.email.imap_writer import append_message
from tests.test_graph_sender import Graph, tokens  # noqa: F401  (tokens is a fixture)
from tests.test_graph_sender import account as microsoft_account
from tests.test_inline_attachments import (  # noqa: F401  (service is a fixture)
    _account,
    _make_gmail,
    _owner,
    _slot,
    _tool,
    service,
)
from tests.test_smtp_stale_connection import sender, smtp_server  # noqa: F401  (fixtures)

# The shape of the server's own Content-IDs (upload_service.content_id_for). At this
# length and position the HTML line is long enough for a soft break to land inside
# the cid: URL, which is the case that cost the logo.
CONTENT_ID = "BGTQ_oNutKUf8xDZUEJiAxlhaMjFqsBQ6CTCnPAkuaY@mailindex.invalid"

BODY_TEXT = (
    "Grüße aus der Prüfung: Dieser Absatz ist absichtlich länger als sechsundsiebzig Zeichen, "
    "damit die Kodierung umbrechen muss. Runde. Der nächste Satz folgt direkt. Wir haben uns "
    "vorgestellt, dass ä ö ü ß und € heil ankommen.\n"
    "Gleichung: a = b + c, Rabatt=10% und x=3D bleiben wörtlich stehen; das Gleichheitszeichen "
    "darf weder verschwinden noch sich vermehren.\n"
    "Kurze Zeile.\n"
    "Letzte Zeile mit Umlauten am Ende: Äpfel, Öl, Übermut – fertig.\n"
)


def html_for(content_id: str) -> str:
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:20px;color:#191917;'
        'background-color:#FFFFFF;padding:0 0 12px 0;margin:0;border:0;">'
        '<p style="margin:0 0 8px 0;">Grüße aus der Prüfung: Dieser Absatz ist absichtlich länger als '
        "sechsundsiebzig Zeichen, damit die Kodierung umbrechen muss. "
        '<span style="font-weight:bold;color:#0B6E69;">Runde.</span> Der nächste Satz folgt direkt. '
        "Wir haben <span>uns</span> vorgestellt, dass ä ö ü ß und € heil ankommen.</p>"
        '<p style="margin:0 0 8px 0;">Gleichung: a = b + c, Rabatt=10% und x=3D bleiben wörtlich stehen.</p>'
        '<table role="presentation" border="0" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;table-layout:fixed;width:100%;max-width:440px;"><tr>'
        f'<td style="padding:0 16px 0 0;vertical-align:top;"><img src="cid:{content_id}" width="40" height="20" '
        'alt="Prüfbild – synthetisch" style="display:block;border:0;width:40px;height:20px;"></td>'
        '<td style="vertical-align:top;"><a href="https://example.invalid/pfad/mit/einem/sehr/langen/link'
        '?parameter=wert&amp;zweiter=wert2" style="color:#191917;text-decoration:underline;">'
        "Ein langer Link mit Umlauten: Prüfung</a></td></tr></table></div>"
    )


# A real 1x1 PNG, so content sniffing on the upload path files it as image/png.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
ATTACHMENT = "Synthetische Testdatei für die QP-Prüfung.\nZeile 2 = zwei.\n".encode()


def parts(content_id: str) -> list[dict]:
    """One embedded image and one ordinary attachment, as both builders take them."""
    return [
        {"data": PNG, "filename": "qp-probe.png", "content_type": "image/png",
         "disposition": "inline", "content_id": content_id},
        {"data": ATTACHMENT, "filename": "qp-probe-anhang.txt", "content_type": "text/plain",
         "disposition": "attachment", "content_id": None},
    ]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Reading bytes back the way Exchange Online did
# --------------------------------------------------------------------------

_HEX = frozenset(b"0123456789ABCDEFabcdef")


def exchange_qp_decode(encoded: bytes) -> bytes:
    """Decode quoted-printable as Exchange Online decoded the drafts it stored.

    Only "=" CRLF is a soft line break. An escape whose two characters are not hex
    digits keeps the "=" and swallows both, which is how "=" LF "e" turned into a
    single "=".
    """
    out = bytearray()
    i = 0
    while i < len(encoded):
        byte = encoded[i]
        if byte != 0x3D:
            out.append(byte)
            i += 1
            continue
        pair = encoded[i + 1 : i + 3]
        if pair == b"\r\n":
            pass
        elif len(pair) == 2 and pair[0] in _HEX and pair[1] in _HEX:
            out.append(int(pair, 16))
        else:
            out.append(0x3D)
        i += 3
    return bytes(out)


def as_exchange_reads_it(raw: bytes) -> dict:
    """The visible content of a message: its two bodies, and every file by name."""
    message = email.message_from_bytes(raw, policy=policy.default)
    bodies: dict[str, str] = {}
    files: dict[str, dict] = {}
    for part in message.walk():
        if part.is_multipart():
            continue
        encoding = (part.get("Content-Transfer-Encoding") or "7bit").strip().lower()
        if encoding == "quoted-printable":
            data = exchange_qp_decode(part.get_payload().encode("ascii"))
        else:
            data = part.get_payload(decode=True)
        if part.get_content_maintype() == "text" and part.get_content_disposition() is None:
            # Line endings, and the final one the encoder adds, belong to the
            # wire rather than to what anybody wrote.
            # errors="replace" is what a mail client shows for a split sequence.
            text = data.decode(part.get_content_charset() or "ascii", errors="replace").replace("\r\n", "\n")
            bodies.setdefault(part.get_content_subtype(), text.rstrip("\n"))
        else:
            files[part.get_filename()] = {
                "sha256": sha256(data),
                "disposition": part.get_content_disposition(),
                "content_id": part.get("Content-ID"),
            }
    return {"text": bodies.get("plain"), "html": bodies.get("html"), "files": files}


def assert_crlf_only(raw: bytes) -> None:
    rest = raw.replace(b"\r\n", b"")
    assert b"\n" not in rest, "a bare LF reached the wire"
    assert b"\r" not in rest, "a lone CR reached the wire"


def assert_round_trips(raw: bytes, content_id: str) -> None:
    assert_crlf_only(raw)
    seen = as_exchange_reads_it(raw)
    assert seen["text"] == BODY_TEXT.rstrip("\n")
    assert seen["html"] == html_for(content_id)
    assert f'src="cid:{content_id}"' in seen["html"]
    assert seen["files"]["qp-probe.png"] == {
        "sha256": sha256(PNG),
        "disposition": "inline",
        "content_id": f"<{content_id}>",
    }
    assert seen["files"]["qp-probe-anhang.txt"]["sha256"] == sha256(ATTACHMENT)
    assert seen["files"]["qp-probe-anhang.txt"]["disposition"] == "attachment"


def a_draft(content_id: str = CONTENT_ID) -> bytes:
    return build_draft(
        sender="owner@contoso.com",
        to_addresses=["owner@contoso.com"],
        cc_addresses=[],
        subject="Lange Zeilen – Prüfung",
        body_text=BODY_TEXT,
        body_html=html_for(content_id),
        headers={},
        attachments=parts(content_id),
    )


# --------------------------------------------------------------------------
# The draft builder
# --------------------------------------------------------------------------


def test_the_hazard_is_present_in_this_message():
    """Quoted-printable bodies, with a soft break inside the cid: URL.

    Without this the other tests could pass on a message that never exercised
    the failure.
    """
    message = email.message_from_bytes(a_draft(), policy=policy.default)
    html = next(part for part in message.walk() if part.get_content_type() == "text/html")
    text = next(part for part in message.walk() if part.get_content_type() == "text/plain")
    assert html["Content-Transfer-Encoding"] == "quoted-printable"
    assert text["Content-Transfer-Encoding"] == "quoted-printable"
    assert CONTENT_ID not in html.get_payload()
    assert "=3D" in text.get_payload()


def test_the_decoder_reproduces_what_exchange_made_of_bare_lf_soft_breaks():
    """The oracle has to fail the way Exchange failed, or it proves nothing."""
    seen = as_exchange_reads_it(a_draft().replace(b"\r\n", b"\n"))

    # Verbatim from the draft Exchange stored on 16 October 2026.
    assert "absichtlich läng=r als" in seen["text"]
    assert "Runde. De= nächste" in seen["text"]
    assert f"cid:{CONTENT_ID}" not in seen["html"]


def test_a_draft_survives_the_decoder_exchange_uses():
    assert_round_trips(a_draft(), CONTENT_ID)


def test_a_draft_without_attachments_is_crlf_as_well():
    raw = build_draft(
        sender="owner@contoso.com",
        to_addresses=["owner@contoso.com"],
        cc_addresses=[],
        subject="Ohne Anhang",
        body_text=BODY_TEXT,
        body_html=html_for(CONTENT_ID),
        headers={},
    )
    assert_crlf_only(raw)
    seen = as_exchange_reads_it(raw)
    assert seen["text"] == BODY_TEXT.rstrip("\n")
    assert seen["html"] == html_for(CONTENT_ID)


def test_headers_render_exactly_as_before_only_with_crlf():
    """Display names, encoded words and an RFC 2231 filename parse back to what was given.

    Serializing with a different policy (compat32, say) would still be CRLF and
    would still decode the bodies -- and would mangle these.
    """
    to = ['"Doe, John" <jd@example.com>', "Zoë Ärger <z@example.com>"] + [f"u{n}@example.com" for n in range(6)]
    cc = ["Jörg Müller-Lüdenscheidt <jm@example.com>"]
    filename = "Prüfbericht über die Qualität der Zeilenumbrüche im Oktober 2026 – endgültig.txt"
    raw = build_draft(
        sender="owner@contoso.com",
        to_addresses=to,
        cc_addresses=cc,
        subject="Grüße – eine Prüfung der Kopfzeilen, lang genug, um gefaltet zu werden",
        body_text=BODY_TEXT,
        body_html="<p>Grüße</p>",
        headers={},
        attachments=[{"data": ATTACHMENT, "filename": filename, "content_type": "text/plain",
                      "disposition": "attachment", "content_id": None}],
    )

    assert_crlf_only(raw)
    message = email.message_from_bytes(raw, policy=policy.default)
    assert getaddresses([str(message["To"])]) == getaddresses(to)
    assert getaddresses([str(message["Cc"])]) == getaddresses(cc)
    assert str(message["Subject"]) == "Grüße – eine Prüfung der Kopfzeilen, lang genug, um gefaltet zu werden"
    attachment = next(part for part in message.walk() if part.get_content_disposition() == "attachment")
    assert attachment.get_filename() == sanitize_filename(filename)  # spaces become underscores, umlauts stay
    assert "ü" in attachment.get_filename()
    assert attachment.get_payload(decode=True) == ATTACHMENT


# --------------------------------------------------------------------------
# Both draft branches, through the real tool, captured at the provider
# --------------------------------------------------------------------------


class _RecordingImap:
    """Stands in for the aioimaplib client: records each APPEND literal as given."""

    def __init__(self):
        self.literals: list[bytes] = []

    async def append(self, message_bytes, mailbox=None, flags=None):
        self.literals.append(message_bytes)
        return SimpleNamespace(result="OK", lines=[b"[APPENDUID 9 41] APPEND completed."])


@pytest.fixture
def imap_wire(monkeypatch):
    """The literal the real append_message hands to the IMAP library."""
    from src.services import write_service

    imap = _RecordingImap()

    async def fake_with_mailbox(db, config, operation):
        return await operation(SimpleNamespace(client=imap))

    async def fake_list_folders(client):
        return [{"name": "Drafts", "special_use": "drafts"}]

    monkeypatch.setattr(write_service, "_with_mailbox", fake_with_mailbox)
    monkeypatch.setattr(write_service, "list_folders", fake_list_folders)
    return imap


@pytest.fixture
def gmail_wire(monkeypatch):
    """The message the real GmailApiClient posts to Gmail's drafts endpoint."""
    from src.email.gmail_api_client import GmailApiClient
    from src.services import write_service

    posted: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/drafts")
        posted.append(base64.urlsafe_b64decode(json.loads(request.content)["message"]["raw"]))
        return httpx.Response(200, json={"id": "draft-1", "message": {"id": "m-1"}})

    async def fake_gmail(db, config, operation):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await operation(GmailApiClient(config, http_client=http, access_token="token"))

    monkeypatch.setattr(write_service, "_gmail", fake_gmail)
    return posted


async def _save_through_the_tool(app, account_id: int) -> str:
    image = await _slot(app, filename="qp-probe.png", payload=PNG)
    attached = await _slot(app, filename="qp-probe-anhang.txt", payload=ATTACHMENT)
    save = await _tool("save_draft")
    result = await save(
        account_id=account_id,
        to_addresses=["recipient@example.com"],
        subject="Lange Zeilen – Prüfung",
        body_text=BODY_TEXT,
        body_html=html_for(image["content_id"]),
        upload_ids=[attached["upload_id"]],
        inline_upload_ids=[image["upload_id"]],
    )
    assert result["success"] is True
    return image["content_id"]


@pytest.mark.asyncio
async def test_save_draft_files_bytes_that_survive_on_the_imap_branch(service, imap_wire):  # noqa: F811
    account = _account(service, _owner(service).id)

    content_id = await _save_through_the_tool(service, account.id)

    assert len(imap_wire.literals) == 1
    assert_round_trips(imap_wire.literals[0], content_id)


@pytest.mark.asyncio
async def test_save_draft_creates_bytes_that_survive_on_the_gmail_branch(service, gmail_wire):  # noqa: F811
    account = _account(service, _owner(service).id)
    _make_gmail(service, account.id)

    content_id = await _save_through_the_tool(service, account.id)

    assert len(gmail_wire) == 1
    assert_round_trips(gmail_wire[0], content_id)


# --------------------------------------------------------------------------
# The APPEND itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_sends_its_literal_with_crlf_whatever_it_was_given():
    imap = _RecordingImap()
    client = SimpleNamespace(client=imap)
    bare = b"Subject: x\nContent-Transfer-Encoding: quoted-printable\n\nl=C3=A4ng=\ner\r\nlone\rcr\n"

    uid = await append_message(client, "Drafts", bare, flags=["\\Draft"])

    assert uid == 41
    assert imap.literals[0] == (
        b"Subject: x\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\nl=C3=A4ng=\r\ner\r\nlone\r\ncr\r\n"
    )


def test_a_lone_cr_among_crlf_is_still_rewritten():
    """The shortcut for canonical input must not mistake one lone CR for canonical."""
    raw = b"Subject: x\r\n\r\na\r\nlone\rcr\r\n"

    assert with_crlf_line_endings(raw) == b"Subject: x\r\n\r\na\r\nlone\r\ncr\r\n"


@pytest.mark.asyncio
async def test_append_leaves_a_crlf_literal_alone():
    imap = _RecordingImap()
    raw = a_draft()

    await append_message(SimpleNamespace(client=imap), "Drafts", raw, flags=[])

    assert imap.literals == [raw]
    # Not even copied: a draft can be 35 MB, and this runs on the event loop.
    assert with_crlf_line_endings(raw) is raw


# --------------------------------------------------------------------------
# The send paths, and send == draft
# --------------------------------------------------------------------------


async def _send_through_graph() -> bytes:
    graph = Graph()
    result = await GraphMailSender(microsoft_account(), http_client=graph.client()).send_email(
        to_addresses=["owner@contoso.com"],
        subject="Lange Zeilen – Prüfung",
        body_text=BODY_TEXT,
        body_html=html_for(CONTENT_ID),
        attachments=parts(CONTENT_ID),
    )
    assert result["delivery_state"] == "sent"
    return base64.b64decode(graph.requests[-1].content)


@pytest.mark.asyncio
async def test_graph_receives_crlf_mime_that_round_trips(tokens):  # noqa: F811
    assert_round_trips(await _send_through_graph(), CONTENT_ID)


@pytest.mark.asyncio
async def test_smtp_delivers_crlf_mime_that_round_trips(sender, smtp_server):  # noqa: F811
    result = await sender.send_email(
        to_addresses=["recipient@example.com"],
        subject="Lange Zeilen – Prüfung",
        body_text=BODY_TEXT,
        body_html=html_for(CONTENT_ID),
        attachments=parts(CONTENT_ID),
    )

    assert result["delivery_state"] == "sent"
    assert_round_trips(smtp_server.delivered[-1], CONTENT_ID)


@pytest.mark.asyncio
async def test_a_draft_and_the_sent_message_carry_the_same_visible_content(tokens):  # noqa: F811
    assert as_exchange_reads_it(a_draft()) == as_exchange_reads_it(await _send_through_graph())
