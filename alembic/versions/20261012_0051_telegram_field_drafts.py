"""Telegram field bot: linked chats, link codes, the update queue and field drafts.

Revision ID: 20261012_0051_telegram
Revises: 20261011_0050_document_files
Create Date: 2026-10-12

Workers send photos and short texts to a Telegram bot; each becomes a field
draft a manager confirms or rejects in the Field inbox. Created defensively
(skipped if present) so it coexists with the runtime guard in
``app/app_factory.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261012_0051_telegram"
down_revision = "20261011_0050_document_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "telegram_chats" not in tables:
        op.create_table(
            "telegram_chats",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("username", sa.String(64), nullable=True),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("allowed_kinds", sa.String(200), nullable=False, server_default=""),
            sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("language", sa.String(2), nullable=False, server_default="ar"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("linked_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("linked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
        op.create_index("ix_telegram_chats_chat_id", "telegram_chats", ["chat_id"], unique=True)
        op.create_index("ix_telegram_chats_employee_id", "telegram_chats", ["employee_id"])
        op.create_index("ix_telegram_chats_user_id", "telegram_chats", ["user_id"])
    if "telegram_link_codes" not in tables:
        op.create_table(
            "telegram_link_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code_hash", sa.String(64), nullable=False),
            sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("allowed_kinds", sa.String(200), nullable=False, server_default=""),
            sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("language", sa.String(2), nullable=False, server_default="ar"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("used_by_chat_id", sa.BigInteger(), nullable=True),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
        op.create_index("ix_telegram_link_codes_code_hash", "telegram_link_codes", ["code_hash"], unique=True)
    if "telegram_chat_guard" not in tables:
        op.create_table(
            "telegram_chat_guard",
            sa.Column("chat_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("wrong_codes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("told_unlinked", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "telegram_updates" not in tables:
        op.create_table(
            "telegram_updates",
            sa.Column("update_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=True),
            sa.Column("media_group_id", sa.String(64), nullable=True),
            sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
            sa.Column("claim", sa.String(32), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("payload", sa.Text(), nullable=True),
            sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        )
        for col in ("chat_id", "media_group_id", "status", "claim", "received_at"):
            op.create_index(f"ix_telegram_updates_{col}", "telegram_updates", [col])
    if "field_drafts" not in tables:
        op.create_table(
            "field_drafts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("kind", sa.String(30), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="awaiting_worker"),
            sa.Column("draft_json", sa.Text(), nullable=False),
            sa.Column("doc_key", sa.String(36), nullable=True),
            sa.Column("source_text", sa.Text(), nullable=True),
            sa.Column("worker_note", sa.Text(), nullable=True),
            sa.Column("warnings", sa.Text(), nullable=True),
            sa.Column("model_call", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("bot_message_id", sa.BigInteger(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("sent_for_review_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reviewed_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reject_reason", sa.String(300), nullable=True),
            sa.Column("result_refs", sa.String(300), nullable=True),
            sa.Column("images_deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
        for col in ("chat_id", "status", "doc_key", "created_at"):
            op.create_index(f"ix_field_drafts_{col}", "field_drafts", [col])


def downgrade() -> None:
    for table in ("field_drafts", "telegram_updates", "telegram_chat_guard", "telegram_link_codes", "telegram_chats"):
        op.drop_table(table)
