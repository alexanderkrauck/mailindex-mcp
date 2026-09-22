"""Authenticated, tenant-scoped HTTP API."""

import asyncio
import logging
import secrets
from datetime import datetime
from typing import Annotated
from urllib.parse import parse_qs, quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, EmailStr, Field, SecretStr
from sqlalchemy.orm import Session

from src.config import settings
from src.database.connection import get_db
from src.email.email_processor import EmailProcessor
from src.email.smtp_client import SMTPClient
from src.email.smtp_sender import EmailSenderManager
from src.handlers.email_handler_types import SendMailInput
from src.models.smtp_config import SMTPConfig
from src.models.user import User
from src.security.account_connect_tokens import (
    verify_account_connect_token,
    verify_password_setup_token,
)
from src.security.auth import get_current_user, owned_account_query
from src.security.download_tokens import issue_download_token, verify_download_token
from src.security.provider_tokens import GMAIL_MAIL_SCOPE, encode_oauth_credential
from src.security.upload_tokens import verify_upload_token
from src.services.attachment_service import owned_attachment, refetch_attachment_bytes
from src.services.mail_service import (
    get_thread,
    owned_account,
    owned_email,
    search_mail,
    search_mail_regex,
    send_mail,
    serialize_attachment,
    serialize_email,
)
from src.services.upload_service import store_upload_stream
from src.web_pages import (
    invalid_setup_page,
    password_form_page,
    password_saved_page,
)

logger = logging.getLogger(__name__)
router = APIRouter()
email_sender_manager = EmailSenderManager()
email_processor = EmailProcessor()
CurrentUser = Annotated[User, Depends(get_current_user)]
GMAIL_CONNECTION_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    GMAIL_MAIL_SCOPE,
)


class GoogleLoginInput(BaseModel):
    credential: str


class MailAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    account_name: EmailStr | None = None
    provider: str = Field(default="imap", pattern="^(imap|zoho|gmail)$")
    host: str
    port: int = Field(default=993, ge=1, le=65535)
    smtp_host: str
    smtp_port: int = Field(default=465, ge=1, le=65535)
    username: str
    password: SecretStr | None = Field(default=None, min_length=1)
    imap_use_ssl: bool = True
    imap_use_tls: bool = False
    smtp_use_ssl: bool = True
    smtp_use_tls: bool = False
    enabled: bool = True
    verify_connection: bool = True


class MailAccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    account_name: EmailStr | None = None
    provider: str | None = Field(default=None, pattern="^(imap|zoho|gmail)$")
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: SecretStr | None = Field(default=None, min_length=1)
    imap_use_ssl: bool | None = None
    imap_use_tls: bool | None = None
    smtp_use_ssl: bool | None = None
    smtp_use_tls: bool | None = None
    enabled: bool | None = None
    verify_connection: bool = True


class ReplyInput(BaseModel):
    account_id: int
    body_text: str | None = None
    body_html: str | None = None
    cc_addresses: list[EmailStr] = Field(default_factory=list)
    include_original: bool = True
    idempotency_key: str | None = None


class ForwardInput(BaseModel):
    account_id: int
    to_addresses: list[EmailStr]
    body_text: str | None = None
    body_html: str | None = None
    cc_addresses: list[EmailStr] = Field(default_factory=list)
    bcc_addresses: list[EmailStr] = Field(default_factory=list)
    include_attachments: bool = True
    idempotency_key: str | None = None


def account_response(account: SMTPConfig) -> dict:
    result = account.dict()
    result["credential_configured"] = bool(account.credential_ciphertext)
    return result


def validate_transport(account: SMTPConfig) -> None:
    if not account.imap_use_ssl and not account.imap_use_tls:
        raise HTTPException(status_code=400, detail="IMAP must use SSL or STARTTLS")
    if not account.smtp_use_ssl and not account.smtp_use_tls:
        raise HTTPException(status_code=400, detail="SMTP must use SSL or STARTTLS")


