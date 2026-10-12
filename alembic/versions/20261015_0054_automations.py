"""Automations: rules, detected events, the run log and the e-mail cap.

Revision ID: 20261015_0054_automations
Revises: 20261014_0053_assistant_settings
Create Date: 2026-10-15

Created defensively (skipped if present) so it coexists with the runtime guard in ``app/app_factory.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261015_0054_automations"
down_revision = "20261014_0053_assistant_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "automation_rules" not in tables:
        op.create_table(
            "automation_rules",
            sa.Column("key", sa.String(40), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("trigger", sa.String(10), nullable=False, server_default="daily"),
            sa.Column("send_time", sa.String(5), nullable=False, server_default="07:00"),
            sa.Column("weekday", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("month_day", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("settings", sa.Text(), nullable=True),
            sa.Column("recipients", sa.Text(), nullable=True),
            sa.Column("language", sa.String(2), nullable=False, server_default="en"),
            sa.Column("digest_minutes", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("watermark", sa.Integer(), nullable=True),
            sa.Column("last_period", sa.String(20), nullable=True),
            sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_status", sa.String(300), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
    if "automation_events" not in tables:
        op.create_table(
            "automation_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("rule_key", sa.String(40), nullable=False),
            sa.Column("ref", sa.String(80), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("urgent", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("held", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("run_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.UniqueConstraint("rule_key", "ref", name="uq_automation_event_ref"),
        )
        for col in ("rule_key", "run_id", "created_at"):
            op.create_index(f"ix_automation_events_{col}", "automation_events", [col])
    if "automation_runs" not in tables:
        op.create_table(
            "automation_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("rule_key", sa.String(40), nullable=False),
            sa.Column("period", sa.String(20), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(12), nullable=False, server_default="running"),
            sa.Column("items", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("recipients", sa.Text(), nullable=True),
            sa.Column("error", sa.String(300), nullable=True),
        )
        for col in ("rule_key", "started_at"):
            op.create_index(f"ix_automation_runs_{col}", "automation_runs", [col])
    if "automation_settings" not in tables:
        op.create_table(
            "automation_settings",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("daily_cap", sa.Integer(), nullable=False, server_default="40"),
            sa.Column("gmail_limit_day", sa.Date(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )


def downgrade() -> None:
    for table in ("automation_settings", "automation_runs", "automation_events", "automation_rules"):
        op.drop_table(table)
