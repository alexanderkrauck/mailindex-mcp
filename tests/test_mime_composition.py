"""MIME composition for outbound mail: real types, inline parts, attachments.

The assertions parse the produced bytes back with the stdlib parser and look at
the real tree, because a substring match cannot tell a header from a payload and
cannot see whether ``multipart/related`` actually wraps the body.
"""

import asyncio
import email
import email.message
from email import policy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.email.draft_builder import build_draft

PNG = b"\x89PNG\r\n\x1a\n\x00\xff\xfe binary payload \x00\x01\x02"
PDF = b"%PDF-1.4\x00\xff trailer"

HTML_ONE = '<p>hello <img src="cid:logo@example.com"></p>'
HTML_TWO = '<p><img src="cid:logo@example.com"><img src="cid:sig@example.com"></p>'


def parse(raw: bytes) -> email.message.EmailMessage:
    return email.message_from_bytes(raw, policy=policy.default)


def shape(msg):
    """The structural skeleton: content type plus children, nothing else."""
    if msg.is_multipart():
        return (msg.get_content_type(), [shape(part) for part in msg.iter_parts()])
    return msg.get_content_type()


def describe(msg):
    """What a mail client renders from, normalised for cross-builder comparison."""
    if msg.is_multipart():
        return {"type": msg.get_content_type(), "parts": [describe(part) for part in msg.iter_parts()]}
    payload = msg.get_payload(decode=True)
    if msg.get_content_maintype() == "text":
        # The two builders pick different (equally valid) transfer encodings for
        # text, and one appends a trailing newline; neither is visible to a reader.
        payload = payload.rstrip(b"\r\n")
    return {
        "type": msg.get_content_type(),
        "disposition": msg.get_content_disposition(),
        "filename": msg.get_filename(),
        "cid": msg.get("Content-ID"),
        "payload": payload,
    }


def leaves(msg):
    if msg.is_multipart():
        for part in msg.iter_parts():
            yield from leaves(part)
    else:
        yield msg


def compose_sent(body_text=None, body_html=None, attachments=None):
    """Drive the real send path and return (parsed message, wire bytes)."""
    from src.email.smtp_sender import EmailSender

    config = SimpleNamespace(
        id=1,
        name="Test",
        account_name="sender@example.com",
        username="sender@example.com",
    )
    sender = EmailSender(config)
    sender._server = MagicMock()
    sender._server.send_message.return_value = {}

    result = asyncio.run(
        sender.send_email(
            to_addresses=["recipient@example.com"],
            subject="Subject",
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
        )
    )
    assert result["success"] is True, result
    raw = sender._server.send_message.call_args.args[0].as_bytes()
    return parse(raw), raw


def compose_draft(body_text=None, body_html=None, attachments=None):
    raw = build_draft(
        sender="sender@example.com",
        to_addresses=["recipient@example.com"],
        cc_addresses=[],
        subject="Subject",
        body_text=body_text or "",
        body_html=body_html or "",
        headers={},
        attachments=attachments,
    )
    return parse(raw), raw


BUILDERS = {"sent": compose_sent, "draft": compose_draft}


@pytest.fixture(params=sorted(BUILDERS))
def compose(request):
    return BUILDERS[request.param]


# --------------------------------------------------------------------------
# Nothing changes for the messages that are already being sent
# --------------------------------------------------------------------------


def test_send_without_attachments_keeps_todays_tree():
    message, _ = compose_sent(body_text="Body", body_html="<p>Body</p>")

    assert shape(message) == ("multipart/mixed", [("multipart/alternative", ["text/plain", "text/html"])])


def test_draft_without_attachments_keeps_todays_tree():
    message, _ = compose_draft(body_text="Body", body_html="<p>Body</p>")

    assert shape(message) == ("multipart/alternative", ["text/plain", "text/html"])


