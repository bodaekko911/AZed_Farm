from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, event
from sqlalchemy.sql import func

from app.core.text_norm import normalize
from app.database import Base

# The delivery areas the PDF invoice matching always knew (شرم ↔ sharm …): seeded into every new vocabulary — by
# the migration on an existing database, and on table creation for a fresh one — and edited on the Vocabulary page
# from then on. (group, spelling)
AREA_SEEDS = [("sharm", "شرم"), ("dahab", "دهب"), ("nuweiba", "نويبع"), ("taba", "طابا"), ("cairo", "القاهرة"),
              ("hurghada", "الغردقة"), ("el tor", "الطور")]
AREA_SEED_NOTE = "Delivery area — was built into the PDF invoice matching"


def area_seed_rows(have: set | None = None) -> list[dict]:
    have = have or set()
    return [{"alias": spelling, "alias_key": normalize(spelling), "entity_type": "area", "record_id": None,
             "target_key": normalize(group), "source": "seed", "note": AREA_SEED_NOTE}
            for group, spelling in AREA_SEEDS if ("area", normalize(spelling)) not in have]


class VocabularyEntry(Base):
    """A local name, nickname or abbreviation for a record: "خس بلدي" = the
    product "Local Lettuce", "الشمالية" = the farm "North Farm".

    ``alias_key`` is the alias as compared (``app.core.text_norm.normalize``);
    one alias can mean one record per type. Areas are not records — an area
    entry belongs to a spelling group (``target_key``, e.g. "sharm") instead of
    pointing at a ``record_id``.
    """
    __tablename__ = "vocabulary_entries"
    __table_args__ = (UniqueConstraint("entity_type", "alias_key", name="uq_vocabulary_type_alias"),)

    id = Column(Integer, primary_key=True, index=True)
    alias = Column(String(150), nullable=False)
    alias_key = Column(String(150), nullable=False, index=True)
    entity_type = Column(String(30), nullable=False, index=True)
    record_id = Column(Integer, nullable=True)
    target_key = Column(String(150), nullable=True)      # areas: the group's normalised name
    note = Column(Text)
    source = Column(String(20), nullable=False, default="manual")   # manual / learned / import / seed
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by_name = Column(String(120))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


@event.listens_for(VocabularyEntry.__table__, "after_create")
def _seed_areas(table, connection, **_kw):
    """A brand-new vocabulary (create_all on a fresh database) starts with the delivery areas, as the migration
    gives an existing one."""
    connection.execute(table.insert(), area_seed_rows())


class ProductUnitWord(Base):
    """A local unit for one product and what it holds in the product's own unit: a "قفص" (crate) of tomatoes is
    20 kg, a "ربطة" (bunch) of mint 0.25 kg. Used only to convert quantities — never to pick the product."""
    __tablename__ = "product_unit_words"
    __table_args__ = (UniqueConstraint("product_id", "word_key", name="uq_unit_word_product_word"),)

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    word = Column(String(60), nullable=False)
    word_key = Column(String(60), nullable=False)
    qty = Column(Numeric(14, 4), nullable=False)            # in the product's own unit
    note = Column(Text)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by_name = Column(String(120))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BusinessTerm(Base):
    """A short definition the assistant should know — "the box" = the weekly vegetable box product. Given to the
    model as data inside a fenced block, never as an instruction."""
    __tablename__ = "business_terms"

    id = Column(Integer, primary_key=True, index=True)
    term = Column(String(80), nullable=False)
    term_key = Column(String(80), nullable=False, unique=True)
    definition = Column(String(400), nullable=False)
    source = Column(String(20), nullable=False, default="manual")
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by_name = Column(String(120))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# "هالك" drives behaviour in code (the field bot routes it to spoilage) and stays there; as a term the assistant
# also has it spelled out.
TERM_SEEDS = [("هالك", "spoilage — produce thrown away as damaged or rotten (also تالف، بايظ، فاسد)")]


def term_seed_rows(have: set | None = None) -> list[dict]:
    have = have or set()
    return [{"term": t, "term_key": normalize(t), "definition": d, "source": "seed"}
            for t, d in TERM_SEEDS if normalize(t) not in have]


@event.listens_for(BusinessTerm.__table__, "after_create")
def _seed_terms(table, connection, **_kw):
    connection.execute(table.insert(), term_seed_rows())
