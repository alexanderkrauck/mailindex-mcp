"""Build the RFC 5322 bytes of a draft, and the MIME plan both builders share.

The planning half lives here rather than in a module of its own so the draft
and the sent copy cannot drift: a message composed once must render the same
way whether the user saved it or sent it. ``smtp_sender`` imports the plan and
renders it with the ``email.mime`` classes it already uses; this module renders
it with ``EmailMessage``. The *shapes* are what must agree, not the machinery.
"""

import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from src.email import sanitize_filename

# A media type is two tokens and nothing else. Deliberately not stripped and
# deliberately ASCII: whitespace, a parameter, a second slash or a bare newline
# all fail, so nothing a caller supplies can become a second header or extend
# the Content-Type with a parameter of its choosing.
_CONTENT_TYPE_TOKEN = re.compile(r"[\w.+-]{1,64}/[\w.+-]{1,64}", re.ASCII)

# multipart/* and message/* describe a structure rather than a payload. Labelling
# an opaque blob as either produces a part whose declared shape does not match
# its bytes, so they fall back like any other unusable type.
_STRUCTURAL_MAINTYPES = frozenset({"multipart", "message"})

DEFAULT_MAINTYPE = "application"
DEFAULT_SUBTYPE = "octet-stream"

# An addr-spec-shaped token, no angle brackets (we add those), no spaces, no
# quotes, no semicolons, nothing that could close the header and start another.
_CONTENT_ID_TOKEN = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+(?:@[A-Za-z0-9][A-Za-z0-9.-]*)?", re.ASCII)
_MAX_CONTENT_ID_LENGTH = 190

# Characters an id could *continue* with, used to terminate a cid: match. A
# reference followed by one of these names some longer id, so it is not a
# reference to this part: ``cid:logo@xyz`` must not claim the part whose id is
# ``logo@x``. Stopping that is this class's entire job.
#
# Deliberately narrower than _CONTENT_ID_TOKEN above, and the difference is the
# point. An id may legally contain ' & # ? -- but in HTML those characters are
# overwhelmingly how a URL *ends*: the single-quoted attribute form, an
# HTML-escaped entity, a query, a fragment. Treating them as continuations made
# ``<img src='cid:ID'>`` -- one of the two standard quoting forms, and the one
# an author picks at random -- fail the test, and the part was then silently
# demoted to an ordinary attachment: the image disappeared from the body while
# the send still reported success and nothing anywhere reported a warning. The
# two failure modes are not symmetric. A false negative loses the image with no
# diagnostic; a false positive merely embeds a part nothing points at, which is
# the behaviour an unreferenced inline part already gets on purpose.
#
# THAT NARROWING IS ONLY SAFE BECAUSE OF THE ID ALPHABET IT RUNS AGAINST.
# Excluding ' & # ? reopens a prefix collision in principle -- with them out of
# the class, ``cid:logo?x@example.com`` in the HTML reads as a reference to the
# part whose id is ``logo`` -- and today nothing can reach it: every id here is
# server-derived by ``upload_service.content_id_for`` as
# ``token_urlsafe(32) + '@mailindex.invalid'``, whose alphabet is [-0-9A-Z_a-z]
# only, and no MCP tool exposes a caller-facing content_id field. If
# ``content_id_for`` ever admits a character from this excluded set, or a
# caller-supplied id ever reaches ``plan_outbound_parts``, re-derive this class
# rather than assuming the collision is still unreachable.
_CID_CONTINUATION = r"A-Za-z0-9!$%*+/=^_`{|}~@-"
# '.' is the ambiguous one, so it is judged by what follows it rather than by
# itself: ``cid:logo@example.com.au`` continues into another host, while
# ``cid:logo@example.com.\"`` is the id followed by sentence punctuation and a
# delimiter. A '.' before a delimiter or the end of the string terminates.
#
# The delimiter set has to be read against prose, not just against markup. A
# reference written into a sentence ends ``cid:ID.,`` or ``cid:ID.;`` just as
# readily as ``cid:ID. `` -- an ordinary sentence dot followed by ordinary
# punctuation -- and treating those as continuations silently demoted the part,
# the same no-diagnostic data loss the quoting fix above addresses. None of
# , ; ! ] } : can follow a '.' inside a real dot-atom host label, so admitting
# them as terminators cannot cost a genuine continuation.
_CID_DOT_CONTINUES = r"\.[^\s\"'<>)\,;!\]}:]"


