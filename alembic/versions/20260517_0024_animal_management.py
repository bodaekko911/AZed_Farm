"""Add animal_groups and feeding_logs tables (animal management stage 1)

Revision ID: 20260517_0024_animal_management
Revises: 20260517_0023_receipt_location
Create Date: 2026-05-17

Creates the two tables needed for stage 1 of the Animal Management module:

  • animal_groups — herds/flocks/pens
  • feeding_logs  — records of feed consumed by groups

Idempotent: checks table existence before creating so it is safe to run
on databases that may already have partial state.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "20260517_0024_animal_management"
down_revision: Union[str, None] = "20260517_0023_receipt_location"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return table in insp.get_table_names()


def upgrade() -> None:
    if context.is_offline_mode():
        return

    if not _table_exists("animal_groups"):
        op.create_table(
            "animal_groups",
            sa.Column("id",          sa.Integer(), primary_key=True),
            sa.Column("name",        sa.String(length=150), nullable=False),
            sa.Column("animal_type", sa.String(length=30),  nullable=False, server_default="other"),
            sa.Column("headcount",   sa.Integer(),          nullable=False, server_default="0"),
            sa.Column("farm_id",     sa.Integer(),          nullable=True),
            sa.Column("status",      sa.String(length=20),  nullable=False, server_default="active"),
            sa.Column("notes",       sa.Text(),             nullable=True),
            sa.Column("created_at",  sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], name="fk_animal_groups_farm_id"),
        )
        op.create_index("ix_animal_groups_name",    "animal_groups", ["name"],    unique=False)
        op.create_index("ix_animal_groups_farm_id", "animal_groups", ["farm_id"], unique=False)

    if not _table_exists("feeding_logs"):
        op.create_table(
            "feeding_logs",
            sa.Column("id",              sa.Integer(),       primary_key=True),
            sa.Column("animal_group_id", sa.Integer(),       nullable=False),
            sa.Column("product_id",      sa.Integer(),       nullable=False),
            sa.Column("location_id",     sa.Integer(),       nullable=False),
            sa.Column("qty",             sa.Numeric(14, 4),  nullable=False),
            sa.Column("feed_date",       sa.Date(),          nullable=False),
            sa.Column("note",            sa.Text(),          nullable=True),
            sa.Column("user_id",         sa.Integer(),       nullable=True),
            sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["animal_group_id"], ["animal_groups.id"], name="fk_feeding_logs_group"),
            sa.ForeignKeyConstraint(["product_id"],      ["products.id"],      name="fk_feeding_logs_product"),
            sa.ForeignKeyConstraint(["location_id"],     ["stock_locations.id"], name="fk_feeding_logs_location"),
            sa.ForeignKeyConstraint(["user_id"],         ["users.id"],         name="fk_feeding_logs_user"),
        )
        op.create_index("ix_feeding_logs_group",    "feeding_logs", ["animal_group_id"], unique=False)
        op.create_index("ix_feeding_logs_product",  "feeding_logs", ["product_id"],      unique=False)
        op.create_index("ix_feeding_logs_location", "feeding_logs", ["location_id"],     unique=False)
        op.create_index("ix_feeding_logs_date",     "feeding_logs", ["feed_date"],       unique=False)


def downgrade() -> None:
    if context.is_offline_mode():
        return

    if _table_exists("feeding_logs"):
        op.drop_index("ix_feeding_logs_date",     table_name="feeding_logs")
        op.drop_index("ix_feeding_logs_location", table_name="feeding_logs")
        op.drop_index("ix_feeding_logs_product",  table_name="feeding_logs")
        op.drop_index("ix_feeding_logs_group",    table_name="feeding_logs")
        op.drop_table("feeding_logs")
    if _table_exists("animal_groups"):
        op.drop_index("ix_animal_groups_farm_id", table_name="animal_groups")
        op.drop_index("ix_animal_groups_name",    table_name="animal_groups")
        op.drop_table("animal_groups")