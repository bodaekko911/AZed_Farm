"""Add sex breakdown (male/female counts) and birth date to animal groups.

Revision ID: 20260630_0040_animal_sex_and_birth
Revises: 20260629_0039_product_categories
Create Date: 2026-06-30

Adds three nullable columns to ``animal_groups``:
  • male_count   — optional count of male animals (informational; does not
                   drive headcount)
  • female_count — optional count of female animals (informational)
  • birth_date   — birth / hatch date of the cohort; drives the auto age
                   display on the Animals page

Each column is added defensively (skipped if it already exists) so this
migration is safe even when the runtime schema guard in
``app/core/migrations.py`` has already created the columns at boot.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260630_0040_animal_sex_and_birth"
down_revision = "20260629_0039_product_categories"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("male_count", sa.Integer(), True),
    ("female_count", sa.Integer(), True),
    ("birth_date", sa.Date(), True),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "animal_groups" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("animal_groups")}
    for name, col_type, nullable in _COLUMNS:
        if name not in existing:
            op.add_column(
                "animal_groups",
                sa.Column(name, col_type, nullable=nullable),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "animal_groups" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("animal_groups")}
    for name, _col_type, _nullable in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("animal_groups", name)