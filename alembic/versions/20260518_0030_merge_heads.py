"""Merge alembic heads — carbon_footprint branch + main branch

Revision ID: 20260518_0030_merge
Revises: 20260518_0029_hotfix_cols, 20260510_0018_carbon_footprint
Create Date: 2026-05-18

Background:
  When the carbon-footprint feature was added on 2026-05-10, its migration
  was based on revision `eadb1eb64495`. The next migration (supplier AP,
  2026-05-13) was based on the OTHER merge head `a5ad9f786549` and never
  pulled the carbon-footprint chain forward. The result: two parallel
  Alembic heads with no merge, which causes `alembic upgrade head` to
  refuse to run (it doesn't know which head to advance to).

  This silent failure has been blocking ALL migrations since 2026-05-14,
  which is why the database is missing vacation_days_per_month,
  food_allowance, transportation_allowance, animal_group_id,
  is_animal_expense, works_with_animals, and the animal cost columns.

  This migration is a no-op merge that joins both heads. Once applied,
  the chain has a single head again and future `alembic upgrade head`
  calls will work normally.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "20260518_0030_merge"
down_revision: Union[str, Sequence[str], None] = (
    "20260518_0029_hotfix_cols",
    "20260510_0018_carbon_footprint",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Pure merge — no schema changes.
    pass


def downgrade() -> None:
    # Pure merge — no schema changes.
    pass