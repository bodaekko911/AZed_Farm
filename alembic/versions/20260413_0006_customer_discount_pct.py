"""add customer discount percentage

Revision ID: 20260413_0006
Revises: 20260413_0005
Create Date: 2026-04-13 20:40:00

Idempotent: on a fresh-from-zero database, `customers.discount_pct` is
already created by `0001_initial_schema`. On older databases that pre-date
that schema consolidation, this migration is what adds the column. The
existence check below makes the migration safe to apply in either case.

In offline mode (`alembic upgrade --sql`), there is no real database to
inspect, so we skip the column-add path entirely and only emit the
deterministic UPDATE. This mirrors the pattern in 0002_runtime_alignment.
"""

from alembic import context, op
import sqlalchemy as sa


revision = "20260413_0006"
down_revision = "20260413_0005"
branch_labels = None
depends_on = None


def _column_exists(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if context.is_offline_mode():
        # No live DB to inspect. Skip the conditional column-add; emit
        # only the deterministic UPDATE for the SQL script output.
        op.execute("UPDATE customers SET discount_pct = 0 WHERE discount_pct IS NULL")
        return

    connection = op.get_bind()
    inspector = sa.inspect(connection)

    if inspector.has_table("customers") and not _column_exists(inspector, "customers", "discount_pct"):
        op.add_column(
            "customers",
            sa.Column("discount_pct", sa.Numeric(precision=6, scale=2), nullable=True, server_default="0"),
        )

    # Re-inspect after potential add so the column-presence check is current.
    inspector = sa.inspect(connection)
    if inspector.has_table("customers") and _column_exists(inspector, "customers", "discount_pct"):
        connection.execute(sa.text("UPDATE customers SET discount_pct = 0 WHERE discount_pct IS NULL"))
        op.alter_column("customers", "discount_pct", server_default=None)


def downgrade() -> None:
    if context.is_offline_mode():
        # Cannot safely emit a conditional DROP in offline mode.
        return

    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if inspector.has_table("customers") and _column_exists(inspector, "customers", "discount_pct"):
        op.drop_column("customers", "discount_pct")