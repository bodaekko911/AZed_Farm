"""Add animal_intake_logs (receive animals)

Revision ID: 20260609_0034_animal_intake
Revises: 20260602_0033_partial_pay
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260609_0034_animal_intake"
down_revision: Union[str, None] = "20260602_0033_partial_pay"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    # Idempotent: skip if the table already exists (e.g. created out of band).
    if _has_table("animal_intake_logs"):
        return
    op.create_table(
        "animal_intake_logs",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("animal_group_id", sa.Integer(), sa.ForeignKey("animal_groups.id"), nullable=False, index=True),
        sa.Column("intake_date", sa.Date(), nullable=False, index=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(length=150), nullable=True),
        sa.Column("unit_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("total_cost", sa.Numeric(14, 2), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("expense_id", sa.Integer(), sa.ForeignKey("expenses.id"), nullable=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    if _has_table("animal_intake_logs"):
        op.drop_table("animal_intake_logs")