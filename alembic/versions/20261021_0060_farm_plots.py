"""Greenhouses & fields under farms: what goes in (seeds, fertilizer) and what comes out.

Revision ID: 20261021_0060_farm_plots
Revises: 20261020_0059_unit_words
Create Date: 2026-10-21

* ``farm_plots`` — a greenhouse or field on a farm.
* ``plot_inputs`` — seeds / fertilizer / pesticide applied to a plot, either taken from inventory
  (product + storage, stock deducted) or typed in by name with its cost.
* ``farm_delivery_items.plot_id`` — the plot a harvested line came from; drives plot production.

Anyone whose stored permissions grant ``page_farm`` gets the new tab and the two actions, unless
one is already explicitly revoked — same rule as the animal tabs in 0057.
"""
from alembic import context, op
import sqlalchemy as sa

revision = "20261021_0060_farm_plots"
down_revision = "20261020_0059_unit_words"
branch_labels = None
depends_on = None

PLOT_PERMISSIONS = (
    "tab_farm_plots",
    "action_farm_plot_manage",
    "action_farm_plot_input",
)


def _with_plot_permissions(raw: str | None) -> str | None:
    if not raw:
        return raw
    tokens = [t.strip() for t in raw.split(",") if t and t.strip()]
    if "page_farm" not in tokens:
        return raw
    present = set(tokens)
    added = [t for t in PLOT_PERMISSIONS if t not in present and f"-{t}" not in present]
    if not added:
        return raw
    return ",".join(sorted(present | set(added)))


def upgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("farm_plots"):
        op.create_table(
            "farm_plots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("farm_id", sa.Integer(), sa.ForeignKey("farms.id"), nullable=False),
            sa.Column("name", sa.String(150), nullable=False),
            sa.Column("plot_type", sa.String(20), nullable=False, server_default="greenhouse"),
            sa.Column("area_m2", sa.Numeric(12, 2), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Integer(), server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_farm_plots_id", "farm_plots", ["id"])
        op.create_index("ix_farm_plots_farm_id", "farm_plots", ["farm_id"])

    if not inspector.has_table("plot_inputs"):
        op.create_table(
            "plot_inputs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plot_id", sa.Integer(), sa.ForeignKey("farm_plots.id"), nullable=False),
            sa.Column("input_type", sa.String(20), nullable=False, server_default="seed"),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
            sa.Column("location_id", sa.Integer(), sa.ForeignKey("stock_locations.id"), nullable=True),
            sa.Column("item_name", sa.String(200), nullable=False),
            sa.Column("qty", sa.Numeric(14, 4), nullable=False),
            sa.Column("unit", sa.String(30), nullable=True),
            sa.Column("total_cost", sa.Numeric(14, 2), nullable=True),
            sa.Column("crop", sa.String(150), nullable=True),
            sa.Column("applied_on", sa.Date(), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_plot_inputs_id", "plot_inputs", ["id"])
        op.create_index("ix_plot_inputs_plot_id", "plot_inputs", ["plot_id"])
        op.create_index("ix_plot_inputs_product_id", "plot_inputs", ["product_id"])
        op.create_index("ix_plot_inputs_applied_on", "plot_inputs", ["applied_on"])

    if inspector.has_table("farm_delivery_items"):
        cols = {c["name"] for c in inspector.get_columns("farm_delivery_items")}
        if "plot_id" not in cols:
            op.add_column(
                "farm_delivery_items",
                sa.Column("plot_id", sa.Integer(), sa.ForeignKey("farm_plots.id"), nullable=True),
            )
            op.create_index("ix_farm_delivery_items_plot_id", "farm_delivery_items", ["plot_id"])

    if inspector.has_table("users") and "permissions" in {c["name"] for c in inspector.get_columns("users")}:
        rows = bind.execute(sa.text("SELECT id, permissions FROM users")).mappings().all()
        for row in rows:
            updated = _with_plot_permissions(row["permissions"])
            if updated != row["permissions"]:
                bind.execute(
                    sa.text("UPDATE users SET permissions = :permissions WHERE id = :id"),
                    {"id": row["id"], "permissions": updated},
                )


def downgrade() -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("farm_delivery_items"):
        if "plot_id" in {c["name"] for c in inspector.get_columns("farm_delivery_items")}:
            op.drop_index("ix_farm_delivery_items_plot_id", table_name="farm_delivery_items")
            op.drop_column("farm_delivery_items", "plot_id")
    if inspector.has_table("plot_inputs"):
        op.drop_table("plot_inputs")
    if inspector.has_table("farm_plots"):
        op.drop_table("farm_plots")
    # Unknown permissions are ignored when read, so the extra tokens are harmless.
