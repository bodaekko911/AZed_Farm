"""Vocabulary: unit words per product, and business terms for the assistant.

Revision ID: 20261020_0059_unit_words
Revises: 20261019_0058_vocabulary
Create Date: 2026-10-20

* ``product_unit_words`` — a local unit of one product and what it holds in the product's own unit ("قفص" of
  tomatoes = 20 kg). Used to convert typed and handwritten quantities; never to pick the product.
* ``business_terms`` — short definitions the assistant is given as data ("the box" = the weekly vegetable box),
  capped in total by the app. Seeded with "هالك" (spoilage), which also stays in code where it drives behaviour.
"""
from alembic import context, op
import sqlalchemy as sa

from app.models.vocabulary import term_seed_rows

revision = "20261020_0059_unit_words"
down_revision = "20261019_0058_vocabulary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("product_unit_words"):
        op.create_table(
            "product_unit_words",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
            sa.Column("word", sa.String(60), nullable=False),
            sa.Column("word_key", sa.String(60), nullable=False),
            sa.Column("qty", sa.Numeric(14, 4), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_by_name", sa.String(120), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("product_id", "word_key", name="uq_unit_word_product_word"),
        )
        op.create_index("ix_product_unit_words_id", "product_unit_words", ["id"])
        op.create_index("ix_product_unit_words_product_id", "product_unit_words", ["product_id"])
    if not inspector.has_table("business_terms"):
        op.create_table(
            "business_terms",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("term", sa.String(80), nullable=False),
            sa.Column("term_key", sa.String(80), nullable=False, unique=True),
            sa.Column("definition", sa.String(400), nullable=False),
            sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_by_name", sa.String(120), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_business_terms_id", "business_terms", ["id"])
    if context.is_offline_mode():
        return
    seed(bind)


def seed(bind) -> None:
    """Insert the seed terms that are not there yet."""
    terms = sa.table("business_terms", sa.column("term"), sa.column("term_key"), sa.column("definition"),
                     sa.column("source"))
    have = {k for (k,) in bind.execute(sa.text("SELECT term_key FROM business_terms"))}
    rows = term_seed_rows(have)
    if rows:
        bind.execute(terms.insert(), rows)


def downgrade() -> None:
    op.drop_table("business_terms")
    op.drop_table("product_unit_words")
