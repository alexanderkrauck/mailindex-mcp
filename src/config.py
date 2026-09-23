"""Configuration settings for Email Server."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EMAILSERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    # Database
    database_url: str = "postgresql://emailserver:emailserver@postgres:5432/emailserver"
    database_echo: bool = False

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_reload: bool = False

    # Email Server Settings
    smtp_host: str = "0.0.0.0"
    smtp_port: int = 2525

    # Email Processing
    email_check_interval: int = 30  # seconds
    max_emails_per_batch: int = 50
    # How often every folder is re-censused to mirror flags, moves and deletions.
    # Six hours was chosen when a census loaded every message body; it no longer
    # does, and read state that is six hours stale is not worth reporting.
    deletion_reconcile_interval: int = 300
    # The reconciler always tombstones; physical removal happens after the grace
    # period, so a wrong deletion inference stays recoverable for a few days.
    upstream_delete_policy: Literal["hard_delete", "tombstone", "retain"] = "tombstone"
    tombstone_grace_days: int = 7
    # Matched as suffixes: real folders are "INBOX.Trash", "INBOX.SPAM",
    # "[Google Mail]/Trash". Exact names match almost nothing.
    # Gmail localises Trash as "Bin", Zoho calls it "Deleted Messages": a folder
    # missing from this list keeps ranking as live mail after it is deleted.
    excluded_folder_suffixes: list[str] = [
        "trash",
        "bin",
        "spam",
        "junk",
        "deleted items",
        "deleted messages",
        "papierkorb",
    ]
    gmail_page_size: int = 100
    gmail_backfill_pages_per_cycle: int = 5
    gmail_history_pages_per_cycle: int = 20
    gmail_request_concurrency: int = 10
    # aioimaplib's own default is 10s, which a metadata FETCH over a large folder
    # exceeds routinely. A command that times out is reported as a sync failure.
    imap_command_timeout_seconds: float = 60.0
    imap_cleanup_timeout_seconds: float = 5.0
    imap_backfill_messages_per_cycle: int = 500
    imap_max_message_size: int = 50 * 1024 * 1024
    sync_account_concurrency: int = 2
    sync_lease_seconds: int = 120
    sync_stale_after_seconds: int = 900
    # Hard wall-clock work budget; cleanup is the only additional allowance.
    sync_job_timeout_seconds: float = 300.0
    sync_job_cleanup_seconds: float = 5.0
    sync_watchdog_timeout_seconds: float = 30.0
    sync_scheduler_interval_seconds: float = 2.0
    sync_max_accounts: int = 10000
    # Set in the supervised API child's environment, not required in deployment.
    sync_external_supervisor: bool = False

    # Time to establish a connection: TCP, TLS handshake and AUTH. Bounded
    # deliberately tightly -- a dead or blackholed host must fail the request
    # rather than hang it -- but above the old hardcoded 10s, which a TLS
    # handshake over a slow mobile link can genuinely exceed.
    smtp_connect_timeout_seconds: float = 15.0
    # Time for a command once connected, and therefore the budget the DATA
    # phase runs under. The same hardcoded 10s used to govern the socket for
    # its whole life, so a large attachment on a slow link tripped a
    # *connect*-sized timeout mid-transfer -- and a timeout there is ambiguous,
    # which retires the caller's upload slot. A 25 MiB attachment is ~34 MiB on
    # the wire after base64; 120s puts the floor at roughly 285 KB/s, which a
    # slow link still meets, while a hung socket is still bounded.
    smtp_command_timeout_seconds: float = 120.0

    # Attachment Settings
    max_attachment_size: int = 10 * 1024 * 1024  # 10MB

    # Text-Only Storage (Global Settings - permissive defaults)
    # Global stronger negative: if global=False, account CANNOT enable it
    store_text_only: bool = False
    max_attachment_size_text: int = 10 * 1024 * 1024  # Max size for text extraction

    # Full-text search. "simple" indexes words verbatim and is always present, so
    # an exact search can never be widened by a stemmer. Every further entry adds a
    # Snowball-stemmed copy of the same text to one combined index, which is what
    # makes "invoice" find "invoices" and "Rechnung" find "Rechnungen". Changing
    # this list requires rebuilding the index; see the README.
    search_text_configs: list[str] = ["simple", "english", "german"]

    # OCR. Empty uses every language installed in the image, which is the only
    # way image attachments in mixed-language mailboxes get read correctly.
    # Pin a subset such as "deu+eng" to trade coverage for speed.
    ocr_languages: str = ""

    # Text Extraction Settings (which types to extract text from)
    extract_pdf_text: bool = True
    extract_docx_text: bool = True
    extract_image_text: bool = True
    extract_other_text: bool = True

    # Security and identity
    # development  loopback only, no token. Safe only while Docker binds to loopback.
    # single_user  one owner authenticated by a static bearer token.
    # google       multi-user Google OAuth with dynamic client registration.
    auth_mode: Literal["development", "single_user", "google"] = "development"
    api_token: str = ""
    public_base_url: str = "http://localhost:8002"
    google_client_id: str = ""
    google_client_secret: str = ""
    google_required_scopes: list[str] = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ]
    allowed_client_redirect_uris: list[str] = [
        "https://claude.ai/api/mcp/auth_callback",
        "https://chatgpt.com/connector/oauth/*",
        "http://localhost:*",
        "http://127.0.0.1:*",
    ]
    registration_mode: Literal["open", "allowlist"] = "allowlist"
    allowed_google_subjects: list[str] = []
    allowed_google_emails: list[str] = []
    bootstrap_user_sub: str = "development-owner"
    bootstrap_user_email: str = "local-owner@example.invalid"
    claim_legacy_accounts_on_first_login: bool = False
    data_dir: str = "/data"
    credential_encryption_key: str = ""
    jwt_signing_key: str = ""
    session_secret: str = ""
    attachment_token_ttl_seconds: int = 300
    account_connect_token_ttl_seconds: int = 300
    password_setup_token_ttl_seconds: int = 900
    search_cursor_ttl_seconds: int = 86_400
    extraction_timeout_seconds: int = 30
    max_extracted_text_chars: int = 250_000
    max_message_body_chars: int = 250_000
    max_pdf_pages: int = 100
    regex_statement_timeout_ms: int = 2_000
    max_regex_pattern_length: int = 256
    max_send_recipients: int = 50
    max_sends_per_minute: int = 20
    max_accounts_per_user: int = 20
    max_outbound_attachment_bytes: int = 25 * 1024 * 1024
    max_outbound_attachments: int = 20
    # An AI client cannot hand the server a file: bytes would have to cross the
    # conversation. It asks for an upload slot instead and PUTs the bytes to a
    # signed URL, exactly mirroring how attachment downloads already work.
    # The slot is short-lived because it is a write grant against this volume.
    outbound_upload_ttl_seconds: int = 900
    # Per owner, so one tenant cannot fill the data volume with slots nobody
    # ever sends. Counted over pending uploads only; a consumed one is swept.
    # The count is the binding constraint, not the byte budget: this many slots
    # each filled to max_outbound_attachment_bytes is the true worst case a
    # single tenant can park on a shared volume, so 10 x 25MiB = 250MiB keeps it
    # level with the byte budget below rather than five times over it.
    max_pending_uploads_per_user: int = 10
    max_pending_upload_bytes_per_user: int = 250 * 1024 * 1024

    # Mailbox writes are ordinary behaviour: this is a mail client for an agent,
    # and moving, marking and deleting are what a mail client does. Safety lives
    # in the mechanism -- leases, server confirmation before the local commit,
    # tombstones instead of destruction -- not in a switch.
    max_writes_per_minute: int = 30
    # Messages one write call may touch. Bulk triage is the point -- an inbox with
    # 8,000 newsletters cannot be cleaned one tool call at a time -- so this is a
    # backstop against a runaway selection, not a pace-setter. Commands are
    # chunked and the lease is refreshed, so a large batch is still one connection.
    max_write_batch: int = 25_000
    # UIDs per IMAP command. Servers cap a command line at a few kilobytes, and
    # arbitrary UIDs cost about seven bytes each; chunking here keeps one bulk
    # operation on one connection under one lease.
    imap_uid_set_chunk: int = 500
    # How long a write waits for a sync pass to release the mailbox before giving
    # up. A pass over a large account outlasts a few seconds, and failing a write
    # the caller would only retry itself is worse than waiting.
    mail_write_lease_wait_seconds: float = 90.0

    # Logging
    log_level: str = "INFO"
    log_file: str = ""  # Empty = stdout only (stateless). Set to path for file logging.

    # MCP
    mcp_enabled: bool = True
    mcp_port: int = 8001

settings = Settings()


def data_path(filename: str) -> Path:
    """Return a path in the persistent private data directory."""
    return Path(settings.data_dir) / filename
