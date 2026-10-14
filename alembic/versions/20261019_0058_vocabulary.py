"""Business vocabulary: local names, nicknames and abbreviations for records.

Revision ID: 20261019_0058_vocabulary
Revises: 20261018_0057_animal_tabs
Create Date: 2026-10-19

Creates ``vocabulary_entries`` and seeds what used to be hard-coded:

* the delivery areas (``AREA_ALIASES`` in assistant_actions) as area groups —
  شرم ↔ sharm, دهب ↔ dahab … — so they are edited like everything else;
* the two record aliases from the prompt's ``LOCAL_WORDS`` — "المطبخ" /
  "للمطبخ" = the client Habiba Kitchen, "الساند بونك" / "من الساند بونك" = the
  farm Habiba/SPC — when those records exist.

Keys are computed with the app's own ``normalize`` so a seeded alias compares
exactly as one typed on the Vocabulary page.
"""
from alembic import context, op
import sqlalchemy as sa

from app.core.text_norm import normalize
from app.models.vocabulary import area_seed_rows

revision = "20261019_0058_vocabulary"
down_revision = "20261018_0057_animal_tabs"
branch_labels = None
depends_on = None

# (type, record name, alias) — the aliases that were in LOCAL_WORDS.
RECORD_SEEDS = [("b2b_client", "Habiba Kitchen", "المطبخ"), ("b2b_client", "Habiba Kitchen", "للمطبخ"),
                ("farm", "Habiba/SPC", "الساند بونك"), ("farm", "Habiba/SPC", "من الساند بونك")]
TABLES = {"b2b_client": "b2b_clients", "farm": "farms"}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("vocabulary_entries"):
        op.create_table(
            "vocabulary_entries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("alias", sa.String(150), nullable=False),
            sa.Column("alias_key", sa.String(150), nullable=False),
            sa.Column("entity_type", sa.String(30), nullable=False),
            sa.Column("record_id", sa.Integer(), nullable=True),
            sa.Column("target_key", sa.String(150), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_by_name", sa.String(120), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("entity_type", "alias_key", name="uq_vocabulary_type_alias"),
        )
        op.create_index("ix_vocabulary_entries_id", "vocabulary_entries", ["id"])
        op.create_index("ix_vocabulary_entries_alias_key", "vocabulary_entries", ["alias_key"])
        op.create_index("ix_vocabulary_entries_entity_type", "vocabulary_entries", ["entity_type"])
    if context.is_offline_mode():
        return
    seed(bind)


def seed(bind) -> None:
    """Insert the seeds that are not there yet. Run by the migration; tests call it on their own database."""
    entries = sa.table("vocabulary_entries", sa.column("alias"), sa.column("alias_key"), sa.column("entity_type"),
                       sa.column("record_id"), sa.column("target_key"), sa.column("source"), sa.column("note"))
    have = {(t, k) for t, k in bind.execute(sa.text("SELECT entity_type, alias_key FROM vocabulary_entries"))}
    rows = area_seed_rows(have)          # the old hard-coded AREA_ALIASES, exactly
    have |= {("area", r["alias_key"]) for r in rows}
    for kind, name, alias in RECORD_SEEDS:
        table = TABLES[kind]
        if not sa.inspect(bind).has_table(table):
            continue
        record_id = next((rid for rid, n in bind.execute(sa.text(f"SELECT id, name FROM {table}"))
                          if normalize(n) == normalize(name)), None)
        key = normalize(alias)
        if record_id is not None and (kind, key) not in have:
            rows.append({"alias": alias, "alias_key": key, "entity_type": kind, "record_id": record_id,
                         "target_key": None, "source": "seed", "note": "Was in the assistant's local words"})
            have.add((kind, key))
    if rows:
        bind.execute(entries.insert(), rows)


def downgrade() -> None:
    op.drop_table("vocabulary_entries")
