"""Make a mail account deletable.

Three changes, all for delete_mail_account:

send_audits.smtp_config_id becomes nullable with ON DELETE SET NULL. send_audits is
append-only: it records what this server sent on someone's behalf. Its account
reference was ON DELETE RESTRICT, so an account that had ever sent mail could not
be deleted at all. Now the account and everything indexed from it can go while
the record of what it sent stays.

email_attachments.email_log_id and send_audits.reply_to_email_id get indexes.
Both are foreign keys to email_logs without one, so deleting a message made
PostgreSQL scan the whole referencing table -- once per deleted message. Against
a 28,000-message account that cost 45 seconds with 30,000 attachments, and 17
seconds with 20,000 send audits (which belong to every tenant and only grow);
with both indexes the same deletion takes under a second. The deletion holds the
account row for that long, which also stalls anything waiting on that row.

Revision ID: 20261016_17
Revises: 20260922_16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20261016_17"
down_revision = "20260922_16"
branch_labels = None
depends_on = None

AUDITS = "send_audits"
COLUMN = "smtp_config_id"
# The name PostgreSQL gave the constraint when the table was created without
# one, which is also what a fresh install's create_all produces today. Reusing
# it keeps a migrated database and a fresh one identical.
DEFAULT_NAME = "send_audits_smtp_config_id_fkey"

# (table, column, index) -- each name is the one SQLAlchemy gives index=True on
# the model, so a fresh install's create_all and this migration agree.
INDEXES = (
    ("email_attachments", "email_log_id", "ix_email_attachments_email_log_id"),
    ("send_audits", "reply_to_email_id", "ix_send_audits_reply_to_email_id"),
)


def _account_reference(inspector) -> dict | None:
    for foreign_key in inspector.get_foreign_keys(AUDITS):
        if foreign_key["constrained_columns"] == [COLUMN] and foreign_key["referred_table"] == "smtp_configs":
            return foreign_key
    return None


def _rule(foreign_key: dict | None) -> str:
    return ((foreign_key or {}).get("options") or {}).get("ondelete", "").upper()


def _has_index(inspector, table: str, name: str) -> bool:
    return any(index["name"] == name for index in inspector.get_indexes(table))


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if AUDITS in tables:
        column = next(column for column in inspector.get_columns(AUDITS) if column["name"] == COLUMN)
        foreign_key = _account_reference(inspector)
        # A fresh install already built this shape from the model, and a second
        # run of this revision finds it too.
        if not (column["nullable"] and _rule(foreign_key) == "SET NULL"):
            name = (foreign_key or {}).get("name") or DEFAULT_NAME
            with op.batch_alter_table(AUDITS) as batch:
                if foreign_key is not None and foreign_key.get("name"):
                    batch.drop_constraint(foreign_key["name"], type_="foreignkey")
                batch.alter_column(COLUMN, existing_type=sa.Integer(), nullable=True)
                batch.create_foreign_key(name, "smtp_configs", [COLUMN], ["id"], ondelete="SET NULL")

    for table, indexed_column, index in INDEXES:
        if table in tables and not _has_index(inspector, table, index):
            op.create_index(index, table, [indexed_column])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if AUDITS in tables:
        orphaned = bind.execute(sa.text(f"SELECT count(*) FROM {AUDITS} WHERE {COLUMN} IS NULL")).scalar()
        if orphaned:
            # Restoring NOT NULL would mean deleting the records of mail that
            # was really sent. That is a decision for a person, not a downgrade.
            raise RuntimeError(
                f"{orphaned} send audit record(s) belong to deleted accounts; "
                "they would have to be removed before the account reference can be required again"
            )

    for table, _column, index in INDEXES:
        if table in tables and _has_index(inspector, table, index):
            op.drop_index(index, table_name=table)

    if AUDITS in tables:
        foreign_key = _account_reference(inspector)
        name = (foreign_key or {}).get("name") or DEFAULT_NAME
        with op.batch_alter_table(AUDITS) as batch:
            if foreign_key is not None and foreign_key.get("name"):
                batch.drop_constraint(foreign_key["name"], type_="foreignkey")
            batch.alter_column(COLUMN, existing_type=sa.Integer(), nullable=False)
            batch.create_foreign_key(name, "smtp_configs", [COLUMN], ["id"], ondelete="RESTRICT")
