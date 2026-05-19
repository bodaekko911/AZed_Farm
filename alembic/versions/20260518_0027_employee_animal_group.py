"""Link employees to an animal group (for payroll cost allocation)

Revision ID: 20260518_0027_emp_animal
Revises: 20260518_0026_animal_costs
Create Date: 2026-05-18

Adds employees.animal_group_id (nullable FK to animal_groups.id). When a
payroll is paid out, the auto-generated salary expense will inherit this
value so the Animals → Analyze tab includes labor cost for that group.

Idempotent: safe to re-run.
"""
from typing import Sequence, Union

from alembic import op
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
    if not _has_column("employees", "animal_group_id"):
        op.add_column(
            "employees",
            sa.Column("animal_group_id", sa.Integer(), nullable=True),
        )
    if not _has_fk("employees", "fk_employees_animal_group"):
        try:
            op.create_foreign_key(
                "fk_employees_animal_group",
                "employees",
                "animal_groups",
                ["animal_group_id"],
                ["id"],
            )
        except Exception:
            # SQLite / other backends: column exists, FK is best-effort.
            pass
    if not _has_index("employees", "ix_employees_animal_group_id"):
        try:
            op.create_index(
                "ix_employees_animal_group_id",
                "employees",
                ["animal_group_id"],
                unique=False,
            )
        except Exception:
            pass


def downgrade() -> None:
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
        op.drop_column("employees", "animal_group_id")