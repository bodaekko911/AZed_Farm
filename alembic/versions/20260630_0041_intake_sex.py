"""Add sex split (male/female counts) to animal intake logs.

Revision ID: 20260630_0041_intake_sex
Revises: 20260630_0040_animal_sex_and_birth
Create Date: 2026-06-30

Adds two nullable columns to ``animal_intake_logs``:
  • male_count   — optional count of males in a received batch
  • female_count — optional count of females in a received batch

When set, these roll into the parent group's male_count / female_count
(mirroring how ``count`` rolls into headcount) and are reversed when an
intake is undone.

Both columns are added defensively (skipped if already present) so this
migration is safe even when the runtime schema guard in
``app/core/migrations.py`` has already created them at boot.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260630_0041_intake_sex"
down_revision = "20260630_0040_animal_sex_and_birth"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("male_count", sa.Integer(), True),
    ("female_count", sa.Integer(), True),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "animal_intake_logs" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("animal_intake_logs")}
    for name, col_type, nullable in _COLUMNS:
        if name not in existing:
            op.add_column(
                "animal_intake_logs",
                sa.Column(name, col_type, nullable=nullable),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "animal_intake_logs" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("animal_intake_logs")}
    for name, _col_type, _nullable in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("animal_intake_logs", name)