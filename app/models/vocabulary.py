from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, event
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