@dataclass(frozen=True)
class OutboundPart:
    """One attachment, already validated: every field is safe in a header."""

    data: bytes
    filename: str
    maintype: str
    subtype: str
    content_id: str | None = None


def parse_content_type(value: object) -> tuple[str, str]:
    """Split a caller-supplied media type, or fall back to the opaque default."""
    if not isinstance(value, str):
        return DEFAULT_MAINTYPE, DEFAULT_SUBTYPE
    if not _CONTENT_TYPE_TOKEN.fullmatch(value):
        return DEFAULT_MAINTYPE, DEFAULT_SUBTYPE
    maintype, subtype = value.split("/", 1)
    if maintype.lower() in _STRUCTURAL_MAINTYPES:
        return DEFAULT_MAINTYPE, DEFAULT_SUBTYPE
    return maintype.lower(), subtype.lower()


def parse_content_id(value: object) -> str | None:
    """Return a bare, header-safe Content-ID, or None if it cannot be trusted."""
    if not isinstance(value, str):
        return None
    if not value or len(value) > _MAX_CONTENT_ID_LENGTH:
        return None
    if not _CONTENT_ID_TOKEN.fullmatch(value):
        return None
    return value


def _is_referenced(body_html: str | None, content_id: str) -> bool:
    """True when the HTML actually points at this part with a cid: URL.

    The scheme is matched case-insensitively because authors write it both ways;
    the id itself is matched exactly, since that is what the header will carry.
    The terminator stops ``cid:logo@x`` from claiming a reference to ``logo@xyz``
    without also rejecting the ways a URL ordinarily ends in HTML -- see
    ``_CID_CONTINUATION``.
    """
    if not body_html:
        return False
    pattern = (
        r"(?i:cid:)"
        + re.escape(content_id)
        + f"(?![{_CID_CONTINUATION}])"
        + f"(?!{_CID_DOT_CONTINUES})"
    )
    return re.search(pattern, body_html) is not None


def plan_outbound_parts(
    attachments: list[dict] | None,
    body_html: str | None,
) -> tuple[list[OutboundPart], list[OutboundPart]]:
    """Split attachments into (inline, ordinary), validating every header value.

    A part is only inline when it asked to be, carries a usable Content-ID no
    other part has claimed, and the HTML actually references it. Everything that
    fails one of those is still delivered — as an ordinary attachment, because a
    related part nothing points at is invisible in most clients, and dropping a
    file the user attached is worse than putting it at the bottom.
    """
    inline_parts: list[OutboundPart] = []
    attachment_parts: list[OutboundPart] = []
    claimed_ids: set[str] = set()

    for attachment in attachments or []:
        maintype, subtype = parse_content_type(attachment.get("content_type"))
        part = OutboundPart(
            data=attachment["data"],
            filename=sanitize_filename(attachment.get("filename") or "attachment"),
            maintype=maintype,
            subtype=subtype,
        )

        disposition = attachment.get("disposition")
        wants_inline = isinstance(disposition, str) and disposition.strip().lower() == "inline"
        content_id = parse_content_id(attachment.get("content_id")) if wants_inline else None

        if content_id and content_id not in claimed_ids and _is_referenced(body_html, content_id):
            claimed_ids.add(content_id)
            # dataclasses.replace would do, but the explicit call keeps the
            # frozen fields visible at the one place inlining is decided.
            inline_parts.append(
                OutboundPart(
                    data=part.data,
                    filename=part.filename,
                    maintype=part.maintype,
                    subtype=part.subtype,
                    content_id=content_id,
                )
            )
        else:
            attachment_parts.append(part)

    return inline_parts, attachment_parts


