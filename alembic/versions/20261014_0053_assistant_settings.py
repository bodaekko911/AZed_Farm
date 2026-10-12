"""Assistant settings: which AI provider to use (auto, the first only, the fallback only).

Revision ID: 20261014_0053_assistant_settings
Revises: 20261013_0052_tg_approvers
Create Date: 2026-10-14

Created defensively (skipped if present) so it coexists with the runtime guard in ``app/app_factory.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261014_0053_assistant_settings"
down_revision = "20261013_0052_tg_approvers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "assistant_settings" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "assistant_settings",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("provider_mode", sa.String(10), nullable=False, server_default="auto"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("assistant_settings")