def test_legacy_attachment_dict_keeps_todays_behaviour(compose):
    message, _ = compose(
        body_text="Body",
        body_html="<p>Body</p>",
        attachments=[{"data": PDF, "filename": "report.pdf"}],
    )

    assert shape(message) == (
        "multipart/mixed",
        [("multipart/alternative", ["text/plain", "text/html"]), "application/octet-stream"],
    )
    part = list(leaves(message))[-1]
    assert part.get_content_disposition() == "attachment"
    assert part.get_filename() == "report.pdf"
    assert part.get("Content-ID") is None
    assert part.get_payload(decode=True) == PDF


def test_root_carries_exactly_one_mime_version(compose):
    _, raw = compose(
        body_text="Body",
        body_html=HTML_ONE,
        attachments=[
            {"data": PNG, "filename": "logo.png", "content_type": "image/png", "disposition": "inline", "content_id": "logo@example.com"},
        ],
    )
    root_headers = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]

    assert root_headers.lower().count(b"mime-version:") == 1


# --------------------------------------------------------------------------
# Real content types and dispositions
# --------------------------------------------------------------------------


def test_plain_attachment_gets_its_real_content_type(compose):
    message, _ = compose(
        body_text="Body",
        body_html="<p>Body</p>",
        attachments=[{"data": PDF, "filename": "report.pdf", "content_type": "application/pdf"}],
    )

    assert shape(message) == (
        "multipart/mixed",
        [("multipart/alternative", ["text/plain", "text/html"]), "application/pdf"],
    )
    part = list(leaves(message))[-1]
    assert part.get_content_disposition() == "attachment"
    assert part.get_filename() == "report.pdf"
    assert part.get_payload(decode=True) == PDF


def test_inline_image_is_wrapped_in_related_and_mixed_collapses(compose):
    message, _ = compose(
        body_text="Body",
        body_html=HTML_ONE,
        attachments=[
            {
                "data": PNG,
                "filename": "logo.png",
                "content_type": "image/png",
                "disposition": "inline",
                "content_id": "logo@example.com",
            }
        ],
    )

    assert shape(message) == (
        "multipart/related",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png"],
    )
    image = list(leaves(message))[-1]
    assert image.get("Content-ID") == "<logo@example.com>"
    assert image.get_content_disposition() == "inline"
    assert image.get_filename() == "logo.png"
    html = [part for part in leaves(message) if part.get_content_type() == "text/html"][0]
    assert "cid:logo@example.com" in html.get_content()


def test_inline_and_plain_attachment_produce_the_full_tree(compose):
    message, _ = compose(
        body_text="Body",
        body_html=HTML_ONE,
        attachments=[
            {
                "data": PNG,
                "filename": "logo.png",
                "content_type": "image/png",
                "disposition": "inline",
                "content_id": "logo@example.com",
            },
            {"data": PDF, "filename": "report.pdf", "content_type": "application/pdf"},
        ],
    )

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


def test_multiple_inline_parts_keep_distinct_content_ids(compose):
    message, _ = compose(
        body_text="Body",
        body_html=HTML_TWO,
        attachments=[
            {"data": PNG, "filename": "logo.png", "content_type": "image/png", "disposition": "inline", "content_id": "logo@example.com"},
            {"data": PDF, "filename": "sig.gif", "content_type": "image/gif", "disposition": "inline", "content_id": "sig@example.com"},
        ],
    )

    assert shape(message) == (
        "multipart/related",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png", "image/gif"],
    )
    ids = [part.get("Content-ID") for part in leaves(message) if part.get("Content-ID")]
    assert ids == ["<logo@example.com>", "<sig@example.com>"]
    assert all(part.get_content_disposition() == "inline" for part in list(leaves(message))[2:])


def test_unreferenced_inline_part_is_delivered_as_an_attachment(compose):
    message, _ = compose(
        body_text="Body",
        body_html="<p>no image here</p>",
        attachments=[
            {"data": PNG, "filename": "logo.png", "content_type": "image/png", "disposition": "inline", "content_id": "logo@example.com"},
        ],
    )

    assert shape(message) == (
        "multipart/mixed",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png"],
    )
    image = list(leaves(message))[-1]
    assert image.get_content_disposition() == "attachment"
    assert image.get_payload(decode=True) == PNG