async def verify_account_connection(account: SMTPConfig) -> dict[str, bool]:
    """Test a detached account without returning provider error details or credentials."""
    if not account.credential_ciphertext:
        return {"imap": False, "smtp": False}
    detached = SMTPConfig.create_detached(account)
    imap = SMTPClient(detached)
    try:
        imap_ok = await imap.connect()
    finally:
        await imap.disconnect()
    sender = await email_sender_manager.get_sender(detached)
    try:
        smtp_ok = await sender.connect()
    finally:
        sender.disconnect()
        await email_sender_manager.invalidate(account.id)
    return {"imap": imap_ok, "smtp": smtp_ok}


async def safe_connection_test(account: SMTPConfig) -> dict[str, bool]:
    try:
        return await verify_account_connection(account)
    except Exception as exc:
        logger.info(
            "Connection test failed for account %s: %s",
            account.id,
            type(exc).__name__,
        )
        return {"imap": False, "smtp": False}


@router.post("/auth/google")
async def google_login(payload: GoogleLoginInput, request: Request, db: Session = Depends(get_db)):
    """Create a browser session from a Google Identity Services credential."""
    if settings.auth_mode != "google":
        raise HTTPException(status_code=404, detail="Google login is not enabled")
    from src.security.auth import resolve_user, verify_google_id_token

    claims = await verify_google_id_token(payload.credential)
    user = resolve_user(db, claims)
    request.session["identity"] = {
        "sub": user.google_sub,
        "email": user.email,
        "name": user.display_name,
    }
    return {"id": user.id, "email": user.email, "display_name": user.display_name}


