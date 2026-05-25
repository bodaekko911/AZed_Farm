"""Add animal purchase cost and link expenses to animal groups

Revision ID: 20260518_0026_animal_costs
Revises: 20260518_0025_mortality_log
Create Date: 2026-05-18

Adds:
  - animal_groups.purchase_cost      (total amount paid for the whole group)
  - animal_groups.cost_per_head      (optional per-head price; UI may use either)
  - expenses.animal_group_id         (FK -> animal_groups.id, nullable)

Idempotent: safe to re-run on databases that may already have any of these.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260518_0026_animal_costs"
down_revision: Union[str, None] = "20260518_0025_mortality_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def _has_index(table: str, index: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(ix["name"] == index for ix in insp.get_indexes(table))


def _has_fk(table: str, fk_name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(fk.get("name") == fk_name for fk in insp.get_foreign_keys(table))


def upgrade() -> None:
    if context.is_offline_mode():
        return

    # ── animal_groups: purchase cost columns ──
    if not _has_column("animal_groups", "purchase_cost"):
        op.add_column(
            "animal_groups",
            sa.Column("purchase_cost", sa.Numeric(14, 2), nullable=True),
        )
    if not _has_column("animal_groups", "cost_per_head"):
        op.add_column(
            "animal_groups",
            sa.Column("cost_per_head", sa.Numeric(14, 2), nullable=True),
        )

    # ── expenses: link to animal group ──
    if not _has_column("expenses", "animal_group_id"):
        op.add_column(
            "expenses",
            sa.Column("animal_group_id", sa.Integer(), nullable=True),
        )
    # Add FK (Postgres allows naming; sqlite ignores). Wrap in try because some
    # backends won't support adding an FK after the fact without batch ops.
    if not _has_fk("expenses", "fk_expenses_animal_group"):
        try:
            op.create_foreign_key(
                "fk_expenses_animal_group",
                "expenses",
                "animal_groups",
                ["animal_group_id"],
                ["id"],
            )
        except Exception:
            # SQLite / other backends: column exists, FK is best-effort.
            pass
    if not _has_index("expenses", "ix_expenses_animal_group_id"):
        try:
            op.create_index(
                "ix_expenses_animal_group_id",
                "expenses",
                ["animal_group_id"],
                unique=False,
            )
        except Exception:
            pass


def downgrade() -> None:
    if context.is_offline_mode():
        return

    if _has_index("expenses", "ix_expenses_animal_group_id"):
        try:
            op.drop_index("ix_expenses_animal_group_id", table_name="expenses")
        except Exception:
            pass
    if _has_fk("expenses", "fk_expenses_animal_group"):
        try:
            op.drop_constraint("fk_expenses_animal_group", "expenses", type_="foreignkey")
        except Exception:
            pass
    if _has_column("expenses", "animal_group_id"):
        op.drop_column("expenses", "animal_group_id")
    if _has_column("animal_groups", "cost_per_head"):
        op.drop_column("animal_groups", "cost_per_head")
    if _has_column("animal_groups", "purchase_cost"):
        op.drop_column("animal_groups", "purchase_cost")