"""support tickets (3 tables) and stored_files.uploaded_by_user_id

1. stored_files.uploaded_by_user_id: VARCHAR(36), nullable, FK users ON DELETE SET NULL,
   indexed. Existing files keep NULL (their uploader was never recorded and is not
   guessed); only files uploaded after this release carry it. Support ticket attachments
   require it to match the attaching user, so older files cannot be attached.
   On PostgreSQL stored_files can be large (it may hold file bytes), so:
     - ADD COLUMN (nullable, no default) only changes the catalog;
     - the foreign key is added NOT VALID, then VALIDATEd (validation takes a lock that
       does not block reads or writes);
     - the index is built CONCURRENTLY outside the transaction, so writes continue.
2. support_tickets, support_ticket_messages, support_ticket_attachments: new, empty tables
   with their indexes. Creating them is instant; their foreign keys briefly lock the
   referenced tables (organizations, users, stored_files) against writes.

Every step checks first, so the revision can be re-run after a partial run. Nothing
existing is rewritten or deleted.

Downgrade drops the three tables only while support_tickets is empty -- it refuses
rather than delete tickets -- then drops the uploader column (only that metadata is lost).

Revision ID: e7f1a2b3c4d5
Revises: d5b8f0c3e2a4
Create Date: 2026-11-13 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f1a2b3c4d5"
down_revision: Union[str, Sequence[str], None] = "d5b8f0c3e2a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK = "fk_stored_files_uploaded_by_user_id"
_IX = "ix_stored_files_uploaded_by_user_id"


def _bind():
    return op.get_bind()


def _pg() -> bool:
    return _bind().dialect.name == "postgresql"


def _tables() -> set[str]:
    return set(sa.inspect(_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(_bind()).get_indexes(table)}


def _fks(table: str) -> set[str]:
    return {fk["name"] for fk in sa.inspect(_bind()).get_foreign_keys(table) if fk.get("name")}


def _index_invalid(name: str) -> bool:
    row = _bind().execute(
        sa.text("SELECT NOT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n"),
        {"n": name},
    ).first()
    return bool(row and row[0])


def _uploader_column() -> None:
    if "uploaded_by_user_id" not in _columns("stored_files"):
        op.add_column("stored_files", sa.Column("uploaded_by_user_id", sa.String(length=36), nullable=True))
    if not _pg():
        if _IX not in _indexes("stored_files"):
            op.create_index(_IX, "stored_files", ["uploaded_by_user_id"])
        return  # SQLite (local dev): the model's ForeignKey is enough there
    if _FK not in _fks("stored_files"):
        op.execute(sa.text(
            f'ALTER TABLE stored_files ADD CONSTRAINT "{_FK}" FOREIGN KEY (uploaded_by_user_id) '
            "REFERENCES users (id) ON DELETE SET NULL NOT VALID"
        ))
        op.execute(sa.text(f'ALTER TABLE stored_files VALIDATE CONSTRAINT "{_FK}"'))
    if _IX in _indexes("stored_files") and not _index_invalid(_IX):
        return
    with op.get_context().autocommit_block():
        op.execute(sa.text(f'DROP INDEX CONCURRENTLY IF EXISTS "{_IX}"'))
        op.execute(sa.text(f'CREATE INDEX CONCURRENTLY "{_IX}" ON stored_files (uploaded_by_user_id)'))


def _support_tables() -> None:
    tables = _tables()
    if "support_tickets" not in tables:
        op.create_table(
            "support_tickets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("ticket_number", sa.String(30), nullable=False),
            sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("created_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("subject", sa.String(200), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("category", sa.String(30), nullable=False),
            sa.Column("priority", sa.String(20), nullable=False, server_default="medium"),
            sa.Column("status", sa.String(30), nullable=False, server_default="open"),
            sa.Column("assigned_to_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("ticket_number", name="uq_support_tickets_ticket_number"),
        )
    existing = _indexes("support_tickets")
    for name, cols in (
        ("ix_support_tickets_organization_id", ["organization_id"]),
        ("ix_support_tickets_created_by_user_id", ["created_by_user_id"]),
        ("ix_support_tickets_assigned_to_user_id", ["assigned_to_user_id"]),
        ("ix_support_tickets_last_activity_at", ["last_activity_at"]),
        ("ix_support_tickets_org_status", ["organization_id", "status"]),
        ("ix_support_tickets_status_priority", ["status", "priority"]),
    ):
        if name not in existing:
            op.create_index(name, "support_tickets", cols)

    if "support_ticket_messages" not in tables:
        op.create_table(
            "support_ticket_messages",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("ticket_id", sa.String(36), sa.ForeignKey("support_tickets.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("author_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("author_type", sa.String(20), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "ix_support_ticket_messages_ticket_id" not in _indexes("support_ticket_messages"):
        op.create_index("ix_support_ticket_messages_ticket_id", "support_ticket_messages", ["ticket_id"])

    if "support_ticket_attachments" not in tables:
        op.create_table(
            "support_ticket_attachments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("ticket_id", sa.String(36), sa.ForeignKey("support_tickets.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("message_id", sa.String(36),
                      sa.ForeignKey("support_ticket_messages.id", ondelete="CASCADE"), nullable=True),
            sa.Column("stored_file_id", sa.String(36), sa.ForeignKey("stored_files.id", ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("stored_file_id", name="uq_support_ticket_attachments_stored_file"),
        )
    existing = _indexes("support_ticket_attachments")
    for name, cols in (
        ("ix_support_ticket_attachments_ticket_id", ["ticket_id"]),
        ("ix_support_ticket_attachments_message_id", ["message_id"]),
    ):
        if name not in existing:
            op.create_index(name, "support_ticket_attachments", cols)


def upgrade() -> None:
    _support_tables()
    _uploader_column()


def downgrade() -> None:
    if "support_tickets" in _tables():
        count = _bind().execute(sa.text("SELECT count(*) FROM support_tickets")).scalar()
        if count:
            raise RuntimeError(
                f"support_tickets holds {count} ticket(s); refusing to drop them. Export or remove them "
                "deliberately first. Nothing was changed."
            )
    for table in ("support_ticket_attachments", "support_ticket_messages", "support_tickets"):
        if table in _tables():
            op.drop_table(table)
    if "uploaded_by_user_id" in _columns("stored_files"):
        if _pg():
            if _IX in _indexes("stored_files"):
                with op.get_context().autocommit_block():
                    op.execute(sa.text(f'DROP INDEX CONCURRENTLY IF EXISTS "{_IX}"'))
            if _FK in _fks("stored_files"):
                op.drop_constraint(_FK, "stored_files", type_="foreignkey")
            op.drop_column("stored_files", "uploaded_by_user_id")
        else:
            if _IX in _indexes("stored_files"):
                op.drop_index(_IX, table_name="stored_files")
            with op.batch_alter_table("stored_files") as batch:
                batch.drop_column("uploaded_by_user_id")
