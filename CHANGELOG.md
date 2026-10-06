# Changelog

Notable changes. Dates are release dates; the git history is finer grained.

## v0.3.0 — 2026-10-06

### Microsoft 365 / Exchange Online

- Connect Microsoft 365 mailboxes with Microsoft's own sign-in, the way Gmail is
  connected. `begin_microsoft_connection` returns a signed link, the owner picks
  the account and approves, and the page that follows says whether the mailbox is
  usable. Exchange Online accepts no password or app password over IMAP, so there
  is no password route for these mailboxes. Setup is one Entra app registration;
  the README walks through it.
- Read, search, flag, move, delete, draft and manage folders over IMAP with an
  OAuth token (XOAUTH2). Nothing about the sync machinery changed: a Microsoft
  account is an IMAP account whose password is a short-lived token.
- Send through Microsoft Graph instead of SMTP AUTH. Exchange Online ships with
  SMTP AUTH switched off for the whole organisation, and enabling it is a
  decision for that organisation's administrator, which a connector should not
  have to ask for. Graph needs only the user's own consent. The message is the
  MIME the SMTP sender builds -- composition now lives in one function both
  transports share, so the two cannot drift -- and a send is classified the way
  SMTP's is: a refusal or a failure to connect
  is a definite non-send, anything after the request left is unknown and is never
  retried, because a second request could deliver the message twice.
- Store only the refresh token (and tenant) per mailbox. The app registration's
  secret stays in the server's environment, so it can be renewed without
  connecting any mailbox again, and a token Microsoft has revoked is reported as
  an authentication failure that names the remedy.
- Check the connection at the moment it is made. A mailbox with IMAP switched
  off, or a grant without permission to send, is saved but flagged on the page the
  owner is already looking at rather than left to fail quietly.
- Leave out the folders that are not mail. Exchange lists the calendar, contacts,
  tasks, notes and journal over IMAP as if they were folders of messages, and
  indexing them filled search with calendar entries. They are recognised by the
  mailbox's own language (English, German, French, Spanish, Italian, Dutch,
  Portuguese) and only when a language's whole set of defaults is present, so a
  folder a user named "Agenda" is not mistaken for one. An unrecognised language
  is indexed in full, with a warning; `EMAILSERVER_EXCLUDED_SYNC_FOLDERS` names
  the folders to skip.
- Scope an account-connection link to its provider. All signed links share one
  key, so a link minted for Gmail could otherwise start a Microsoft consent flow.

### Fixes found by connecting a real mailbox

- Repeat a refused `SEARCH` without a charset. aioimaplib sends `CHARSET utf-8`;
  Exchange answers `NO [BADCHARSET (US-ASCII)]`, so every folder looked
  unsearchable and nothing synchronised. A server that takes the charset is never
  asked differently.
- Pass the XOAUTH2 token to aioimaplib as text. Its annotation says bytes, but it
  puts the value into an f-string and calls `.encode()` on it for logging, so
  bytes both corrupted the bearer token and crashed. The OAuth IMAP path had never
  run before, because Gmail OAuth accounts use the Gmail API.
- Find an attachment again by its checksum when the recorded position names a
  different part. One message can be filed in several folders, and Exchange keeps
  the sender's MIME in Sent Items but rebuilds the copy it delivers, dropping the
  text alternative and renumbering every part after it; mail a user sends to
  themselves therefore had attachments that could not be downloaded.

### Build

- Keep SQLAlchemy below 2.1. A clean install resolved 2.1.3, where a bare
  `postgresql://` URL means psycopg 3 while the image ships psycopg2, so the image
  built from this tag would not have started.

## v0.2.1 — 2026-09-23

### Sending reliability

- Check a pooled SMTP connection is still open before reusing it. Providers hang
  up on idle connections, so the first send after a quiet spell was written into
  a socket the server had already closed and came back as an ambiguous result.
  A stale connection is now replaced and the send proceeds.
- Report a send that demonstrably never left as failed rather than unknown. Only
  a failure from the point the message content goes on the wire is ambiguous; one
  from the greeting, MAIL FROM or RCPT TO is not, and reporting it as ambiguous
  cost the caller their upload slot for a message that was never transmitted.
  A connection this server did not build still reports no evidence, and no
  evidence is still read as ambiguous.
- Make the SMTP timeouts configurable and separate connecting from commands. One
  hardcoded 10 s governed both, so a large attachment on a slow link could time
  out mid-transfer and be recorded as an unknown outcome.

## v0.2.0 — 2026-09-22

### Outbound attachments

- Send files with a message, as ordinary attachments or embedded in the body.
  An AI client cannot hand the server bytes -- they would have to cross the
  conversation -- so `begin_attachment_upload` reserves a slot and returns a
  signed URL to PUT to, mirroring how attachment downloads already work.
  `send_mail` and `save_draft` take `upload_ids` for ordinary attachments and
  `inline_upload_ids` for embedded ones, and both lists may be used at once.
