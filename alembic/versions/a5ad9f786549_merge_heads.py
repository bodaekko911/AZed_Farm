"""merge_heads

Revision ID: a5ad9f786549
Revises: 20260507_0017, 20260510_0018_carbon_footprint
Create Date: 2026-05-10 16:53:59.267233

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5ad9f786549'
down_revision: Union[str, None] = ('20260507_0017', '20260510_0018_carbon_footprint')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
