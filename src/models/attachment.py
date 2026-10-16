"""Email attachment metadata and extracted text."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


class EmailAttachment(Base):
    """Email attachment model for storing attachment metadata and extracted text."""

    __tablename__ = "email_attachments"

    id = Column(Integer, primary_key=True)
    email_log_id = Column(Integer, ForeignKey("email_logs.id"), nullable=False, index=True)

    filename = Column(String(500), nullable=False)
    content_type = Column(String(100), nullable=True)
    claimed_content_type = Column(String(100), nullable=True)
    detected_content_type = Column(String(100), nullable=True)
    content_id = Column(String(255), nullable=True)
    provider_attachment_id = Column(String(500), nullable=True)
    part_index = Column(Integer, nullable=True)
    size = Column(Integer, default=0)
    sha256 = Column(String(64), nullable=True)

    text_content = Column(Text, nullable=True)  # Extracted text stored directly in DB
    extraction_state = Column(String(32), nullable=False, default="pending")
    extraction_error = Column(Text, nullable=True)
    extractor_version = Column(String(32), nullable=True)

    created_at = Column(DateTime, default=func.now())

    email_log = relationship("EmailLog", back_populates="attachments")

    def __repr__(self):
        return f"<EmailAttachment(filename='{self.filename}', size={self.size})>"
