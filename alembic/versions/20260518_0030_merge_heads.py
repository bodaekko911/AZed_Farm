"""Merge alembic heads - carbon_footprint branch + animal-expense branch

Revision ID: 20260518_0030_merge
Revises: 20260518_0028_is_animal_exp, 20260510_0018_carbon_footprint
Create Date: 2026-05-18

Background:
  When the carbon-footprint feature was added on 2026-05-10, its migration
  was based on revision `eadb1eb64495`. The next migration (supplier AP,
  2026-05-13) was based on the OTHER merge head `a5ad9f786549` and never
  pulled the carbon-footprint chain forward. The result was two parallel
  Alembic heads with no merge, which causes `alembic upgrade head` to
  refuse to run (it does not know which head to advance to).

  All the column additions that the original docstring warned about
  (vacation_days_per_month, food_allowance, transportation_allowance,
  is_animal_expense, works_with_animals, animal cost columns) have since
  been applied by their own individual migrations (0019_emp_vacation,
  0020_emp_allowances, 0027_emp_animal, 0028_is_animal_exp). This file
  is now a pure no-op merge that joins the carbon-footprint head with
  the current animal-expense head so the chain has a single head again.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "20260518_0030_merge"
down_revision: Union[str, Sequence[str], None] = (
    "20260518_0028_is_animal_exp",
    "20260510_0018_carbon_footprint",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Pure merge - no schema changes.
    pass


def downgrade() -> None:
    # Pure merge - no schema changes.
    pass