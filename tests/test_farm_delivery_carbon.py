"""Farm delivery transport carbon logging — regression tests.

The carbon module promises auto-logged transport emissions from farm intake.
These tests pin the contract of the service helpers:

- create: distance + vehicle → CarbonLog (quantity=km, kg = km × factor),
  silently skipped when distance is missing or the factor is inactive
- resync (edit): old logs removed, new log reflects current state
- delete: logs removed
"""

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.services import farm_intake_service as svc
from app.models.carbon import CarbonLog


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else [self._value]

    def first(self):
        items = self.all()
        return items[0] if items else None


class FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.deleted = []

    async def execute(self, statement):
        return FakeScalarResult(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)


def _van_factor():
    return SimpleNamespace(id=5, source_key="van_km",
                           factor_kg_co2e_per_unit=Decimal("0.23"))


def _delivery(**overrides):
    base = dict(
        id=77,
        delivery_number="FD-0077",
        farm_id=2,
        user_id=9,
        delivery_date=date(2026, 6, 10),
        distance_km=Decimal("35.0"),
        vehicle_type="van",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── create ───────────────────────────────────────────────────────────────────

def test_delivery_with_distance_creates_transport_log() -> None:
    db = FakeSession([_van_factor()])
    created = asyncio.run(svc.create_carbon_log_for_delivery(db, _delivery(), user_id=9))

    assert created is True
    assert len(db.added) == 1
    log = db.added[0]
    assert isinstance(log, CarbonLog)
    assert log.ref_type == "farm_delivery"
    assert log.ref_id == 77
    assert float(log.quantity) == 35.0
    assert float(log.kg_co2e) == 8.05          # 35 km × 0.23
    assert log.farm_id == 2
    assert log.log_date == date(2026, 6, 10)


def test_delivery_without_distance_is_skipped() -> None:
    db = FakeSession([])  # must not even query the factor
    created = asyncio.run(
        svc.create_carbon_log_for_delivery(db, _delivery(distance_km=None))
    )
    assert created is False
    assert db.added == []


def test_unknown_vehicle_falls_back_to_van() -> None:
    db = FakeSession([_van_factor()])
    created = asyncio.run(
        svc.create_carbon_log_for_delivery(db, _delivery(vehicle_type="donkey"))
    )
    assert created is True  # van_km fallback, never a crash


def test_missing_factor_is_skipped_silently() -> None:
    db = FakeSession([None])  # factor lookup returns nothing
    created = asyncio.run(svc.create_carbon_log_for_delivery(db, _delivery()))
    assert created is False
    assert db.added == []


# ── resync (edit) ────────────────────────────────────────────────────────────

def test_resync_replaces_stale_log() -> None:
    stale = SimpleNamespace(id=900, ref_type="farm_delivery", ref_id=77)
    db = FakeSession([
        [stale],         # existing logs query
        _van_factor(),   # factor lookup for the rebuild
    ])
    delivery = _delivery(distance_km=Decimal("50.0"))
    asyncio.run(svc.resync_carbon_log_for_delivery(db, delivery, user_id=9))

    assert stale in db.deleted
    assert len(db.added) == 1
    assert float(db.added[0].kg_co2e) == 11.5  # 50 × 0.23


def test_resync_after_clearing_distance_removes_log() -> None:
    stale = SimpleNamespace(id=901, ref_type="farm_delivery", ref_id=77)
    db = FakeSession([[stale]])  # only the delete query — create skips early
    delivery = _delivery(distance_km=None)
    asyncio.run(svc.resync_carbon_log_for_delivery(db, delivery))

    assert stale in db.deleted
    assert db.added == []


# ── delete ───────────────────────────────────────────────────────────────────

def test_delete_removes_transport_logs() -> None:
    l1 = SimpleNamespace(id=902, ref_type="farm_delivery", ref_id=77)
    l2 = SimpleNamespace(id=903, ref_type="farm_delivery", ref_id=77)
    db = FakeSession([[l1, l2]])
    asyncio.run(svc.delete_carbon_log_for_delivery(db, 77))
    assert l1 in db.deleted and l2 in db.deleted


# ── wiring guards ────────────────────────────────────────────────────────────

def test_farm_router_wires_carbon_helpers() -> None:
    """The service helpers exist but are worthless unless the mounted router
    actually calls them — pin the wiring so it can't silently regress."""
    import inspect
    from app.routers import farm

    source = inspect.getsource(farm)
    assert "resync_carbon_log_for_delivery" in source, "edit_delivery must resync"
    assert "delete_carbon_log_for_delivery" in source, "delete_delivery must clean up"
    assert "distance_km" in source and "vehicle_type" in source

    create_src = inspect.getsource(farm.create_delivery)
    assert "distance_km" in create_src, "create_delivery must pass distance to the service"


def test_backfill_covers_deliveries() -> None:
    import inspect
    from app.routers import carbon

    source = inspect.getsource(carbon.backfill_auto_logs)
    assert "farm_delivery" in source and "create_carbon_log_for_delivery" in source, (
        "backfill must include deliveries with distance but no transport log"
    )