@router.post("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return {"success": True}


@router.get("/me")
async def me(user: CurrentUser):
    return {"id": user.id, "email": user.email, "display_name": user.display_name}


@router.get("/accounts")
async def list_accounts(user: CurrentUser, db: Session = Depends(get_db)):
    return [account_response(account) for account in owned_account_query(db, user).order_by(SMTPConfig.id).all()]


@router.post("/accounts", status_code=201)
async def create_account(payload: MailAccountCreate, user: CurrentUser, db: Session = Depends(get_db)):
    if owned_account_query(db, user).count() >= settings.max_accounts_per_user:
        raise HTTPException(status_code=400, detail="Mail account limit reached")
    if owned_account_query(db, user).filter(SMTPConfig.name == payload.name).first():
        raise HTTPException(status_code=409, detail="An account with this name already exists")
    values = payload.model_dump(exclude={"password", "verify_connection"})
    account = SMTPConfig(owner_user_id=user.id, auth_type="password", **values)
    if payload.password is not None:
        account.password = payload.password.get_secret_value()
    account.sync_state = (
        "disabled"
        if not account.enabled
        else ("pending" if account.credential_ciphertext else "credentials_required")
    )
    validate_transport(account)
    db.add(account)
    db.flush()
    connection = None
    if payload.verify_connection and account.credential_ciphertext:
        connection = await safe_connection_test(account)
        if not all(connection.values()):
            account.sync_state = "error"
            account.last_error_code = "CONNECTION_TEST_FAILED"
            account.last_error_message = "Saved configuration did not pass the connection test"
    db.commit()
    db.refresh(account)
    logger.info("User %s added mail account %s", user.id, account.id)
    result = account_response(account)
    result["configuration_saved"] = True
    if connection is not None:
        result["connection_test"] = connection
    return result


@router.get("/accounts/{account_id:int}")
async def get_account(account_id: int, user: CurrentUser, db: Session = Depends(get_db)):
    return account_response(owned_account(db, user.id, account_id))


@router.patch("/accounts/{account_id:int}")
async def update_account(
    account_id: int, payload: MailAccountUpdate, user: CurrentUser, db: Session = Depends(get_db)
):
    account = owned_account(db, user.id, account_id)
    if (
        payload.name is not None
        and payload.name != account.name
        and owned_account_query(db, user).filter(SMTPConfig.name == payload.name).first()
    ):
        raise HTTPException(status_code=409, detail="An account with this name already exists")
    values = payload.model_dump(
        exclude_unset=True,
        exclude={"password", "verify_connection"},
    )
    identity_fields = {"provider", "host", "username"}
    identity_changed = {
        field
        for field in identity_fields
        if field in values and getattr(account, field) != values[field]
    }
    for field, value in values.items():
        setattr(account, field, value)
    if payload.password is not None:
        account.password = payload.password.get_secret_value()
    validate_transport(account)
    connection_fields = {
        "provider",
        "host",
        "port",
        "smtp_host",
        "smtp_port",
        "username",
        "password",
        "imap_use_ssl",
        "imap_use_tls",
        "smtp_use_ssl",
        "smtp_use_tls",
    }
    should_verify = payload.verify_connection and bool(payload.model_fields_set & connection_fields)
    connection = None
    if should_verify and account.credential_ciphertext:
        connection = await safe_connection_test(account)
    if identity_changed:
        account.sync_cursors.clear()
        account.initial_sync_complete = False
        account.backfill_complete = False
        account.backfill_processed = 0
        account.backfill_total = None
        account.provider_sync_token = None
        account.sync_page_token = None
    account.sync_lock_token = None
    account.sync_locked_at = None
    account.sync_lock_expires_at = None
    connection_failed = connection is not None and not all(connection.values())
    account.last_error_code = (
        "CONNECTION_TEST_FAILED" if connection_failed else None
    )
    account.last_error_message = (
        "Saved configuration did not pass the connection test"
        if connection_failed
        else None
    )
    account.consecutive_failures = 0
    account.retry_at = None
    account.sync_state = (
        "disabled"
        if not account.enabled
        else (
            "credentials_required"
            if not account.credential_ciphertext
            else (
                "error"
                if connection_failed
                else ("healthy" if account.backfill_complete else "pending")
            )
        )
    )
    db.commit()
    db.refresh(account)
    await email_sender_manager.invalidate(account.id)
    result = account_response(account)
    result["configuration_saved"] = True
    if connection is not None:
        result["connection_test"] = connection
    return result


def _password_setup_account(
    db: Session,
    account_id: int,
    token: str,
) -> SMTPConfig | None:
    account = db.query(SMTPConfig).filter(SMTPConfig.id == account_id).first()
    if not account:
        return None
    try:
        claims = verify_password_setup_token(
            token,
            account_id,
            account.credential_ciphertext,
        )
    except ValueError:
        return None
    if claims.user_id != account.owner_user_id:
        return None
    return account


def _password_setup_session_account(
    request: Request,
    db: Session,
    account_id: int,
) -> SMTPConfig | None:
    grant = request.session.get("mail_password_setup")
    if not isinstance(grant, dict) or grant.get("account_id") != account_id:
        return None
    return _password_setup_account(db, account_id, str(grant.get("token") or ""))


@router.get("/accounts/{account_id:int}/password/setup")
async def start_password_setup(
    account_id: int,
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    account = _password_setup_account(db, account_id, token)
    if not account:
        return invalid_setup_page()
    request.session["mail_password_setup"] = {
        "account_id": account_id,
        "token": token,
    }
    return RedirectResponse(
        url=f"{settings.public_base_url.rstrip('/')}/api/v1/accounts/{account_id}/password",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/accounts/{account_id:int}/password")
async def password_setup_form(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    account = _password_setup_session_account(request, db, account_id)
    if not account:
        request.session.pop("mail_password_setup", None)
        return invalid_setup_page()
    return password_form_page(
        account_name=account.name,
        address=account.account_name or account.username,
    )


@router.post("/accounts/{account_id:int}/password")
async def save_password_setup(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    account = _password_setup_session_account(request, db, account_id)
    if not account:
        request.session.pop("mail_password_setup", None)
        return invalid_setup_page()
    content_type = request.headers.get("content-type", "").partition(";")[0]
    body = await request.body()
    if content_type != "application/x-www-form-urlencoded" or len(body) > 4096:
        return password_form_page(
            account_name=account.name,
            address=account.account_name or account.username,
            error="The password submission was not valid. Please try again.",
        )
    try:
        values = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=2,
        )
    except (UnicodeDecodeError, ValueError):
        values = {}
    passwords = values.get("password", [])
    password = passwords[0] if len(passwords) == 1 else ""
    if not password or len(password) > 1024:
        return password_form_page(
            account_name=account.name,
            address=account.account_name or account.username,
            error="Enter a valid mailbox password.",
        )

    account.password = password
    account.last_error_code = None
    account.last_error_message = None
    account.consecutive_failures = 0
    account.retry_at = None
    account.sync_lock_token = None
    account.sync_locked_at = None
    account.sync_lock_expires_at = None
    account.sync_state = "pending" if account.enabled else "disabled"
    db.commit()
    await email_sender_manager.invalidate(account.id)
    request.session.pop("mail_password_setup", None)
    logger.info("User %s stored a credential for account %s", account.owner_user_id, account.id)
    return password_saved_page()


@router.delete("/accounts/{account_id:int}")
async def delete_account(account_id: int, user: CurrentUser, db: Session = Depends(get_db)):
    account = owned_account(db, user.id, account_id)
    account.enabled = False
    account.sync_state = "disabled"
    account.sync_lock_token = None
    account.sync_locked_at = None
    account.sync_lock_expires_at = None
    account.retry_at = None
    db.commit()
    return {"success": True, "message": "Account disabled; synced data was retained"}


@router.post("/accounts/{account_id:int}/test")
async def test_account(account_id: int, user: CurrentUser, db: Session = Depends(get_db)):
    account = owned_account(db, user.id, account_id)
    if not account.credential_ciphertext:
        raise HTTPException(status_code=400, detail="Mailbox password is not configured")
    connection = await verify_account_connection(account)
    if not all(connection.values()):
        raise HTTPException(status_code=400, detail=connection)
    return {"imap": "connected", "smtp": "connected"}


@router.post("/accounts/{account_id:int}/sync")
async def sync_account(account_id: int, user: CurrentUser, db: Session = Depends(get_db)):
    owned_account(db, user.id, account_id)
    result = await email_processor.process_server_now(account_id, owner_user_id=user.id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


def _gmail_flow(state: str | None = None):
    from google_auth_oauthlib.flow import Flow

    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"{settings.public_base_url.rstrip('/')}/api/v1/accounts/gmail/callback"],
        }
    }
    flow = Flow.from_client_config(
        client_config,
        scopes=GMAIL_CONNECTION_SCOPES,
        state=state,
    )
    flow.redirect_uri = f"{settings.public_base_url.rstrip('/')}/api/v1/accounts/gmail/callback"
    return flow


def _start_gmail_connection(request: Request, user: User) -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    request.session["gmail_oauth_state"] = state
    request.session["gmail_connect_user_id"] = user.id
    flow = _gmail_flow(state)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    if not flow.code_verifier:
        raise HTTPException(
            status_code=500,
            detail="Gmail OAuth PKCE initialization failed",
        )
    request.session["gmail_oauth_code_verifier"] = flow.code_verifier
    return RedirectResponse(authorization_url)


@router.get("/accounts/gmail/connect")
async def connect_gmail(request: Request, user: CurrentUser):
    return _start_gmail_connection(request, user)


@router.get("/accounts/gmail/connect/mcp")
async def connect_gmail_from_mcp(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
):
    try:
        user_id = verify_account_connect_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user = db.query(User).filter(User.id == user_id, User.status == "active").first()
    if not user:
        raise HTTPException(status_code=401, detail="Account connection user is unavailable")
    return _start_gmail_connection(request, user)


def _recover_gmail_scope_change(flow, warning: Warning) -> bool:
    granted_scopes = set(getattr(warning, "new_scope", ()) or ())
    token = getattr(warning, "token", None)
    if not set(GMAIL_CONNECTION_SCOPES).issubset(granted_scopes):
        return False
    if not token or not token.get("access_token"):
        return False
    flow.oauth2session.token = dict(token)
    return True


@router.get("/accounts/gmail/callback")
async def gmail_callback(
    request: Request,
    code: str,
    state: str,
    db: Session = Depends(get_db),
):
    expected_state = request.session.pop("gmail_oauth_state", None)
    if not expected_state or not secrets.compare_digest(state, expected_state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    user_id = request.session.pop("gmail_connect_user_id", None)
    code_verifier = request.session.pop("gmail_oauth_code_verifier", None)
    if not code_verifier:
        raise HTTPException(
            status_code=400,
            detail="Gmail connection session expired; start the connection again",
        )
    user = db.query(User).filter(User.id == user_id, User.status == "active").first()
    if not user:
        raise HTTPException(status_code=401, detail="Account connection session expired")
    flow = _gmail_flow(state)
    flow.code_verifier = code_verifier
    try:
        await asyncio.to_thread(flow.fetch_token, code=code)
    except Warning as exc:
        if not _recover_gmail_scope_change(flow, exc):
            logger.warning(
                "Gmail OAuth returned insufficient scopes for user %s",
                user.id,
            )
            raise HTTPException(
                status_code=400,
                detail="Google did not grant all required Gmail permissions",
            ) from None
        logger.info(
            "Accepted Gmail OAuth token with an expanded scope set for user %s",
            user.id,
        )
    except Exception as exc:
        logger.warning(
            "Gmail OAuth token exchange failed for user %s: %s",
            user.id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=400,
            detail="Gmail authorization failed; start the connection again",
        ) from None
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise HTTPException(status_code=400, detail="Google did not return an offline refresh token")

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {credentials.token}"},
        )
        response.raise_for_status()
        provider_identity = response.json()
    email = str(provider_identity["email"]).lower()
    account = (
        owned_account_query(db, user)
        .filter(SMTPConfig.provider == "gmail", SMTPConfig.provider_account_id == provider_identity["sub"])
        .first()
    )
    if not account:
        if owned_account_query(db, user).count() >= settings.max_accounts_per_user:
            raise HTTPException(status_code=400, detail="Mail account limit reached")
        account = SMTPConfig(
            owner_user_id=user.id,
            provider="gmail",
            auth_type="oauth2",
            provider_account_id=provider_identity["sub"],
            name=f"Gmail - {email}",
            account_name=email,
            username=email,
            host="imap.gmail.com",
            port=993,
            smtp_host="smtp.gmail.com",
            smtp_port=465,
            imap_use_ssl=True,
            imap_use_tls=False,
            smtp_use_ssl=True,
            smtp_use_tls=False,
            enabled=True,
            credential_ciphertext=encode_oauth_credential(credentials.refresh_token),
        )
        db.add(account)
    else:
        account.credential_ciphertext = encode_oauth_credential(credentials.refresh_token)
        account.enabled = True
    account.sync_state = "healthy" if account.backfill_complete else "pending"
    account.last_error_code = None
    account.last_error_message = None
    account.consecutive_failures = 0
    account.retry_at = None
    db.commit()
    db.refresh(account)
    return RedirectResponse(url=f"{settings.public_base_url.rstrip('/')}?connected=gmail")


@router.get("/emails")
async def list_emails(
    user: CurrentUser,
    db: Session = Depends(get_db),
    account_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    deduplicate: str = Query(default="exact", pattern="^(none|exact|mirror)$"),
):
    return search_mail(
        db,
        user,
        account_id=account_id,
        limit=limit,
        cursor=cursor,
        deduplicate=deduplicate,
    )


@router.get("/emails/search")
async def search_emails(
    user: CurrentUser,
    db: Session = Depends(get_db),
    query: str = "",
    account_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    participant: str | None = None,
    participants: list[str] | None = Query(default=None),
    has_attachments: bool = False,
    search_attachments: bool = False,
    folders: list[str] | None = Query(default=None),
    exclude_folders: list[str] | None = Query(default=None),
    is_unread: bool | None = None,
    is_flagged: bool | None = None,
    is_answered: bool | None = None,
    match: str = Query(default="stemmed", pattern="^(stemmed|exact)$"),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    deduplicate: str = Query(default="exact", pattern="^(none|exact|mirror)$"),
):
    return search_mail(
        db,
        user,
        query=query,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        participant=participant,
        participants=participants,
        has_attachments=has_attachments,
        search_attachments=search_attachments,
        folders=folders,
        exclude_folders=exclude_folders,
        is_unread=is_unread,
        is_flagged=is_flagged,
        is_answered=is_answered,
        match=match,
        limit=limit,
        cursor=cursor,
        deduplicate=deduplicate,
    )


@router.get("/emails/search/regex")
async def search_emails_regex(
    user: CurrentUser,
    pattern: str,
    field: str | None = None,
    fields: list[str] | None = Query(default=None),
    db: Session = Depends(get_db),
    account_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    folders: list[str] | None = Query(default=None),
    exclude_folders: list[str] | None = Query(default=None),
    is_unread: bool | None = None,
    is_flagged: bool | None = None,
    is_answered: bool | None = None,
    limit: int = Query(default=25, ge=1, le=50),
    cursor: str | None = None,
    deduplicate: str = Query(default="exact", pattern="^(none|exact|mirror)$"),
):
    return search_mail_regex(
        db,
        user,
        pattern=pattern,
        field=field,
        fields=fields,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
        folders=folders,
        exclude_folders=exclude_folders,
        is_unread=is_unread,
        is_flagged=is_flagged,
        is_answered=is_answered,
        limit=limit,
        cursor=cursor,
        deduplicate=deduplicate,
    )


@router.get("/emails/{email_id:int}")
async def get_email(email_id: int, user: CurrentUser, db: Session = Depends(get_db)):
    return serialize_email(owned_email(db, user.id, email_id), include_body=True)


@router.get("/emails/{email_id:int}/thread")
async def email_thread(email_id: int, user: CurrentUser, db: Session = Depends(get_db)):
    return get_thread(db, user, email_id)


@router.get("/attachments/{attachment_id:int}")
async def get_attachment(attachment_id: int, user: CurrentUser, db: Session = Depends(get_db)):
    attachment = owned_attachment(db, user.id, attachment_id)
    result = serialize_attachment(attachment, include_text=True)
    token = issue_download_token(user.id, attachment.id)
    result["download_url"] = (
        f"{settings.public_base_url.rstrip('/')}/api/v1/attachments/{attachment.id}/download?token={token}"
    )
    result["download_expires_in"] = settings.attachment_token_ttl_seconds
    return result


@router.get("/attachments/{attachment_id:int}/download")
async def download_attachment(attachment_id: int, token: str, db: Session = Depends(get_db)):
    try:
        user_id = verify_download_token(token, attachment_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    attachment, payload = await refetch_attachment_bytes(db, user_id, attachment_id)
    from src.email import sanitize_filename

    download_name = sanitize_filename(attachment.filename)
    ascii_name = download_name.encode("ascii", errors="ignore").decode() or "attachment"
    return Response(
        content=payload,
        media_type=attachment.detected_content_type or attachment.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(download_name)}"
            )
        },
    )