def test_inline_part_without_any_html_body_is_delivered_as_an_attachment(compose):
    message, _ = compose(
        body_text="Body only",
        body_html=None,
        attachments=[
            {"data": PNG, "filename": "logo.png", "content_type": "image/png", "disposition": "inline", "content_id": "logo@example.com"},
        ],
    )

    assert shape(message) in (
        ("multipart/mixed", [("multipart/alternative", ["text/plain"]), "image/png"]),
        ("multipart/mixed", ["text/plain", "image/png"]),
    )
    image = list(leaves(message))[-1]
    assert image.get_content_disposition() == "attachment"
    assert image.get_payload(decode=True) == PNG


CID = "logo@example.com"


@pytest.mark.parametrize(
    "body_html",
    [
        pytest.param(f'<img src="cid:{CID}">', id="double_quoted"),
        pytest.param(f"<img src='cid:{CID}'>", id="single_quoted"),
        pytest.param(f"<img src=cid:{CID}>", id="unquoted"),
        pytest.param(f"<img src=&quot;cid:{CID}&quot;>", id="html_escaped_quote"),
        pytest.param(f'<img src="cid:{CID}?w=10">', id="trailing_query"),
        pytest.param(f'<img src="cid:{CID}#top">', id="trailing_fragment"),
        pytest.param(f'<div style="background:url(cid:{CID})">x</div>', id="css_url"),
        pytest.param(f'<td background="cid:{CID}">x</td>', id="background_attribute"),
        pytest.param(f'<img SRC="CID:{CID}">', id="uppercase_scheme"),
    ],
)
def test_every_standard_cid_reference_form_embeds_the_part(compose, body_html):
    """An author writes the reference; the server must recognise all of them.

    Both HTML quoting forms are standard and an AI client picks whichever it
    likes, so a reference the composer fails to see is an image that silently
    vanishes from the body -- demoted to an ordinary attachment, with the send
    still reporting success and nothing anywhere reporting a warning.
    """
    message, _ = compose(
        body_text="Body",
        body_html=body_html,
        attachments=[
            {
                "data": PNG,
                "filename": "logo.png",
                "content_type": "image/png",
                "disposition": "inline",
                "content_id": CID,
            }
        ],
    )

    assert shape(message) == (
        "multipart/related",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png"],
    ), f"the reference in {body_html!r} was not recognised, so the image was demoted"
    image = list(leaves(message))[-1]
    assert image.get("Content-ID") == f"<{CID}>"
    assert image.get_content_disposition() == "inline"


def test_a_cid_reference_does_not_claim_a_longer_id():
    """The terminator's real job, which the quoting fix must not give up.

    ``cid:logo@x`` appearing in the HTML is a reference to ``logo@x`` and to
    nothing else: a part whose id merely starts the same way is a different
    part, and claiming the reference for it would embed the wrong file.
    """
    from src.email.draft_builder import _is_referenced

    assert _is_referenced('<img src="cid:logo@xyz.example">', "logo@x") is False
    assert _is_referenced('<img src="cid:logotype@example.com">', "logo") is False
    assert _is_referenced('<img src="cid:logo@example.com.au">', "logo@example.com") is False
    # ...while the exact id still resolves, in either quoting form.
    assert _is_referenced('<img src="cid:logo@example.com">', "logo@example.com") is True
    assert _is_referenced("<img src='cid:logo@example.com'>", "logo@example.com") is True


@pytest.mark.parametrize(
    "after",
    [
        pytest.param("", id="end_of_string"),
        pytest.param(" and so on", id="space"),
        pytest.param("\nnext line", id="newline"),
        pytest.param("</p>", id="close_tag"),
        pytest.param(")", id="close_paren"),
        pytest.param(", and so on", id="comma"),
        pytest.param("; and so on", id="semicolon"),
        pytest.param("! Look at it", id="exclamation"),
        pytest.param("]", id="close_bracket"),
        pytest.param("}", id="close_brace"),
        pytest.param(": here it is", id="colon"),
    ],
)
def test_a_sentence_dot_after_a_reference_never_hides_it(after):
    """A '.' ending a sentence must not swallow the id it follows.

    '.' is judged by what comes after it, and the set of characters that could
    end the id there was drawn too narrowly: only whitespace, quotes, '<', '>'
    and ')' terminated. A reference written into prose as ``cid:ID.,`` or
    ``cid:ID.;`` -- an ordinary sentence dot followed by ordinary punctuation --
    therefore looked like a host label continuing, so the part was silently
    demoted to an ordinary attachment and the image vanished from the body with
    the send still reporting success.

    None of the characters added here can follow a '.' inside a real dot-atom
    host label, so widening the set cannot cost a true continuation -- which
    ``test_a_cid_reference_does_not_claim_a_longer_id`` holds.
    """
    from src.email.draft_builder import _is_referenced

    body_html = f"<p>See the chart at cid:{CID}.{after}</p>"

    assert _is_referenced(body_html, CID) is True, (
        f"a trailing sentence dot followed by {after[:1]!r} hid the reference, "
        "so the image was silently demoted"
    )


