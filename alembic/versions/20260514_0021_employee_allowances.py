"""add food and transportation allowance to employees

Revision ID: 20260514_0020
Revises: 20260514_0019
Create Date: 2026-05-14
"""
from alembic import op
import sqlalchemy as sa

revision = '20260514_0020'
down_revision = '20260514_0019'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('employees', sa.Column('food_allowance', sa.Numeric(12, 2), nullable=False, server_default='0'))
    op.add_column('employees', sa.Column('transportation_allowance', sa.Numeric(12, 2), nullable=False, server_default='0'))


def downgrade():
    op.drop_column('employees', 'transportation_allowance')
    op.drop_column('employees', 'food_allowance')