def _render_part(part: OutboundPart, *, inline: bool) -> EmailMessage:
    """One leaf part, with the headers a mail client needs to place it."""
    message = EmailMessage()
    if inline:
        # Both headers, on purpose: Outlook resolves cid: through Content-ID
        # while some clients only look at the disposition.
        message.set_content(
            part.data,
            maintype=part.maintype,
            subtype=part.subtype,
            disposition="inline",
            filename=part.filename,
            cid=f"<{part.content_id}>",
        )
    else:
        message.set_content(
            part.data,
            maintype=part.maintype,
            subtype=part.subtype,
            disposition="attachment",
            filename=part.filename,
        )
    return message


def _fill_body(message: EmailMessage, body_text: str, body_html: str) -> None:
    """Put the body into ``message``, in the shape the sender also produces.

    The sender builds a ``multipart/alternative`` and attaches only the bodies
    it was actually given, so an html-only message has one ``text/html`` leaf
    and no ``text/plain``. The draft built its body by setting the text first
    and adding the HTML as an alternative, which for a falsy ``body_text``
    emitted an *empty* ``text/plain`` the sent copy never had -- and an empty
    text part placed first in an alternative is what some clients render by
    preference, so the saved copy of an html-only message could show blank
    where the sent one shows the message. Both builders now agree on all four
    combinations, which is what the equivalence tests assert.
    """
    if body_html and not body_text:
        message.set_content(body_html, subtype="html")
        # An alternative with one branch, exactly as the sender emits: the
        # container is what a client walks to find its preferred rendering, and
        # dropping it would be a second, different divergence.
        message.make_alternative()
        return
    message.set_content(body_text or "")
    if body_html:
        message.add_alternative(body_html, subtype="html")


def build_draft(
    *,
    sender: str,
    to_addresses: list[str],
    cc_addresses: list[str],
    subject: str,
    body_text: str,
    body_html: str,
    headers: dict[str, str],
    attachments: list[dict] | None = None,
) -> bytes:
    """A draft is an ordinary message; only the folder and the \\Draft flag differ."""
    message = EmailMessage()
    message["From"] = sender
    if to_addresses:
        message["To"] = ", ".join(to_addresses)
    if cc_addresses:
        message["Cc"] = ", ".join(cc_addresses)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    # Give it an identity now, so the draft the sync pass indexes back is
    # recognisable as this one rather than arriving with a synthetic id.
    message["Message-ID"] = make_msgid()
    for name, value in headers.items():
        message[name] = value

    inline_parts, attachment_parts = plan_outbound_parts(attachments, body_html)

    if not inline_parts:
        # Nothing references the body, so nothing has to wrap it: this is the
        # shape drafts have always had, down to the absent multipart/related.
        _fill_body(message, body_text, body_html)
        for part in attachment_parts:
            message.add_attachment(
                part.data,
                maintype=part.maintype,
                subtype=part.subtype,
                filename=part.filename,
            )
        return message.as_bytes()

    # The body and the parts it points at belong together inside one
    # multipart/related, or a client has no reason to resolve the cid: URLs.
    body = EmailMessage()
    _fill_body(body, body_text, body_html)

    message.make_related()
    message.attach(body)
    # RFC 2387's required parameter, naming the root part a client should
    # render and resolve the cid: URLs against. Read off the body rather than
    # hardcoded, so it cannot come to describe a structure that is not there,
    # and set here rather than passed to make_related() -- which takes no
    # parameters. The sender emits the same thing.
    message.set_param("type", body.get_content_type())
    for part in inline_parts:
        message.attach(_render_part(part, inline=True))

    if attachment_parts:
        # Only now is a mixed level worth its boundary: it exists to carry the
        # parts that are not part of the body.
        message.make_mixed()
        for part in attachment_parts:
            message.attach(_render_part(part, inline=False))

    # make_related/make_mixed build the container by hand and do not add this,
    # unlike set_content; without it the top level is not formally MIME.
    if "MIME-Version" not in message:
        message["MIME-Version"] = "1.0"
    return message.as_bytes()