def test_a_trailing_dot_still_does_not_claim_a_longer_host():
    """The other half: a '.' that really does continue into another label."""
    from src.email.draft_builder import _is_referenced

    assert _is_referenced('<img src="cid:logo@example.com.au">', "logo@example.com") is False
    assert _is_referenced('<img src="cid:logo@example.com.au,">', "logo@example.com") is False
    assert _is_referenced('<img src="cid:logo@example.com.co.uk">', "logo@example.com") is False


def test_related_declares_its_root_part_per_rfc_2387(compose):
    """``type=`` is what tells a client which part the cid: URLs hang off.

    RFC 2387 requires it, Apple Mail and Outlook both emit
    ``type="multipart/alternative"``, and matching real-client output is the
    stated goal. Modern clients tolerate its absence; emitting it costs
    nothing and removes a difference.
    """
    message, _ = compose(
        body_text="Body",
        body_html=HTML_ONE,
        attachments=[
            {
                "data": PNG,
                "filename": "logo.png",
                "content_type": "image/png",
                "disposition": "inline",
                "content_id": "logo@example.com",
            }
        ],
    )

    assert message.get_content_type() == "multipart/related"
    root_part = next(message.iter_parts())
    # The parameter has to name the part that is actually first, or it is worse
    # than useless: it points a client at a structure that is not there.
    assert message.get_param("type") == root_part.get_content_type()
    assert message.get_param("type") == "multipart/alternative"


def test_duplicate_content_ids_demote_the_later_part(compose):
    message, _ = compose(
        body_text="Body",
        body_html=HTML_ONE,
        attachments=[
            {"data": PNG, "filename": "one.png", "content_type": "image/png", "disposition": "inline", "content_id": "logo@example.com"},
            {"data": PDF, "filename": "two.png", "content_type": "image/png", "disposition": "inline", "content_id": "logo@example.com"},
        ],
    )

    assert shape(message) == (
        "multipart/mixed",
        [
            ("multipart/related", [("multipart/alternative", ["text/plain", "text/html"]), "image/png"]),
            "image/png",
        ],
    )
    inline, demoted = list(leaves(message))[2:]
    assert inline.get_content_disposition() == "inline"
    assert demoted.get_content_disposition() == "attachment"
    assert demoted.get_payload(decode=True) == PDF


# --------------------------------------------------------------------------
# Hostile metadata never reaches a header
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "image/png\r\nBcc: evil@example.net",
        "image/png\nBcc: evil@example.net",
        "notatype",
        "",
        "a/b/c",
        "image/png; name=evil",
        "multipart/mixed",
        "  image/png  ",
        b"image/png",
        42,
    ],
)
def test_malformed_content_type_falls_back_to_octet_stream(compose, content_type):
    message, raw = compose(
        body_text="Body",
        body_html="<p>Body</p>",
        attachments=[{"data": PDF, "filename": "report.pdf", "content_type": content_type}],
    )

    part = list(leaves(message))[-1]
    assert part.get_content_type() == "application/octet-stream"
    assert part.get_params() == [("application/octet-stream", "")]
    assert part.get_content_disposition() == "attachment"
    assert part.get_filename() == "report.pdf"
    assert part.get_payload(decode=True) == PDF
    assert {name.lower() for name, _ in part.items()} <= {
        "content-type",
        "content-transfer-encoding",
        "content-disposition",
        "mime-version",
    }
    assert message.get_all("Bcc") is None
    assert b"Bcc" not in raw
    assert b"evil@example.net" not in raw


