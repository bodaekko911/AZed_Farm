"""Animal groups can feed a greenhouse / field: fish in the sandponic system (SPC).

Revision ID: 20261022_0061_group_plot
Revises: 20261021_0060_farm_plots
Create Date: 2026-10-22

``animal_groups.plot_id`` — the bed a group's waste feeds. Its feeding logs then show as that bed's "fish feed" input
and are counted, at the feed's cost price, in the season cost of the bed's farm.
"""
from alembic import context, op
import sqlalchemy as sa

revision = "20261022_0061_group_plot"
down_revision = "20261021_0060_farm_plots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("animal_groups") and "plot_id" not in {c["name"] for c in inspector.get_columns("animal_groups")}:
        op.add_column("animal_groups",
                      sa.Column("plot_id", sa.Integer(), sa.ForeignKey("farm_plots.id"), nullable=True))
        op.create_index("ix_animal_groups_plot_id", "animal_groups", ["plot_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("animal_groups") and "plot_id" in {c["name"] for c in inspector.get_columns("animal_groups")}:
        op.drop_index("ix_animal_groups_plot_id", table_name="animal_groups")
        op.drop_column("animal_groups", "plot_id")
