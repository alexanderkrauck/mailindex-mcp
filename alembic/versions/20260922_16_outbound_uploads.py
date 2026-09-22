"""Record outbound upload slots so attachment bytes never cross the conversation.

An AI client cannot hand the server a file; it asks for a slot, PUTs the bytes to
a signed URL, then names the slot when sending. The payload itself lives on the
data volume, so this table stays metadata a backup can safely carry.

Revision ID: 20260922_16
Revises: 20260802_15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260922_16"
down_revision = "20260802_15"
branch_labels = None
depends_on = None

TABLE = "outbound_uploads"

# Columns added while this revision was still unreleased. Held here rather than
# in a follow-up revision because nothing has ever run the original form, and an
# existing deployment is impossible by construction; the add-if-absent below is
# what keeps this safe against a table an earlier run of *this* revision built.
_LATE_COLUMNS = (
    sa.Column("received_bytes", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("lease_expires_at", sa.DateTime()),
    sa.Column("lease_token", sa.String(32)),
)


def _ensure_late_columns(inspector) -> None:
    """Add the columns a partially-applied earlier run of this revision lacks.

    ``create_all`` on a fresh install already builds the full table, and the
    early return above skips the create entirely in that case, so this is the
    only path by which those two columns reach a table that predates them.
    """
    present = {column["name"] for column in inspector.get_columns(TABLE)}
    for column in _LATE_COLUMNS:
        if column.name not in present:
            op.add_column(TABLE, column.copy())


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    # A fresh install already built this table via Base.metadata.create_all.
    if TABLE in set(inspector.get_table_names()):
        _ensure_late_columns(inspector)
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        # Uniqueness is carried by the unique index below, exactly as
        # Base.metadata.create_all builds it from unique=True + index=True. A
        # UNIQUE constraint here as well would leave a migrated deployment with a
        # redundant second index a fresh install does not have.
        sa.Column("upload_id", sa.String(64), nullable=False),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("declared_size", sa.Integer()),
        sa.Column("declared_sha256", sa.String(64)),
        sa.Column("size", sa.Integer()),
        # The running total of a body still arriving. A slot in 'receiving' is
        # charged this, so concurrent uploads can see each other's growth
        # instead of each being charged only what it declared.
        sa.Column("received_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64)),
        sa.Column("detected_content_type", sa.String(100)),
        # An absolute path on the data volume. The bytes are deliberately not here.
        sa.Column("storage_path", sa.String(1024)),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        # When the current in-flight claim ('receiving' or 'sending') lapses and
        # may be reclaimed. Null in every settled state.
        sa.Column("lease_expires_at", sa.DateTime()),
        # Which attempt holds that claim. A state predicate alone would let a
        # displaced holder go on renewing and promoting over its successor.
        sa.Column("lease_token", sa.String(32)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
    )
    op.create_index(f"ix_{TABLE}_upload_id", TABLE, ["upload_id"], unique=True)
    op.create_index(f"ix_{TABLE}_owner_user_id", TABLE, ["owner_user_id"])


def downgrade() -> None:
    # The payload files are swept by the service, not by the schema.
    if TABLE in set(inspect(op.get_bind()).get_table_names()):
        op.drop_table(TABLE)
