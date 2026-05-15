"""add employee_allowance_advances table

Revision ID: 20260514_0021
Revises: 20260514_0020
Create Date: 2026-05-14
"""
from alembic import op
import sqlalchemy as sa

revision = '20260514_0021'
down_revision = '20260514_0020'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'employee_allowance_advances',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id'), nullable=False, index=True),
        sa.Column('advance_date', sa.Date(), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='open'),
        sa.Column('payroll_id', sa.Integer(), sa.ForeignKey('payroll.id'), nullable=True, index=True),
        sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table('employee_allowance_advances')