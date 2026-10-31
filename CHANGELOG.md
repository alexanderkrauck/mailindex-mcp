# Changelog

Notable changes. Dates are release dates; the git history is finer grained.

## v0.3.3 — 2026-10-31

### Gmail throttling slows a synchronization down instead of failing it

- Gmail enforces each mailbox's quota over seconds to a minute and answers with
  403 `rateLimitExceeded` (or 429) once it is used up. The client did not retry
  the 403 at all: during a catch-up, ten parallel fetches used up the quota, the
  run failed, and the account showed `error` / `PROVIDER_SYNC_FAILED` until a
  retry minutes later used it up again.
- Now a throttle starts a throttled period for that client. Every request waits
  behind one shared pause: 5 s, then 10, 20, 40 and 60 s for each further
  throttle in the period, or as long as a `Retry-After` asks (1 s to 5 minutes).
  Between pauses, requests start one at a time, ten a second. The period ends
  once a minute passes after a pause without another throttle. Answers to
  requests that were already in flight when a pause began do not lengthen it.
  Any other 403 is still final.
- A synchronization never waits past its job's deadline, less a minute: no
  pause and no paced slot may end later. When one would, the run stops, keeps
  what it fetched, and records where it stopped. A run that committed work
  (messages newly recorded for a full sync, or a saved checkpoint) stays
  `backfilling` (or `healthy`) with `last_error_code` `PROVIDER_RATE_LIMITED`,
  a `retry_at` and its failure count reset. A run that committed nothing counts
  as a failure, retried no sooner than Gmail asked.
- A full sync cut short resumes where it stopped. It no longer starts a new
  generation when its first page did not finish, and it skips the messages it
  already recorded on a page it is repeating. History is read in pages of 100
  rather than 500 changes. While throttles keep ending runs before a page is
  done, each next run asks for pages half as large (halved for every
  consecutive failed run, down to one change), so one can finish and save its
  place; a run that saves its place returns to full pages.
- A synchronization error from one of Gmail's HTTP answers is classified by its
  status, not by words in its text. The text includes the request URL, so a 500
  for a message whose id happened to contain "429" showed as rate limiting.
  Other error texts count as throttling only for phrases such as "rate limit",
  "too many requests" or "throttled", no longer for a bare "rate" or "429".
- A tool call (a label change, a move to Trash, a permanent deletion, a draft, an
  attachment fetch) waits at most 15 seconds, then answers `RATE_LIMITED` with
  `retry_after`: the remaining pause, or once paced requests run out of time,
  the length of the last pause. Label changes and permanent deletions Gmail did
  apply before that are recorded in the index, and the answer says how many.
- Parallel requests that need a new access token share one refresh, and a late
  401 for the old token no longer discards the new one.
- A chunk of parallel fetches now lets every fetch finish, and keeps the messages
  that arrived, before it re-raises a failure.

### Permanent deletion on Gmail accounts connected by OAuth works

- `delete_mail` with `permanent: true` always failed on these accounts. It called
  `POST messages/{id}/delete`, a route Gmail does not have (it answers 404), and
  fell back to Gmail's real route, `DELETE messages/{id}`, only on a 405 that
  never came. It now calls `DELETE` directly, through the same throttle handling
  and token refresh as every other Gmail call.
- Gmail deletes a message whatever its labels are, and the index can be behind
  Gmail. So each message's labels are read from Gmail first, and only one Gmail
  itself still has in the Bin or Spam is deleted. One restored since the index
  last saw it is left alone, its new place is recorded, and the answer lists it
  as skipped, also when the call ends throttled or refused.
- A message Gmail no longer has counts as deleted. Any other refusal stays an
  error, including a 404 that is not Gmail's own "not found".
- Asked to delete mail that is not in the Bin, the answer now names the
  messages, and no longer tells the caller to move them there first without
  saying that one someone restored was taken out on purpose.

## v0.3.2 — 2026-10-31

