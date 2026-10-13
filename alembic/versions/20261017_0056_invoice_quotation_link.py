"""B2B invoices remember the quotation they came from.

Revision ID: 20261017_0056_invoice_quote
Revises: 20261016_0055_quotations
Create Date: 2026-10-17

Added defensively (skipped if present) so it coexists with the runtime guard in ``app/app_factory.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261017_0056_invoice_quote"
down_revision = "20261016_0055_quotations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("b2b_invoices")}
    if "quotation_id" not in columns:
        with op.batch_alter_table("b2b_invoices") as batch:
            batch.add_column(sa.Column("quotation_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_b2b_invoices_quotation_id", "b2b_quotations", ["quotation_id"], ["id"])
        op.create_index("ix_b2b_invoices_quotation_id", "b2b_invoices", ["quotation_id"])


def downgrade() -> None:
    op.drop_index("ix_b2b_invoices_quotation_id", table_name="b2b_invoices")
    with op.batch_alter_table("b2b_invoices") as batch:
        batch.drop_constraint("fk_b2b_invoices_quotation_id", type_="foreignkey")
        batch.drop_column("quotation_id")
