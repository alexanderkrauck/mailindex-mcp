"""Send mail through Microsoft Graph, for Exchange Online mailboxes.

SMTP AUTH is the obvious way to send as a Microsoft 365 user, and on a tenant that
left Microsoft's defaults alone it does not work: client submission is switched
off for the whole organisation, and enabling it is a security decision for that
organisation's administrator rather than something a mail connector should ask
of one. Graph's ``sendMail`` needs nothing but the user's own consent.

The message is the very MIME the SMTP sender would have transmitted, handed to
Graph as base64 text. Exchange keeps the Message-ID it is given, files a copy in
Sent Items, and takes the Bcc recipients from a Bcc header -- so, unlike SMTP,
where Bcc travels in the envelope, it is written into the message here, and
Exchange removes it from the copies it delivers.
"""

import asyncio
import base64
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from src.config import settings
from src.email.smtp_sender import OutboundCompositionError, compose_outbound_message
from src.security.provider_tokens import ProviderAuthError, ProviderNotConfigured, access_token_for

logger = logging.getLogger(__name__)

SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"


def uses_graph(config) -> bool:
    """Whether this account sends through Graph rather than SMTP."""
    return getattr(config, "provider", None) == "microsoft" and getattr(config, "auth_type", None) == "oauth2"


def _granted_scopes(token: str) -> set[str] | None:
    """The scopes an access token carries, or None when it is opaque.

    Work and school accounts get a JWT whose ``scp`` claim says what was
    consented to. Personal accounts get an opaque token, which says nothing, and
    nothing is the one thing this must not be read as evidence from.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        segment = parts[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        return set(str(claims.get("scp", "")).split())
    except (ValueError, UnicodeError, AttributeError):
        return None


def _describe_refusal(response: httpx.Response) -> str:
    detail = ""
    try:
        error = response.json().get("error", {})
        detail = f"{error.get('code', '')}: {error.get('message', '')}".strip(": ")
    except (ValueError, AttributeError):
        pass
    detail = " ".join(detail.split())[:300]
    return f"Microsoft Graph refused the message ({response.status_code}{': ' + detail if detail else ''})"


class GraphMailSender:
    """The same contract as EmailSender, over a different wire."""

    def __init__(self, smtp_config, *, http_client: httpx.AsyncClient | None = None):
        self.config = smtp_config
        self._client = http_client
        self._owns_client = http_client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=settings.smtp_connect_timeout_seconds,
                    read=settings.smtp_command_timeout_seconds,
                    write=settings.smtp_command_timeout_seconds,
                    pool=settings.smtp_connect_timeout_seconds,
                )
            )
        return self._client

    async def connect(self) -> bool:
        """Whether this mailbox can obtain a token that is allowed to send."""
        try:
            token = await asyncio.to_thread(access_token_for, self.config, "send")
        except ProviderNotConfigured as exc:
            logger.error("Microsoft sending is not configured for %s: %s", self.config.name, exc)
            return False
        except Exception as exc:
            logger.error("Could not obtain a Microsoft sending token for %s: %s", self.config.name, type(exc).__name__)
            return False
        scopes = _granted_scopes(token)
        if scopes is not None and "Mail.Send" not in scopes:
            logger.error("%s has not granted Mail.Send; connect the mailbox again", self.config.name)
            return False
        return True

    def disconnect(self) -> None:
        # Nothing is held open between sends: each one is a single request.
        return None

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def send_email(
        self,
        to_addresses: List[str],
        subject: str,
        body_text: Optional[str] = None,
        body_html: Optional[str] = None,
        cc_addresses: Optional[List[str]] = None,
        bcc_addresses: Optional[List[str]] = None,
        attachments: Optional[List[Dict]] = None,
        reply_to: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send one message. Never retries after the request has left.

        Everything up to the POST is a provable non-send and is reported as
        ``failed``, so the caller's upload slots come back and an honest retry
        works. From the POST onwards a missing answer is ``unknown``: the request
        may have been processed, and a retry would then deliver it twice.
        """
        try:
            msg = compose_outbound_message(
                self.config,
                to_addresses=to_addresses,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                cc_addresses=cc_addresses,
                attachments=attachments,
                reply_to=reply_to,
                in_reply_to=in_reply_to,
                references=references,
            )
            if bcc_addresses:
                msg["Bcc"] = ", ".join(bcc_addresses)
            body = base64.b64encode(msg.as_bytes())
        except OutboundCompositionError:
            return {
                "success": False,
                "message": "Failed to construct an outbound attachment",
                "delivery_state": "failed",
            }
        except Exception as exc:
            logger.error("Failed to compose a message for %s: %s", self.config.name, exc)
            return {
                "success": False,
                "message": f"Failed to send email via {self.config.name}: {exc}",
                "delivery_state": "failed",
            }

        recipient_count = len(to_addresses) + len(cc_addresses or []) + len(bcc_addresses or [])
        try:
            token = await asyncio.to_thread(access_token_for, self.config, "send")
        except (ProviderAuthError, ProviderNotConfigured) as exc:
            return {"success": False, "message": str(exc), "delivery_state": "failed"}
        except Exception as exc:
            logger.error("Could not obtain a Microsoft sending token for %s: %s", self.config.name, exc)
            return {
                "success": False,
                "message": f"Could not obtain a Microsoft access token for {self.config.name}: {type(exc).__name__}",
                "delivery_state": "failed",
            }

        client = self._http()
        response: httpx.Response | None = None
        try:
            for attempt in range(2):
                try:
                    response = await client.post(
                        SEND_URL,
                        content=body,
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                    # No connection was ever made, so no byte of the message left.
                    logger.error("Could not reach Microsoft Graph for %s: %s", self.config.name, type(exc).__name__)
                    return {
                        "success": False,
                        "message": f"Could not reach Microsoft Graph before sending: {type(exc).__name__}",
                        "delivery_state": "failed",
                    }
                except httpx.HTTPError as exc:
                    # The request was on the wire and nothing says what became of it.
                    logger.error("Microsoft Graph send to %s is unconfirmed: %s", self.config.name, type(exc).__name__)
                    return {
                        "success": False,
                        "message": f"Microsoft Graph result is ambiguous for {self.config.name}: {type(exc).__name__}",
                        "delivery_state": "unknown",
                    }
                if response.status_code == 401 and attempt == 0:
                    # An expired token is refused before anything is processed, so
                    # this is the one retry that cannot send twice.
                    try:
                        token = await asyncio.to_thread(access_token_for, self.config, "send")
                    except Exception as exc:
                        return {
                            "success": False,
                            "message": f"Could not renew the Microsoft access token: {exc}",
                            "delivery_state": "failed",
                        }
                    continue
                break
        finally:
            if self._owns_client:
                await self.close()

        if response is None:
            # Unreachable: the loop either answers or returns. Said anyway, because
            # the safe reading of a request with no recorded outcome is "unknown".
            return {"success": False, "message": "Microsoft Graph gave no answer", "delivery_state": "unknown"}
        if 200 <= response.status_code < 300:
            logger.info("Email sent to %s recipients via %s (Graph)", recipient_count, self.config.name)
            return {
                "success": True,
                "message": f"Email sent to {recipient_count} recipients",
                "recipients": recipient_count,
                "smtp_server": self.config.name,
                "transport": "microsoft_graph",
                "message_id": msg["Message-ID"],
                "delivery_state": "sent",
            }
        if 400 <= response.status_code < 500 and response.status_code != 408:
            # Graph validates and authorises before it accepts, so a 4xx is a
            # refusal, not a message in flight.
            message = _describe_refusal(response)
            if response.status_code == 429 and response.headers.get("Retry-After"):
                message += f"; retry after {response.headers['Retry-After']} seconds"
            logger.error("%s for %s", message, self.config.name)
            return {"success": False, "message": message, "delivery_state": "failed"}
        message = _describe_refusal(response)
        logger.error("%s for %s; delivery is unconfirmed", message, self.config.name)
        return {"success": False, "message": message, "delivery_state": "unknown"}
