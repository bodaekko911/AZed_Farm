"""Weekly and monthly brief settings.

Revision ID: 20261010_0049_weekly_brief
Revises: 20261008_0048_sale_line_unit_cost
Create Date: 2026-10-10

One-row table for the weekly brief e-mailed to stakeholders: on/off, the day
and time it goes out, recipients, whether to add the short AI summary, and the
week/status of the last send (which stops a second send for the same week).
Created defensively (skipped if present) so it coexists with the runtime guard
in ``app/app_factory.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261010_0049_weekly_brief"
down_revision = "20261008_0048_sale_line_unit_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "weekly_brief_settings" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "weekly_brief_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("send_weekday", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("send_time", sa.String(5), nullable=False, server_default="09:00"),
        sa.Column("recipients", sa.Text(), nullable=False, server_default=""),
        sa.Column("include_ai_summary", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_sent_week", sa.String(10), nullable=True),
        sa.Column("monthly_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("monthly_day", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_sent_month", sa.String(7), nullable=True),
        sa.Column("last_status", sa.String(300), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("weekly_brief_settings")
