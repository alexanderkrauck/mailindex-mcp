# Changelog

Notable changes. Dates are release dates; the git history is finer grained.

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