def test_attachment_without_content_type_key_falls_back_to_octet_stream(compose):
    message, _ = compose(
        body_text="Body",
        body_html="<p>Body</p>",
        attachments=[{"data": PDF, "filename": "report.pdf"}],
    )

    assert list(leaves(message))[-1].get_content_type() == "application/octet-stream"


@pytest.mark.parametrize(
    "content_id",
    [
        "logo@example.com\r\nBcc: evil@example.net",
        "logo@example.com\nBcc: evil@example.net",
        "<already@bracketed.com>",
        "has space@example.com",
        "a" * 400 + "@example.com",
        "",
        None,
        'quote"@example.com',
        "semi;colon@example.com",
        b"logo@example.com",
    ],
)
def test_hostile_content_id_is_never_injected(compose, content_id):
    message, raw = compose(
        body_text="Body",
        body_html=HTML_ONE,
        attachments=[
            {
                "data": PNG,
                "filename": "logo.png",
                "content_type": "image/png",
                "disposition": "inline",
                "content_id": content_id,
            }
        ],
    )

    # An id that cannot be trusted cannot address a related part, so the file is
    # delivered as an ordinary attachment instead of vanishing.
    assert shape(message) == (
        "multipart/mixed",
        [("multipart/alternative", ["text/plain", "text/html"]), "image/png"],
    )
    part = list(leaves(message))[-1]
    assert part.get_content_disposition() == "attachment"
    assert part.get_payload(decode=True) == PNG
    assert part.get("Content-ID") is None
    assert message.get_all("Bcc") is None
    assert b"Bcc" not in raw
    assert b"evil@example.net" not in raw


def test_filename_with_crlf_is_sanitised(compose):
    message, raw = compose(
        body_text="Body",
        body_html="<p>Body</p>",
        attachments=[{"data": PDF, "filename": "report\r\nBcc: evil@example.net.pdf", "content_type": "application/pdf"}],
    )

    part = list(leaves(message))[-1]
    filename = part.get_filename()
    assert "\r" not in filename and "\n" not in filename
    assert message.get_all("Bcc") is None
    assert b"Bcc:" not in raw
    assert b"evil@example.net" not in raw


def test_hostile_disposition_is_treated_as_an_attachment(compose):
    message, _ = compose(
        body_text="Body",
        body_html=HTML_ONE,
        attachments=[
            {
                "data": PNG,
                "filename": "logo.png",
                "content_type": "image/png",
                "disposition": "inline; filename=evil\r\nBcc: evil@example.net",
                "content_id": "logo@example.com",
            }
        ],
    )

    part = list(leaves(message))[-1]
    assert part.get_content_disposition() == "attachment"
    assert message.get_all("Bcc") is None


# --------------------------------------------------------------------------
# Payload fidelity and cross-builder equivalence
# --------------------------------------------------------------------------


def test_binary_payload_round_trips_byte_identically(compose):
    blob = bytes(range(256)) * 4

    message, _ = compose(
        body_text="Body",
        body_html="<p>Body</p>",
        attachments=[{"data": blob, "filename": "blob.bin", "content_type": "application/octet-stream"}],
    )

    part = list(leaves(message))[-1]
    assert part.get("Content-Transfer-Encoding") == "base64"
    assert part.get_payload(decode=True) == blob


