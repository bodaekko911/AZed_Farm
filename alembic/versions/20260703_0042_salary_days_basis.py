"""Add per-employee salary days basis (calendar vs fixed 30-day deals).

Revision ID: 20260703_0042_salary_days_basis
Revises: 20260630_0041_intake_sex
Create Date: 2026-07-03

Adds ``employees.salary_days_basis``:
  • 'calendar' (default) — daily rate = salary ÷ actual days in the month;
    pay accrues per covered day (existing behaviour).
  • 'fixed_30' — daily rate = salary ÷ 30 flat; the full monthly salary is
    owed and each uncovered day deducts salary/30 (deduction-based deal, so
    full attendance in February still pays the full salary).

Added defensively (skipped if present) so it coexists with the runtime
schema guard in ``app/core/migrations.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260703_0042_salary_days_basis"
down_revision = "20260630_0041_intake_sex"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "employees" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("employees")}
    if "salary_days_basis" not in existing:
        op.add_column(
            "employees",
            sa.Column("salary_days_basis", sa.String(12), nullable=False,
                      server_default="calendar"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "employees" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("employees")}
    if "salary_days_basis" in existing:
        op.drop_column("employees", "salary_days_basis")