@router.put("/uploads/{upload_id}")
async def store_upload(
    upload_id: str,
    request: Request,
    # Defaulted rather than required, so an absent token is refused by the
    # verifier with the same 403 as a forged one. A missing grant and a bad grant
    # are the same failure, and answering them differently only tells a prober
    # which half of the request to fix.
    token: str = "",
    db: Session = Depends(get_db),
):
    """Receive the raw bytes of an outbound attachment into a reserved slot.

    The mirror image of the attachment download above: there, a signed URL hands
    bytes out; here, a signed URL takes them in. The body is streamed straight to
    the volume rather than buffered, so the size limit is enforced against what
    has actually arrived and a hostile client cannot spend the process's memory
    before being refused.
    """
    try:
        user_id = verify_upload_token(token, upload_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None

    # A declared length that cannot fit is refused before a single byte is read.
    # It is only a hint -- it may be absent or a lie -- so the streaming check in
    # the service remains the authority, but an honest client is told immediately
    # instead of uploading 2GB to be told at the end.
    #
    # isascii() as well as isdigit(), because str.isdigit() is true of Unicode
    # digits like '\u00b2' that int() then refuses. h11 would never deliver one,
    # but the header is attacker-controlled and an ASGI server that does is not
    # worth an unhandled 500.
    declared_length = request.headers.get("content-length")
    if (
        declared_length
        and declared_length.isascii()
        and declared_length.isdigit()
        and int(declared_length) > settings.max_outbound_attachment_bytes
    ):
        raise HTTPException(
            status_code=413,
            detail=(
                f"Upload exceeds the {settings.max_outbound_attachment_bytes} "
                "byte outbound attachment limit"
            ),
        )

    try:
        upload = await store_upload_stream(db, upload_id, user_id, request.stream())
    except Exception:
        # A rejected upload leaves the slot's row modified but uncommitted, and a
        # dirty session would poison the retry the caller is about to make. Every
        # exception, not just HTTPException: a slot swept mid-upload surfaces as
        # a StaleDataError from the ORM, and skipping the rollback there leaves
        # the connection pinned and answers the client with an opaque 500.
        db.rollback()
        raise
    # Already a plain mapping of what was stored, deliberately: the service does
    # not re-read the row to build it, so a TTL that lapses between the promote
    # and this line cannot turn a successful upload into a 404.
    return upload


@router.post("/send")
async def send_email(payload: SendMailInput, user: CurrentUser, db: Session = Depends(get_db)):
    return await send_mail(db, user, payload)


@router.post("/emails/{email_id:int}/reply")
async def reply_to_email(
    email_id: int, payload: ReplyInput, user: CurrentUser, db: Session = Depends(get_db)
):
    original = owned_email(db, user.id, email_id)
    owned_account(db, user.id, payload.account_id)
    subject = original.subject or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    body_text = payload.body_text or ""
    if payload.include_original:
        quoted = "\n".join(f"> {line}" for line in (original.body_plain or "").splitlines())
        body_text += f"\n\nOn {original.email_date or 'unknown'}, {original.sender} wrote:\n{quoted}"
    send_payload = SendMailInput(
        account_id=payload.account_id,
        to_addresses=[original.sender],
        subject=subject,
        reply_to_email_id=original.id,
        body_text=body_text,
        body_html=payload.body_html,
        cc_addresses=payload.cc_addresses,
        idempotency_key=payload.idempotency_key,
    )
    return await send_mail(db, user, send_payload)


@router.post("/emails/{email_id:int}/forward")
async def forward_email(
    email_id: int, payload: ForwardInput, user: CurrentUser, db: Session = Depends(get_db)
):
    original = owned_email(db, user.id, email_id)
    owned_account(db, user.id, payload.account_id)
    subject = original.subject or ""
    if not subject.lower().startswith("fwd:"):
        subject = f"Fwd: {subject}"
    header = (
        "\n\n---------- Forwarded message ----------\n"
        f"From: {original.sender}\nDate: {original.email_date or 'unknown'}\n"
        f"Subject: {original.subject or '(no subject)'}\nTo: {original.recipient}\n\n"
    )
    attachments = []
    if payload.include_attachments:
        for attachment in original.attachments:
            _, binary = await refetch_attachment_bytes(db, user.id, attachment.id)
            attachments.append({"data": binary, "filename": attachment.filename})

    send_payload = SendMailInput(
        account_id=payload.account_id,
        to_addresses=payload.to_addresses,
        subject=subject,
        body_text=(payload.body_text or "") + header + (original.body_plain or ""),
        body_html=payload.body_html,
        cc_addresses=payload.cc_addresses,
        bcc_addresses=payload.bcc_addresses,
        idempotency_key=payload.idempotency_key,
    )
    return await send_mail(db, user, send_payload, attachments=attachments or None)


@router.get("/status")
async def status_view(user: CurrentUser, db: Session = Depends(get_db)):
    accounts = owned_account_query(db, user)
    account_ids = [row.id for row in accounts.all()]
    from src.models.email import EmailLog

    return {
        "status": "running",
        "processor_active": email_processor.processing,
        "accounts": len(account_ids),
        "emails": db.query(EmailLog).filter(EmailLog.smtp_config_id.in_(account_ids)).count()
        if account_ids
        else 0,
    }