- Compose the MIME tree the way a mail client does: `multipart/related` around
  the body and its inline parts, ordinary attachments beside it in
  `multipart/mixed`, and each part carrying its real content type rather than
  `application/octet-stream`. An inline part is referenced from the HTML body
  by the `content_id` the reservation returns; one that nothing references is
  delivered as an ordinary attachment rather than silently dropped.
- Derive every `Content-ID` server-side, so no client-supplied text reaches a
  MIME header.
- Bound what one owner can park on the shared volume: slot count, bytes, and a
  slot lifetime. Bytes are charged as they arrive, not as they are declared.
- Settle a slot on what the transport actually reported. A send whose outcome
  is unknown -- a timeout, a disconnect, a partial acceptance -- retires its
  slots rather than returning them, because the alternative is delivering
  confidential mail twice on a guess.

## v0.1.4 — 2026-09-21

### Sync reliability

- Bound the entire IMAP greeting/CAPABILITY handshake by an absolute deadline,
  including the lock-holding library task. Pin `aioimaplib` to the tested 2.0.1
  adapter contract. EOF, cancellation and failed logout abort broken connections
  and settle their outstanding work rather than waiting indefinitely.
- Schedule accounts independently in supervised worker processes. A hung account
  no longer blocks the next polling round for every mailbox. An external process
  supervisor enforces wall-clock deadlines even when asyncio cancellation fails.
- Bound lease renewal by the job deadline and fence worker database writes by
  ownership. Keep committed message batches and existing cursors when a job dies.
- Report actual scheduler and account progress separately from API availability.
  Detect wedged scheduling/event-loop work and recover through process supervision
  and the container restart policy. Reap orphaned subprocesses with `tini`.
- Preserve the deployed Google ID-token and FastMCP private-key JWT compatibility
  fixes, previously missing from the published source.

See [sync operations](docs/sync-operations.md) for deadlines, recovery and rollback.

## v0.1.0 — 2026-08-02

First tagged release. Pre-1.0: the schema still changes between versions,
migrations run automatically on start, and there is no backporting.

### It is a mail client now, not a search box

- **Bulk writes selected the way search is.** `mark_mail`, `move_mail` and
  `delete_mail` take either `email_ids` or the same filters `search_mail` takes,
  so clearing eight thousand newsletters is one call rather than eight thousand.
  One connection, UID sets chunked to what a server will accept, and the mailbox
  lease refreshed while the batch runs. Every response reports `matched` against
  `affected` and sets `truncated`, so a partial batch is never mistaken for a
  finished one.
- **`move_mail`, `delete_mail`, `mark_mail`, `save_draft`.** Deleting means
  Trash; `permanent` is refused unless the message is already there, so removing
  mail always takes two deliberate steps.
- **Folder management** — `list_mail_folders`, `create_mail_folder`,
  `rename_mail_folder`, `delete_mail_folder`. Deleting a folder empties it into
  Trash first and refuses for INBOX and for folders the server declares a
  special use for.
- **Gmail API accounts can be written to.** Gmail has labels rather than
  folders, so one location is projected from them by precedence and moving means
  rewriting labels.

### Search

- **Stemmed across languages.** The same text is indexed once per configured
  language into one combined vector. On a real 52,000-message mailbox
  `invoices` went from 104 hits to 1,016 and `Verträge` from 133 to 1,168.
  `match="exact"` keeps the unstemmed vector for order numbers and surnames.
- **Read state is filterable.** `is_unread`, `is_flagged` and `is_answered`,
  normalised from provider flags — deliberately tri-state, because a message
  whose flags were never fetched is not the same as one that was read.
- **Folder scoping**, with Trash and Spam excluded by default and a warning
  naming any message the filter could not judge.
- Image attachments are OCR'd in every installed language rather than English
  only, which was silently mangling accented text.

### Correctness

- **A message's identity no longer encodes where it is.** Location lives in
  `message_placements`, so moving a message upstream relocates it instead of
  deleting and re-creating it.
- **Indexing a mailbox no longer marks it read.** The IMAP client fetched
  `RFC822`, which RFC 3501 defines as an alias for `BODY[]` — a non-peek fetch
  that sets `\Seen` on the server.
- A folder census that comes back short is refused rather than treated as mass
  deletion, and no pass may tombstone more than about 2% of an account.
- Reconciliation is set-based and no longer loads every message body.
- `scripts/repair_placements.py` locates messages with no usable location by
  sweeping folder headers and joining on RFC `Message-ID`.

### Operations

- Published image at `ghcr.io/alexanderkrauck/mailindex-mcp`, amd64 and arm64.
  The image tag omits the leading `v`: this release is `0.1.0`, and `0.1` follows
  its patches.
- `single_user` mode: one owner, a static bearer token, no Google project.
- MIT licence, CI on every push, and migrations verified against PostgreSQL
  from an empty database on every run.
