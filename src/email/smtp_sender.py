"""SMTP email sending functionality."""

import base64
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from typing import Dict, List, Optional, Union

from src.database.connection import get_db_session
from src.email.draft_builder import OutboundPart, plan_outbound_parts
from src.models.smtp_config import SMTPConfig

logger = logging.getLogger(__name__)


class EmailSender:
    """SMTP client for sending emails."""

    def __init__(self, smtp_config: SMTPConfig):
        self.config = smtp_config
        self._server = None

    async def connect(self) -> bool:
        """Connect to SMTP server."""
        try:
            # Create SMTP connection using smtp_host/smtp_port if available, otherwise fall back to host/port
            smtp_host = getattr(self.config, "smtp_host", None) or self.config.host
            smtp_port = getattr(self.config, "smtp_port", self.config.port)

            # Use specific SMTP SSL/TLS settings
            smtp_use_ssl = getattr(self.config, "smtp_use_ssl", False)
            smtp_use_tls = getattr(self.config, "smtp_use_tls", True)

            if smtp_use_ssl:
                # Use SSL connection (typically port 465)
                self._server = smtplib.SMTP_SSL(
                    smtp_host, smtp_port, timeout=10, context=ssl.create_default_context()
                )
            else:
                if not smtp_use_tls:
                    raise ValueError("Plaintext SMTP is disabled; configure SSL or STARTTLS")
                # Use regular connection with optional TLS (typically port 587)
                self._server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
                if smtp_use_tls:
                    context = ssl.create_default_context()
                    self._server.starttls(context=context)

            # Login
            if getattr(self.config, "auth_type", "password") == "oauth2":
                from src.security.provider_tokens import refresh_access_token

                token = refresh_access_token(self.config.credential_ciphertext)
                auth = f"user={self.config.username}\x01auth=Bearer {token}\x01\x01"
                encoded = base64.b64encode(auth.encode()).decode()
                code, response = self._server.docmd("AUTH", f"XOAUTH2 {encoded}")
                if code != 235:
                    raise smtplib.SMTPAuthenticationError(code, response)
            else:
                self._server.login(self.config.username, self.config.password)
            logger.info("Connected to SMTP server %s", self.config.name)
            return True

        except Exception as e:
            logger.error("Failed to connect to SMTP server %s: %s", self.config.name, e)
            return False

    def disconnect(self):
        """Disconnect from SMTP server."""
        if self._server:
            try:
                self._server.quit()
                logger.info("Disconnected from SMTP server %s", self.config.name)
            except Exception as e:
                logger.error("Error disconnecting from SMTP server %s: %s", self.config.name, e)
            finally:
                self._server = None

    @staticmethod
    def _build_part(part: OutboundPart, *, inline: bool) -> MIMEBase:
        """Render one already-validated part.

        Every value here has been through ``plan_outbound_parts``: the type is
        two safe tokens, the Content-ID is a bare token we bracket ourselves,
        and the filename came out of ``sanitize_filename``. Nothing a caller
        supplied is interpolated into a header as-is.
        """
        mime_part = MIMEBase(part.maintype, part.subtype)
        mime_part.set_payload(part.data)
        encoders.encode_base64(mime_part)
        if inline:
            # Content-ID is what a cid: URL resolves against; the inline
            # disposition is what clients that ignore Content-ID look at.
            # Real clients emit both, so both are sent.
            mime_part.add_header("Content-ID", f"<{part.content_id}>")
        mime_part.add_header(
            "Content-Disposition",
            "inline" if inline else "attachment",
            filename=part.filename,
        )
        return mime_part

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
    ) -> Dict[str, Union[bool, str]]:
        """
        Send an email with optional attachments.

        Args:
            to_addresses: List of recipient email addresses
            subject: Email subject
            body_text: Plain text body (optional)
            body_html: HTML body (optional)
            cc_addresses: CC recipients (optional)
            bcc_addresses: BCC recipients (optional)
            attachments: List of attachment dicts with 'data' and 'filename' keys
            reply_to: Reply-to address (optional)
            in_reply_to: Message ID this is a reply to (for threading)
            references: References header for email threading

        Returns:
            Dict with 'success' bool and 'message' string
        """
        if not self._server and not await self.connect():
            return {"success": False, "message": "Failed to connect to SMTP server"}

        try:
            # Decide the structure before building anything: whether a related
            # level is needed depends on which parts the HTML actually uses.
            try:
                inline_parts, attachment_parts = plan_outbound_parts(attachments, body_html)
            except Exception as e:
                logger.error("Error planning outbound attachments: %s", e)
                return {
                    "success": False,
                    "message": "Failed to construct an outbound attachment",
                    "delivery_state": "failed",
                }

            # Create body container
            body_container = MIMEMultipart("alternative")

            # Add text body
            if body_text:
                text_part = MIMEText(body_text, "plain", "utf-8")
                body_container.attach(text_part)

            # Add HTML body
            if body_html:
                html_part = MIMEText(body_html, "html", "utf-8")
                body_container.attach(html_part)

            # If no body provided, add default
            if not body_text and not body_html:
                text_part = MIMEText("", "plain", "utf-8")
                body_container.attach(text_part)

            rendered_inline = []
            rendered_attachments = []
            for part, is_inline in [(p, True) for p in inline_parts] + [(p, False) for p in attachment_parts]:
                try:
                    (rendered_inline if is_inline else rendered_attachments).append(
                        self._build_part(part, inline=is_inline)
                    )
                    logger.debug("Added %s part: %s", "inline" if is_inline else "attachment", part.filename)
                except Exception as e:
                    logger.error("Error adding attachment %s: %s", part.filename, e)
                    return {
                        "success": False,
                        "message": "Failed to construct an outbound attachment",
                        "delivery_state": "failed",
                    }

            if rendered_inline:
                # The body and the parts its cid: URLs point at have to share a
                # multipart/related, or the references cannot resolve.
                #
                # type= is RFC 2387's required parameter naming the root part --
                # the one a client should render and resolve the cid: URLs
                # against. Apple Mail and Outlook both emit it; modern clients
                # tolerate its absence, but matching real-client output exactly
                # is the point. body_container is always the first child, so
                # the declared type is read off it rather than hardcoded.
                related = MIMEMultipart("related", type=body_container.get_content_type())
                related.attach(body_container)
                for part in rendered_inline:
                    related.attach(part)
                root = related
            else:
                root = body_container

            if rendered_attachments:
                msg = MIMEMultipart("mixed")
                msg.attach(root)
                for part in rendered_attachments:
                    msg.attach(part)
            elif rendered_inline:
                # No ordinary attachments: a mixed level with one child buys
                # nothing, so the related container is the message.
                msg = root
            else:
                # Unchanged from before inline parts existed: a mixed wrapper
                # around the alternative body, which is what already ships.
                msg = MIMEMultipart("mixed")
                msg.attach(body_container)

            # Set headers - use account_name if available, otherwise username
            from_email = getattr(self.config, "account_name", self.config.username)
            if not from_email or "@" not in from_email:
                from_email = self.config.username  # fallback
            msg["From"] = from_email
            msg["To"] = ", ".join(to_addresses)
            msg["Subject"] = subject
            msg["Date"] = datetime.now(tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
            msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1])

            if cc_addresses:
                msg["Cc"] = ", ".join(cc_addresses)
            if reply_to:
                msg["Reply-To"] = reply_to
            if in_reply_to:
                msg["In-Reply-To"] = in_reply_to
            if references:
                msg["References"] = references

            # Collect all recipients
            all_recipients = to_addresses[:]
            if cc_addresses:
                all_recipients.extend(cc_addresses)
            if bcc_addresses:
                all_recipients.extend(bcc_addresses)

            # Send email
            refused = self._server.send_message(msg, to_addrs=all_recipients)
            if refused:
                refused_addresses = sorted(refused)
                logger.error(
                    "SMTP partially refused %s of %s recipients via %s",
                    len(refused_addresses),
                    len(all_recipients),
                    self.config.name,
                )
                return {
                    "success": False,
                    "message": "SMTP accepted only part of the recipient set",
                    "delivery_state": "partial",
                    "message_id": msg["Message-ID"],
                    "refused_recipients": refused_addresses,
                }

            recipient_count = len(all_recipients)
            logger.info("Email sent successfully to %s recipients via %s", recipient_count, self.config.name)

            return {
                "success": True,
                "message": f"Email sent to {recipient_count} recipients",
                "recipients": recipient_count,
                "smtp_server": self.config.name,
                "message_id": msg["Message-ID"],
                "delivery_state": "sent",
            }

        except (TimeoutError, smtplib.SMTPServerDisconnected) as e:
            error_msg = f"SMTP result is ambiguous via {self.config.name}: {e}"
            logger.error(error_msg)
            self.disconnect()
            return {"success": False, "message": error_msg, "delivery_state": "unknown"}
        except Exception as e:
            error_msg = f"Failed to send email via {self.config.name}: {e}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg, "delivery_state": "failed"}

    async def send_template_email(
        self, template_name: str, to_addresses: List[str], template_data: Dict, subject: Optional[str] = None, **kwargs
    ) -> Dict[str, Union[bool, str]]:
        """
        Send email using a template.

        Args:
            template_name: Name of the template
            to_addresses: Recipient addresses
            template_data: Data to fill template placeholders
            subject: Email subject (can include template variables)
            **kwargs: Additional send_email arguments
        """
        try:
            # Simple template substitution (in production, use proper template engine)
            body_text = template_data.get("body_text", "")
            body_html = template_data.get("body_html", "")

            # Replace placeholders in templates
            for key, value in template_data.items():
                placeholder = f"{{{key}}}"
                if body_text:
                    body_text = body_text.replace(placeholder, str(value))
                if body_html:
                    body_html = body_html.replace(placeholder, str(value))
                if subject:
                    subject = subject.replace(placeholder, str(value))

            # Send the email
            return await self.send_email(
                to_addresses=to_addresses,
                subject=subject or "Email from Email Server",
                body_text=body_text if body_text else None,
                body_html=body_html if body_html else None,
                **kwargs,
            )

        except Exception as e:
            error_msg = f"Failed to send template email: {e}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.disconnect()