SCENARIOS = {
    "plain": [{"data": PDF, "filename": "report.pdf", "content_type": "application/pdf"}],
    "inline": [
        {"data": PNG, "filename": "logo.png", "content_type": "image/png", "disposition": "inline", "content_id": "logo@example.com"},
    ],
    "inline_and_plain": [
        {"data": PNG, "filename": "logo.png", "content_type": "image/png", "disposition": "inline", "content_id": "logo@example.com"},
        {"data": PDF, "filename": "report.pdf", "content_type": "application/pdf"},
    ],
    "unreferenced_inline": [
        {"data": PNG, "filename": "other.png", "content_type": "image/png", "disposition": "inline", "content_id": "nobody@example.com"},
    ],
    "legacy": [{"data": PDF, "filename": "report.pdf"}],
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize(
    "body_text",
    [
        pytest.param("Body", id="text_and_html"),
        # The natural shape of a message written around an embedded image: the
        # client supplies the HTML it composed and no plain-text alternative.
        # Every equivalence test here used to pass body_text='Body', so the
        # builders were never compared on it -- and they disagreed, the draft
        # carrying an extra empty text/plain leaf the sent copy does not have.
        pytest.param("", id="html_only"),
    ],
)
def test_both_builders_produce_equivalent_structure(scenario, body_text):
    attachments = SCENARIOS[scenario]

    sent, _ = compose_sent(body_text=body_text, body_html=HTML_ONE, attachments=attachments)
    drafted, _ = compose_draft(body_text=body_text, body_html=HTML_ONE, attachments=attachments)

    assert describe(sent) == describe(drafted)


def test_an_html_only_draft_carries_no_empty_text_plain_leaf():
    """The divergence stated as the thing a reader would actually notice.

    An empty text/plain first in a multipart/alternative is what some clients
    render *by preference*, so the draft of an html-only message could show
    blank where the sent copy shows the message.
    """
    sent, _ = compose_sent(body_text="", body_html=HTML_ONE, attachments=SCENARIOS["inline"])
    drafted, _ = compose_draft(body_text="", body_html=HTML_ONE, attachments=SCENARIOS["inline"])

    assert [part.get_content_type() for part in leaves(drafted)] == ["text/html", "image/png"]
    assert shape(drafted) == shape(sent)


def test_text_only_bodies_differ_only_by_the_senders_legacy_wrapper():
    """Pins one of the two structural send/draft divergences, both pre-existing.

    CLASS B, the one this test exercises: with a falsy ``body_html`` the sender
    wraps the single ``text/plain`` in a ``multipart/alternative`` and the draft
    does not.

    CLASS A, pinned elsewhere by ``test_send_without_attachments_keeps_todays_tree``
    and ``test_draft_without_attachments_keeps_todays_tree``: with no
    attachments at all the sender emits a lone ``multipart/mixed`` wrapper
    around the alternative that the draft omits entirely
    (``mixed[alternative[plain, html]]`` against a bare
    ``alternative[plain, html]``).

    Both are structural only. Across the full body x attachment matrix the
    *leaves* -- what a reader actually sees -- are identical in every cell, so
    there is no leaf-level divergence anywhere. Both render identically and
    changing either would alter the tree of messages that already ship, so they
    are frozen here rather than fixed.
    """
    attachments = [
        {"data": PNG, "filename": "logo.png", "content_type": "image/png", "disposition": "inline", "content_id": "logo@example.com"},
    ]

    sent, _ = compose_sent(body_text="Body", body_html=None, attachments=attachments)
    drafted, _ = compose_draft(body_text="Body", body_html=None, attachments=attachments)

    assert shape(sent) == ("multipart/mixed", [("multipart/alternative", ["text/plain"]), "image/png"])
    assert shape(drafted) == ("multipart/mixed", ["text/plain", "image/png"])
    # The leaves - what a reader actually sees - agree part for part.
    assert [describe(part) for part in leaves(sent)] == [describe(part) for part in leaves(drafted)]


# --------------------------------------------------------------------------
# Failure contract
# --------------------------------------------------------------------------


def test_unbuildable_attachment_still_fails_the_send():
    from src.email.smtp_sender import EmailSender

    config = SimpleNamespace(id=1, name="Test", account_name="sender@example.com", username="sender@example.com")
    sender = EmailSender(config)
    sender._server = MagicMock()

    result = asyncio.run(
        sender.send_email(
            to_addresses=["recipient@example.com"],
            subject="Subject",
            body_text="Body",
            attachments=[{"filename": "report.pdf"}],  # no data
        )
    )

    assert result == {
        "success": False,
        "message": "Failed to construct an outbound attachment",
        "delivery_state": "failed",
    }
    sender._server.send_message.assert_not_called()
