"""B2B quotations (price offers): quotations, their lines, and the quotation settings.

Revision ID: 20261016_0055_quotations
Revises: 20261015_0054_automations
Create Date: 2026-10-16

Created defensively (skipped if present) so it coexists with the runtime guard in ``app/app_factory.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261016_0055_quotations"
down_revision = "20261015_0054_automations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "b2b_quotations" not in tables:
        op.create_table(
            "b2b_quotations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("number", sa.String(20), nullable=True),
            sa.Column("client_id", sa.Integer(), sa.ForeignKey("b2b_clients.id"), nullable=True),
            sa.Column("prospect_name", sa.String(200), nullable=True),
            sa.Column("prospect_contact", sa.String(150), nullable=True),
            sa.Column("prospect_phone", sa.String(50), nullable=True),
            sa.Column("prospect_email", sa.String(150), nullable=True),
            sa.Column("prospect_area", sa.String(120), nullable=True),
            sa.Column("quote_date", sa.Date(), nullable=False),
            sa.Column("valid_until", sa.Date(), nullable=False),
            sa.Column("status", sa.String(12), nullable=False, server_default="draft"),
            sa.Column("language", sa.String(2), nullable=False, server_default="en"),
            sa.Column("delivery_area", sa.String(120), nullable=True),
            sa.Column("delivery_day", sa.String(40), nullable=True),
            sa.Column("delivery_fee", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("payment_terms", sa.String(50), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("terms", sa.Text(), nullable=True),
            sa.Column("subtotal", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("discount_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("total", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("weekly_value", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("monthly_value", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("below_cost_approved_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("below_cost_approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_to", sa.String(150), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decided_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("reject_reason", sa.String(300), nullable=True),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
        op.create_index("ix_b2b_quotations_number", "b2b_quotations", ["number"], unique=True)
        op.create_index("ix_b2b_quotations_client_id", "b2b_quotations", ["client_id"])
        op.create_index("ix_b2b_quotations_status", "b2b_quotations", ["status"])
    if "b2b_quotation_items" not in tables:
        op.create_table(
            "b2b_quotation_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("quotation_id", sa.Integer(), sa.ForeignKey("b2b_quotations.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("qty", sa.Numeric(12, 3), nullable=False),
            sa.Column("unit", sa.String(30), nullable=True),
            sa.Column("unit_price", sa.Numeric(14, 2), nullable=False),
            sa.Column("line_discount_pct", sa.Numeric(6, 2), nullable=False, server_default="0"),
            sa.Column("line_total", sa.Numeric(14, 2), nullable=False),
            sa.Column("frequency", sa.String(14), nullable=False, server_default="once"),
            sa.Column("note", sa.String(200), nullable=True),
            sa.Column("unit_cost", sa.Numeric(12, 3), nullable=True),
            sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("ix_b2b_quotation_items_quotation_id", "b2b_quotation_items", ["quotation_id"])
    if "b2b_quotation_settings" not in tables:
        op.create_table(
            "b2b_quotation_settings",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("terms_en", sa.Text(), nullable=True),
            sa.Column("terms_ar", sa.Text(), nullable=True),
            sa.Column("target_margin_pct", sa.Numeric(6, 2), nullable=False, server_default="30"),
            sa.Column("validity_days", sa.Integer(), nullable=False, server_default="14"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )


def downgrade() -> None:
    for table in ("b2b_quotation_items", "b2b_quotations", "b2b_quotation_settings"):
        op.drop_table(table)