class EmailSenderManager:
    """Manages multiple SMTP senders."""

    def __init__(self):
        self._senders = {}

    async def get_sender(self, smtp_config: SMTPConfig) -> EmailSender:
        """Get or create email sender for config."""
        sender_key = smtp_config.id

        if sender_key not in self._senders:
            self._senders[sender_key] = EmailSender(smtp_config)
        else:
            sender = self._senders[sender_key]
            old_connection = (
                sender.config.smtp_host,
                sender.config.smtp_port,
                sender.config.username,
                sender.config.credential_ciphertext,
                sender.config.auth_type,
            )
            new_connection = (
                smtp_config.smtp_host,
                smtp_config.smtp_port,
                smtp_config.username,
                smtp_config.credential_ciphertext,
                smtp_config.auth_type,
            )
            if old_connection != new_connection:
                sender.disconnect()
            sender.config = smtp_config

        return self._senders[sender_key]

    async def invalidate(self, smtp_config_id: int) -> None:
        sender = self._senders.pop(smtp_config_id, None)
        if sender:
            sender.disconnect()

    async def send_email_via_config(
        self, smtp_config_id: int, owner_user_id: int | None = None, **email_args
    ) -> Dict[str, Union[bool, str]]:
        """Send email using specific SMTP configuration."""
        try:
            with get_db_session() as db:
                query = db.query(SMTPConfig).filter(SMTPConfig.id == smtp_config_id)
                if owner_user_id is not None:
                    query = query.filter(SMTPConfig.owner_user_id == owner_user_id)
                config = query.first()
                if not config:
                    return {"success": False, "message": f"SMTP config {smtp_config_id} not found"}

                if not config.enabled:
                    return {"success": False, "message": f"SMTP config {config.name} is disabled"}

                # Create a detached config object outside the session
                temp_config = SMTPConfig.create_detached(config)

            sender = await self.get_sender(temp_config)
            return await sender.send_email(**email_args)

        except Exception as e:
            error_msg = f"Error sending email via config {smtp_config_id}: {e}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def cleanup(self):
        """Disconnect all senders."""
        for sender in self._senders.values():
            sender.disconnect()
        self._senders.clear()


# Global sender manager instance
email_sender_manager = EmailSenderManager()
