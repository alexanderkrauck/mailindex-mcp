"""A write grant for bytes an AI client cannot hand over in conversation.

The client asks for a slot, PUTs the bytes to a signed URL, then names the slot
when sending. The payload lives on the data volume, so a database dump stays a
dump of mail metadata; this row only records where it went and what arrived.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from .base import Base


def _now() -> datetime:
    """UTC, in the shape the naive DateTime columns here hold.

    These columns are naive ``DateTime``, and every read of one -- the expiry
    comparisons, the lease comparisons, the sweep -- happens in that frame. An
    aware value written to one is adapted by psycopg2 as a timestamptz which
    Postgres then casts through the *session* ``TimeZone``, so the value stored
    depends on a server setting rather than on this code. Producing the right
    shape at the source is what keeps writes and reads in one frame.
    """
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


class OutboundUpload(Base):
    __tablename__ = "outbound_uploads"

    id = Column(Integer, primary_key=True)
    # Server-generated and unguessable: it is both the lookup key and the only
    # thing that ever names a file on disk, so a client filename cannot steer a path.
    upload_id = Column(String(64), unique=True, nullable=False, index=True)
    owner_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Already sanitized on the way in; used for the MIME part, never for the path.
    filename = Column(String(500), nullable=False)
    declared_size = Column(Integer, nullable=True)
    declared_sha256 = Column(String(64), nullable=True)
    # What actually arrived, as opposed to what the client promised.
    size = Column(Integer, nullable=True)
    # The running total of a body still arriving, written as it streams. Without
    # it a slot in 'receiving' is charged only its declaration, so concurrent
    # uploads are invisible to each other and the per-user byte budget binds
    # against promises rather than against bytes on the volume.
    received_bytes = Column(Integer, nullable=False, default=0, server_default="0")
    sha256 = Column(String(64), nullable=True)
    detected_content_type = Column(String(100), nullable=True)
    storage_path = Column(String(1024), nullable=True)
    # pending -> receiving -> stored -> sending -> consumed. Only a successful
    # send consumes, so a failed send leaves the slot usable for a retry. Plus
    # one terminal side exit: a 'sending' claim whose holder died without
    # reporting becomes 'send_unknown', because nothing here can tell a message
    # the MTA accepted from one that never left, and guessing means sending
    # confidential mail twice.
    state = Column(String(16), nullable=False, default="pending")
    # The in-flight states carry a lease, because neither a PUT nor a send can
    # be relied on to finish: a worker killed between claim and promotion would
    # otherwise hold the slot -- and its share of the owner's byte and slot
    # budgets -- until the 900s TTL, refusing the owner's own honest retry.
    lease_expires_at = Column(DateTime, nullable=True)
    # Who holds the current in-flight claim. A state predicate identifies a
    # state, not a holder: 'receiving' is satisfied by *any* receiver, so a
    # displaced attempt whose lease was reclaimed could go on renewing -- and
    # promoting -- over the slot its successor now owns. Every write that
    # extends or ends a claim matches this as well as the state, so exactly one
    # attempt can ever act on one claim.
    lease_token = Column(String(32), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<OutboundUpload(upload_id='{self.upload_id}', state='{self.state}')>"