### Grok can connect

- Grok's custom connectors can now add the server. Grok registers itself through
  dynamic client registration with the callback
  `https://grok.com/connectors-oauth-exchange-code/`. The default list of
  allowed client callbacks did not include it, so `/register` answered
  `invalid_redirect_uri` before any sign-in page appeared, and nothing was
  logged. The default now allows that callback with and without the trailing
  slash, and the same path on `console.x.ai`, assumed for connectors a team
  admin sets up there. They are exact paths, not wildcards; as for the existing
  claude.ai entry, FastMCP compares scheme, host, port and path, not a query or
  fragment.
- A deployment that sets `EMAILSERVER_ALLOWED_CLIENT_REDIRECT_URIS` replaces the
  default list and has to add these URIs itself.
- A registration refused for its callback is now logged as a warning naming the
  client and the callback it asked for, so the next client that cannot connect
  shows what to allow. Registrations the MCP SDK refuses earlier, for their
  scope, grant type or response type, are still not logged.

## v0.3.1 — 2026-10-16

### Drafts keep their text on Exchange Online

- Every message handed to a provider now has CRLF line endings, as RFC 5322 and
  RFC 3501 require. Drafts were serialized with bare LF, which Python's own parser
  accepts -- but Exchange Online does not read "=" followed by LF as a
  quoted-printable soft line break. A draft saved there came back with "=" in place
  of the first character after every soft break ("läng=r", "<=span>"), UTF-8
  sequences a break had split were lost, and a `cid:` URL that crossed a break no
  longer matched its image, so inline images did not render. Reproduced against
  Exchange Online; the same bytes with CRLF were stored intact.
- The draft builder writes CRLF, and the IMAP APPEND normalises whatever it is
  given, as `imaplib` does and `aioimaplib` does not. The MIME posted to Microsoft
  Graph is generated with CRLF as well, for conformance: sending never had the
  drafts' damage, because the send path encodes text as base64 and so has no
  soft breaks to lose. SMTP was already CRLF: `smtplib` writes it itself.
- Clients no longer need to keep encoded lines short themselves. Long HTML and
  text lines round-trip unchanged through `save_draft` and `send_mail`.

### Deleting a mail account

- `delete_mail_account` removes an account from this server: the stored
  credential, the settings, and everything indexed from it -- messages, extracted
  attachment text, participants, folder placements, sync checkpoints. Nothing at
  the provider changes, and the mailbox can be connected again. Called with only
  `account_id` it reports what would go; it deletes when `confirm_name` equals
  the account's name. A synchronization step already writing to the account is
  waited for (up to 30 seconds) and the rest of that synchronization fenced out.
  While a step is still writing after that, or a send from the account that has
  recorded its claim has not settled, the answer is `CONFLICT` with
  `retry_after` (and `retryable: true`), and nothing is deleted. A send the
  deletion overtakes fails with nothing sent.
- The record of sent mail outlives the account it was sent from. Migration
  `20261016_17` makes `send_audits.smtp_config_id` nullable with
  `ON DELETE SET NULL`; it was `RESTRICT`, so an account that had ever sent mail
  could not be deleted at all. Downgrading refuses while records of deleted
  accounts exist, rather than deleting them.
- The same migration indexes `email_attachments.email_log_id` and
  `send_audits.reply_to_email_id`. Both are foreign keys to `email_logs` that had
  no index, so deleting a message made PostgreSQL scan each referencing table to
  check nothing still pointed at it. Deleting a 28,000-message account, against
  30,000 attachments and 20,000 send audits, took 48 seconds without them and
  under one with them.
- `send_mail` no longer reports a delivered reply as failed when its account is
  deleted the moment it settles. A send that fails before anything is
  transmitted -- its claim refused because the account was just deleted, say --
  hands its upload slots back instead of leaving them stuck in `sending`.
- `DELETE /api/v1/accounts/{id}` is unchanged: it disables an account and keeps
  its data.

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
