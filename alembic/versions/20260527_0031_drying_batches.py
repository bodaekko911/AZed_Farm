"""Add drying_batches, drying_batch_inputs, drying_batch_outputs, drying_batch_spoilage tables

Revision ID: 20260527_0031_drying_batches
Revises: 20260518_0030_merge
Create Date: 2026-05-27

Creates the four tables needed for the Drying Batch module:

  • drying_batches        — the main batch record (stateful, multi-day)
  • drying_batch_inputs   — raw materials loaded into a batch
  • drying_batch_outputs  — finished products from a completed batch
  • drying_batch_spoilage — spoilage events logged during a batch

Stock invariant: inputs deduct on start, outputs credit on complete,
cancel refunds inputs. Spoilage deducts stock at log time.

Idempotent: checks table existence before creating so it is safe to run
on databases that may already have partial state.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260527_0031_drying_batches"
down_revision: Union[str, None] = "20260518_0030_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return table in insp.get_table_names()


def upgrade() -> None:
    if context.is_offline_mode():
        return

    if not _table_exists("drying_batches"):
        op.create_table(
            "drying_batches",
            sa.Column("id",                 sa.Integer(),              primary_key=True),
            sa.Column("batch_number",       sa.String(length=30),      nullable=False),
            sa.Column("status",             sa.String(length=20),      nullable=False, server_default="in_progress"),
            sa.Column("started_at",         sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("completed_at",       sa.DateTime(timezone=True), nullable=True),
            sa.Column("cancelled_at",       sa.DateTime(timezone=True), nullable=True),
            sa.Column("expected_yield_pct", sa.Numeric(5, 2),          nullable=True),
            sa.Column("actual_yield_pct",   sa.Numeric(5, 2),          nullable=True),
            sa.Column("notes",              sa.Text(),                  nullable=True),
            sa.Column("started_by_id",      sa.Integer(),              nullable=False),
            sa.Column("completed_by_id",    sa.Integer(),              nullable=True),
            sa.ForeignKeyConstraint(["started_by_id"],   ["users.id"], name="fk_drying_batches_started_by"),
            sa.ForeignKeyConstraint(["completed_by_id"], ["users.id"], name="fk_drying_batches_completed_by"),
            sa.UniqueConstraint("batch_number", name="uq_drying_batches_batch_number"),
        )
        op.create_index("ix_drying_batches_id",           "drying_batches", ["id"],           unique=False)
        op.create_index("ix_drying_batches_batch_number", "drying_batches", ["batch_number"], unique=True)
        op.create_index("ix_drying_batches_status",       "drying_batches", ["status"],       unique=False)

    if not _table_exists("drying_batch_inputs"):
        op.create_table(
            "drying_batch_inputs",
            sa.Column("id",         sa.Integer(),      primary_key=True),
            sa.Column("batch_id",   sa.Integer(),      nullable=False),
            sa.Column("product_id", sa.Integer(),      nullable=False),
            sa.Column("qty",        sa.Numeric(12, 3), nullable=False),
            sa.ForeignKeyConstraint(["batch_id"],   ["drying_batches.id"], name="fk_drying_inputs_batch",
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["product_id"], ["products.id"],       name="fk_drying_inputs_product"),
        )
        op.create_index("ix_drying_batch_inputs_id",       "drying_batch_inputs", ["id"],       unique=False)
        op.create_index("ix_drying_batch_inputs_batch_id", "drying_batch_inputs", ["batch_id"], unique=False)

    if not _table_exists("drying_batch_outputs"):
        op.create_table(
            "drying_batch_outputs",
            sa.Column("id",         sa.Integer(),      primary_key=True),
            sa.Column("batch_id",   sa.Integer(),      nullable=False),
            sa.Column("product_id", sa.Integer(),      nullable=False),
            sa.Column("qty",        sa.Numeric(12, 3), nullable=False),
            sa.ForeignKeyConstraint(["batch_id"],   ["drying_batches.id"], name="fk_drying_outputs_batch",
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["product_id"], ["products.id"],       name="fk_drying_outputs_product"),
        )
        op.create_index("ix_drying_batch_outputs_id",       "drying_batch_outputs", ["id"],       unique=False)
        op.create_index("ix_drying_batch_outputs_batch_id", "drying_batch_outputs", ["batch_id"], unique=False)

    if not _table_exists("drying_batch_spoilage"):
        op.create_table(
            "drying_batch_spoilage",
            sa.Column("id",           sa.Integer(),              primary_key=True),
            sa.Column("batch_id",     sa.Integer(),              nullable=False),
            sa.Column("product_id",   sa.Integer(),              nullable=False),
            sa.Column("qty",          sa.Numeric(12, 3),         nullable=False),
            sa.Column("reason",       sa.String(length=50),      nullable=False),
            sa.Column("detail",       sa.Text(),                 nullable=True),
            sa.Column("logged_at",    sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("logged_by_id", sa.Integer(),              nullable=False),
            sa.ForeignKeyConstraint(["batch_id"],     ["drying_batches.id"], name="fk_drying_spoilage_batch",
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["product_id"],   ["products.id"],       name="fk_drying_spoilage_product"),
            sa.ForeignKeyConstraint(["logged_by_id"], ["users.id"],          name="fk_drying_spoilage_logged_by"),
        )
        op.create_index("ix_drying_batch_spoilage_id",       "drying_batch_spoilage", ["id"],       unique=False)
        op.create_index("ix_drying_batch_spoilage_batch_id", "drying_batch_spoilage", ["batch_id"], unique=False)


def downgrade() -> None:
    if context.is_offline_mode():
        return

    if _table_exists("drying_batch_spoilage"):
        op.drop_index("ix_drying_batch_spoilage_batch_id", table_name="drying_batch_spoilage")
        op.drop_index("ix_drying_batch_spoilage_id",       table_name="drying_batch_spoilage")
        op.drop_table("drying_batch_spoilage")

    if _table_exists("drying_batch_outputs"):
        op.drop_index("ix_drying_batch_outputs_batch_id", table_name="drying_batch_outputs")
        op.drop_index("ix_drying_batch_outputs_id",       table_name="drying_batch_outputs")
        op.drop_table("drying_batch_outputs")

    if _table_exists("drying_batch_inputs"):
        op.drop_index("ix_drying_batch_inputs_batch_id", table_name="drying_batch_inputs")
        op.drop_index("ix_drying_batch_inputs_id",       table_name="drying_batch_inputs")
        op.drop_table("drying_batch_inputs")

    if _table_exists("drying_batches"):
        op.drop_index("ix_drying_batches_status",       table_name="drying_batches")
        op.drop_index("ix_drying_batches_batch_number", table_name="drying_batches")
        op.drop_index("ix_drying_batches_id",           table_name="drying_batches")
        op.drop_table("drying_batches")
