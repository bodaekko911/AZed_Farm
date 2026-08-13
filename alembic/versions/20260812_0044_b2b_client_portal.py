"""B2B client portal — shareable read-only statement link per client.

Revision ID: 20260812_0044_b2b_client_portal
Revises: 20260724_0043_consignment_sales
Create Date: 2026-08-12

Adds the columns behind the client-facing portal link on ``b2b_clients``:

  • portal_token          — the secret in the URL (unique, indexed, nullable)
  • portal_enabled        — kill switch; revoking clears the token and unsets this
  • portal_created_at     — when the current link was issued
  • portal_last_viewed_at — last time the client opened it
  • portal_view_count     — how many times it has been opened

Added defensively (skipped if present) so it coexists with the runtime schema
guard in ``app/app_factory.py``.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260812_0044_b2b_client_portal"
down_revision = "20260724_0043_consignment_sales"
branch_labels = None
depends_on = None


COLUMNS = (
    ("portal_token", sa.Column("portal_token", sa.String(64), nullable=True)),
    ("portal_enabled", sa.Column("portal_enabled", sa.Boolean(), server_default=sa.false())),
    ("portal_created_at", sa.Column("portal_created_at", sa.DateTime(timezone=True), nullable=True)),
    ("portal_last_viewed_at", sa.Column("portal_last_viewed_at", sa.DateTime(timezone=True), nullable=True)),
    ("portal_view_count", sa.Column("portal_view_count", sa.Integer(), server_default="0")),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "b2b_clients" not in set(inspector.get_table_names()):
        return

    existing = {c["name"] for c in inspector.get_columns("b2b_clients")}
    for name, column in COLUMNS:
        if name not in existing:
            op.add_column("b2b_clients", column)

    index_names = {i["name"] for i in inspector.get_indexes("b2b_clients")}
    if "ix_b2b_clients_portal_token" not in index_names:
        # Unique: the token is the only credential, so two clients must never
        # be able to share one.
        op.create_index(
            "ix_b2b_clients_portal_token", "b2b_clients", ["portal_token"], unique=True
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "b2b_clients" not in set(inspector.get_table_names()):
        return

    index_names = {i["name"] for i in inspector.get_indexes("b2b_clients")}
    if "ix_b2b_clients_portal_token" in index_names:
        op.drop_index("ix_b2b_clients_portal_token", table_name="b2b_clients")

    existing = {c["name"] for c in inspector.get_columns("b2b_clients")}
    for name, _column in reversed(COLUMNS):
        if name in existing:
            op.drop_column("b2b_clients", name)
