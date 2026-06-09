"""Add intake_type to animal_intake_logs (purchase/birth/transfer)

Revision ID: 20260609_0035_intake_type
Revises: 20260609_0034_animal_intake
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260609_0035_intake_type"
down_revision: Union[str, None] = "20260609_0034_animal_intake"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    # Idempotent: only add the column if it isn't already there.
    if not _has_column("animal_intake_logs", "intake_type"):
        op.add_column(
            "animal_intake_logs",
            sa.Column("intake_type", sa.String(length=20), nullable=False, server_default="purchase"),
        )


def downgrade() -> None:
    if _has_column("animal_intake_logs", "intake_type"):
        op.drop_column("animal_intake_logs", "intake_type")