"""Add mortality_logs table

Revision ID: 20260518_0025_mortality_log
Revises: 20260517_0024_animal_management
Create Date: 2026-05-18

Creates the mortality_logs table for tracking animal deaths within a group.
Idempotent: safe to run on databases that may already have the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260518_0025_mortality_log"
down_revision: Union[str, None] = "20260517_0024_animal_management"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return table in insp.get_table_names()


def upgrade() -> None:
    if not _table_exists("mortality_logs"):
        op.create_table(
            "mortality_logs",
            sa.Column("id",              sa.Integer(),      primary_key=True),
            sa.Column("animal_group_id", sa.Integer(),      nullable=False),
            sa.Column("death_date",      sa.Date(),         nullable=False),
            sa.Column("count",           sa.Integer(),      nullable=False, server_default="1"),
            sa.Column("cause",           sa.String(30),     nullable=False, server_default="unknown"),
            sa.Column("note",            sa.Text(),         nullable=True),
            sa.Column("user_id",         sa.Integer(),      nullable=True),
            sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["animal_group_id"], ["animal_groups.id"], name="fk_mortality_logs_group"),
            sa.ForeignKeyConstraint(["user_id"],         ["users.id"],         name="fk_mortality_logs_user"),
        )
        op.create_index("ix_mortality_logs_group", "mortality_logs", ["animal_group_id"], unique=False)
        op.create_index("ix_mortality_logs_date",  "mortality_logs", ["death_date"],      unique=False)


def downgrade() -> None:
    if _table_exists("mortality_logs"):
        op.drop_index("ix_mortality_logs_date",  table_name="mortality_logs")
        op.drop_index("ix_mortality_logs_group", table_name="mortality_logs")
        op.drop_table("mortality_logs")