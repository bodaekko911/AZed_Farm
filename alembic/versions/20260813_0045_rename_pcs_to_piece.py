"""Rename the unit "pcs" to "piece".

Revision ID: 20260813_0045_rename_pcs_to_piece
Revises: 20260812_0044_b2b_client_portal
Create Date: 2026-08-13

Two columns carry the unit as free text and both are rewritten together:

  • products.unit             — the product's own unit
  • farm_delivery_items.unit  — the unit a delivery line was recorded in

They must move as a pair. The season cost-apply check refuses to write a
per-piece cost onto a per-kilogram product, so renaming only the product would
make it stop matching its own historical delivery lines. (``app.core.units``
also treats the two spellings as equivalent, so nothing breaks if this
migration has not run yet — this simply makes the stored data consistent.)

Case-insensitive, so "PCS" and "Pcs" are caught too. Only exact matches for the
piece spellings are touched; every other unit is left alone.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260813_0045_rename_pcs_to_piece"
down_revision = "20260812_0044_b2b_client_portal"
branch_labels = None
depends_on = None

# Spellings rewritten to "piece". Deliberately narrow — "each" and "ea" are
# treated as synonyms for comparison but are NOT rewritten, since they may be
# meaningful wording to whoever entered them.
_FROM = ("pcs", "pc", "pce", "pieces")
_TO = "piece"

_TARGETS = (("products", "unit"), ("farm_delivery_items", "unit"))


def _rewrite(from_values, to_value):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, column in _TARGETS:
        if table not in tables:
            continue
        if column not in {c["name"] for c in inspector.get_columns(table)}:
            continue
        bind.execute(
            sa.text(
                f"UPDATE {table} SET {column} = :to_value "
                f"WHERE lower(trim({column})) IN :from_values"
            ).bindparams(sa.bindparam("from_values", expanding=True)),
            {"to_value": to_value, "from_values": list(from_values)},
        )


def upgrade() -> None:
    _rewrite(_FROM, _TO)


def downgrade() -> None:
    # Restores the previous default spelling only; the other variants that were
    # folded in ("pc", "pce", "pieces") are not recoverable and stay as "pcs".
    _rewrite((_TO,), "pcs")
