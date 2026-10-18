"""Fish tanks in sandponic beds (SPC), apart from the Animals module.

Revision ID: 20261023_0062_fish_tanks
Revises: 20261022_0061_group_plot
Create Date: 2026-10-23

* ``fish_tanks`` — a tank in a greenhouse / field, with its fish count.
* ``fish_logs`` — feed (from stock), fish added, deaths and harvests.
* ``animal_groups.plot_id`` (0061) is dropped: fish are no longer kept as an animal group linked to a bed.
"""
from alembic import context, op
import sqlalchemy as sa

revision = "20261023_0062_fish_tanks"
down_revision = "20261022_0061_group_plot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("animal_groups") and "plot_id" in {c["name"] for c in inspector.get_columns("animal_groups")}:
        if "ix_animal_groups_plot_id" in {i["name"] for i in inspector.get_indexes("animal_groups")}:
            op.drop_index("ix_animal_groups_plot_id", table_name="animal_groups")
        op.drop_column("animal_groups", "plot_id")

    if not inspector.has_table("fish_tanks"):
        op.create_table(
            "fish_tanks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plot_id", sa.Integer(), sa.ForeignKey("farm_plots.id"), nullable=False),
            sa.Column("name", sa.String(150), nullable=False),
            sa.Column("species", sa.String(100), nullable=True),
            sa.Column("fish_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Integer(), server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_fish_tanks_id", "fish_tanks", ["id"])
        op.create_index("ix_fish_tanks_plot_id", "fish_tanks", ["plot_id"])

    if not inspector.has_table("fish_logs"):
        op.create_table(
            "fish_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tank_id", sa.Integer(), sa.ForeignKey("fish_tanks.id"), nullable=False),
            sa.Column("log_type", sa.String(20), nullable=False),
            sa.Column("log_date", sa.Date(), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
            sa.Column("location_id", sa.Integer(), sa.ForeignKey("stock_locations.id"), nullable=True),
            sa.Column("qty", sa.Numeric(14, 4), nullable=True),
            sa.Column("total_cost", sa.Numeric(14, 2), nullable=True),
            sa.Column("fish_count", sa.Integer(), nullable=True),
            sa.Column("weight_kg", sa.Numeric(12, 3), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_fish_logs_id", "fish_logs", ["id"])
        op.create_index("ix_fish_logs_tank_id", "fish_logs", ["tank_id"])
        op.create_index("ix_fish_logs_log_date", "fish_logs", ["log_date"])
        op.create_index("ix_fish_logs_product_id", "fish_logs", ["product_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("fish_logs"):
        op.drop_table("fish_logs")
    if inspector.has_table("fish_tanks"):
        op.drop_table("fish_tanks")
    if inspector.has_table("animal_groups") and "plot_id" not in {c["name"] for c in inspector.get_columns("animal_groups")}:
        op.add_column("animal_groups", sa.Column("plot_id", sa.Integer(), sa.ForeignKey("farm_plots.id"), nullable=True))
        op.create_index("ix_animal_groups_plot_id", "animal_groups", ["plot_id"])
