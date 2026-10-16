"""Append-only record of outbound send attempts."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from .base import Base


class SendAudit(Base):
    __tablename__ = "send_audits"
    __table_args__ = (UniqueConstraint("owner_user_id", "idempotency_key", name="uq_send_owner_idempotency"),)

    id = Column(Integer, primary_key=True)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Null once the account it was sent from has been deleted: the record of
    # what this server sent on the owner's behalf outlives the account.
    smtp_config_id = Column(Integer, ForeignKey("smtp_configs.id", ondelete="SET NULL"), nullable=True)
    # Indexed because deleting a message makes PostgreSQL find the audits that
    # replied to it; without an index that scans every tenant's audits, once
    # per deleted message.
    reply_to_email_id = Column(
        Integer,
        ForeignKey("email_logs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    idempotency_key = Column(String(255), nullable=False)
    recipients_json = Column(Text, nullable=False)
    subject = Column(Text, nullable=False)
    request_hash = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False)
    provider_message_id = Column(String(768), nullable=True)
    provider_response = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)
