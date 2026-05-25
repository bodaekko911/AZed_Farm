"""Add works_with_animals flag to employees

Revision ID: 20260518_0027_emp_animal
Revises: 20260518_0026_animal_costs
Create Date: 2026-05-18

Adds employees.works_with_animals (boolean, default False). The HR
Employee modal's Farm dropdown gets a "🐾 Animals" option appended; when
picked, the employee is saved with farm_id=NULL and works_with_animals=True.
On payroll payment, that flag causes the auto-generated salary expense
to be tagged is_animal_expense=True, so it lands under "🐾 Animals" in
the expense list and rolls into the combined Animals analysis.

Also cleans up `employees.animal_group_id` if an earlier draft of this
migration ever applied it.

Idempotent: safe to re-run on databases that may already have any of
these columns / FKs / indexes.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260518_0027_emp_animal"
down_revision: Union[str, None] = "20260518_0026_animal_costs"
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

    # ── Add the toggle ──
    if not _has_column("employees", "works_with_animals"):
        op.add_column(
            "employees",
            sa.Column(
                "works_with_animals",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    # ── Clean up any prior animal_group_id column on employees ──
    if _has_index("employees", "ix_employees_animal_group_id"):
        try:
            op.drop_index("ix_employees_animal_group_id", table_name="employees")
        except Exception:
            pass
    if _has_fk("employees", "fk_employees_animal_group"):
        try:
            op.drop_constraint("fk_employees_animal_group", "employees", type_="foreignkey")
        except Exception:
            pass
    if _has_column("employees", "animal_group_id"):
        try:
            op.drop_column("employees", "animal_group_id")
        except Exception:
            pass


def downgrade() -> None:
    if context.is_offline_mode():
        return

    if _has_column("employees", "works_with_animals"):
        op.drop_column("employees", "works_with_animals")