"""Drying multi-stage rewrite — drops v1 tables, creates stage tables.

DESTROYS: drying_batch_inputs, drying_batch_outputs, all DRY-0001 test data,
and associated stock_moves. User must manually restore 56000g Organic Celery
stock after applying this migration.

Revision ID: 20260527_0032_drying_multistage
Revises:     20260527_0031_drying_batches
Create Date: 2026-05-27
"""
from alembic import op
import sqlalchemy as sa
from alembic import context

# revision identifiers
revision      = "20260527_0032_drying_multistage"
down_revision = "20260527_0031_drying_batches"
branch_labels = None
depends_on    = None


def upgrade():
    if context.is_offline_mode():
        return

    # 1. Clean up DRY-0001 stock moves and all test batch data
    op.execute("DELETE FROM stock_moves WHERE ref_type IN ('drying_batch', 'drying_batch_spoilage')")
    op.execute("DELETE FROM drying_batch_spoilage")
    op.execute("DELETE FROM drying_batch_outputs")
    op.execute("DELETE FROM drying_batch_inputs")
    op.execute("DELETE FROM drying_batches")

    # 2. Drop the old child tables
    op.drop_table("drying_batch_outputs")
    op.drop_table("drying_batch_inputs")

    # 3. Drop denormalized yield columns from drying_batches
    op.drop_column("drying_batches", "expected_yield_pct")
    op.drop_column("drying_batches", "actual_yield_pct")

    # 4. Create drying_batch_stages
    op.create_table(
        "drying_batch_stages",
        sa.Column("id",           sa.Integer(), primary_key=True, index=True),
        sa.Column("batch_id",     sa.Integer(),
                  sa.ForeignKey("drying_batches.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("stage_number", sa.Integer(), nullable=False),
        sa.Column("label",        sa.String(80)),
        sa.Column("notes",        sa.Text()),
        sa.Column("logged_at",    sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("logged_by_id", sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("total_input_qty",      sa.Numeric(12, 3)),
        sa.Column("total_output_qty",     sa.Numeric(12, 3)),
        sa.Column("stage_loss_pct",       sa.Numeric(5, 2)),
        sa.Column("cumulative_yield_pct", sa.Numeric(5, 2)),
    )

    # 5. Create drying_batch_stage_inputs
    op.create_table(
        "drying_batch_stage_inputs",
        sa.Column("id",         sa.Integer(), primary_key=True, index=True),
        sa.Column("stage_id",   sa.Integer(),
                  sa.ForeignKey("drying_batch_stages.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("product_id", sa.Integer(),
                  sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty",        sa.Numeric(12, 3), nullable=False),
    )

    # 6. Create drying_batch_stage_outputs
    op.create_table(
        "drying_batch_stage_outputs",
        sa.Column("id",         sa.Integer(), primary_key=True, index=True),
        sa.Column("stage_id",   sa.Integer(),
                  sa.ForeignKey("drying_batch_stages.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("product_id", sa.Integer(),
                  sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty",        sa.Numeric(12, 3), nullable=False),
    )


def downgrade():
    if context.is_offline_mode():
        return

    raise NotImplementedError(
        "Downgrade from drying_multistage is not supported — "
        "it would destroy all stage data with no recovery path. "
        "Restore from a database backup instead."
    )
