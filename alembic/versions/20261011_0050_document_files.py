"""Document images read by Ask, kept as evidence.

Revision ID: 20261011_0050_document_files
Revises: 20261010_0049_weekly_brief
Create Date: 2026-10-11

``document_files`` holds the image(s) of each paper document Ask read — the
resized JPEG the model saw — and ``document_links`` ties a document to the
record(s) made from it (an expense, receive lines, a farm delivery). Images are
stored in the database so they are backed up with the records and served only
through a logged-in route. Created defensively (skipped if present) so it
coexists with the runtime guard in ``app/app_factory.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261011_0050_document_files"
down_revision = "20261010_0049_weekly_brief"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "document_files" not in tables:
        op.create_table(
            "document_files",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("doc_key", sa.String(36), nullable=False),
            sa.Column("page_no", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("kind", sa.String(30), nullable=False),
            sa.Column("content_type", sa.String(40), nullable=False, server_default="image/jpeg"),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("data", sa.LargeBinary(), nullable=False),
            sa.Column("filename", sa.String(150), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
        op.create_index("ix_document_files_doc_key", "document_files", ["doc_key"])
    if "document_links" not in tables:
        op.create_table(
            "document_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("doc_key", sa.String(36), nullable=False),
            sa.Column("ref_type", sa.String(30), nullable=False),
            sa.Column("ref_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
        op.create_index("ix_document_links_doc_key", "document_links", ["doc_key"])
        op.create_index("ix_document_links_ref", "document_links", ["ref_type", "ref_id"])


def downgrade() -> None:
    op.drop_table("document_links")
    op.drop_table("document_files")
