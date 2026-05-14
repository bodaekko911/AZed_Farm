"""add vacation_days_per_month to employees

Revision ID: 20260514_0019
Revises: 20260507_0017_hr_loans_and_deductions
Create Date: 2026-05-14
"""
from alembic import op
import sqlalchemy as sa

revision = '20260514_0019'
down_revision = '20260507_0017'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'employees',
        sa.Column('vacation_days_per_month', sa.Integer(), nullable=False,
                  server_default='0')
    )


def downgrade():
    op.drop_column('employees', 'vacation_days_per_month')