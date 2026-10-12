"""Telegram field bot, phase 2: managers approve from Telegram.

Revision ID: 20261013_0052_tg_approvers
Revises: 20261012_0051_telegram
Create Date: 2026-10-13

* telegram_chats.approver — this chat gets new submissions to approve (chosen by the admin per chat);
* telegram_chats.pending_reject_id — the draft whose rejection reason the bot is waiting for;
* field_drafts.notices — the approval messages sent for a draft, closed once it is decided.

Added defensively (skipped if present) so it coexists with the runtime guard in ``app/app_factory.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261013_0052_tg_approvers"
down_revision = "20261012_0051_telegram"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    chats = _columns("telegram_chats")
    if "approver" not in chats:
        op.add_column("telegram_chats", sa.Column("approver", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "pending_reject_id" not in chats:
        op.add_column("telegram_chats", sa.Column("pending_reject_id", sa.Integer(), nullable=True))
    if "notices" not in _columns("field_drafts"):
        op.add_column("field_drafts", sa.Column("notices", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("field_drafts", "notices")
    op.drop_column("telegram_chats", "pending_reject_id")
    op.drop_column("telegram_chats", "approver")
