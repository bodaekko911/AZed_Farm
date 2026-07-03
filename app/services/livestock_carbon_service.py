"""
Livestock Carbon Service
========================
Auto-generates monthly enteric-methane CarbonLogs for animal groups, using
IPCC 2006 Tier 1 per-head factors (seeded by ensure_carbon_methodology,
source_type="livestock", unit "head-day").

Method
------
For every animal group and every COMPLETE calendar month in a bounded recent
window, one CarbonLog is created:

    quantity  = average headcount over the month × days in the month  (head-days)
    kg_co2e   = quantity × factor_kg_co2e_per_unit

The historical headcount is reconstructed from the current headcount by
walking the intake logs (+count at intake_date) and mortality logs
(−count at death_date) backwards:

    head_at(d) = current_headcount − intakes_after(d) + deaths_after(d)

Manual headcount edits are not journaled, so months before the last manual
edit are an approximation — documented and acceptable for Tier 1 reporting.

Idempotent: a month/group pair is identified by
(ref_type="livestock", ref_id=group.id, log_date=<last day of month>) and is
never created twice. Existing logs are never modified, so a factor edit's
recompute (which rewrites kg_co2e from quantity) remains the single source of
truth for value changes.

Only complete months are logged — the current month appears after it ends —
so figures never change retroactively as the month progresses.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.animal import AnimalGroup, AnimalIntakeLog, MortalityLog
from app.models.carbon import CarbonEmissionFactor, CarbonLog

# animal_type → factor source_key. "other" is deliberately unmapped: emitting
# a guessed factor for an unknown species would be worse than skipping it.
ANIMAL_TYPE_FACTOR_KEYS = {
    "cattle":  "enteric_cattle_head_day",
    "sheep":   "enteric_sheep_head_day",
    "goats":   "enteric_goat_head_day",
    "poultry": "poultry_manure_head_day",
}

REF_TYPE = "livestock"


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _iter_complete_months(start: date, today: date, months_back: int):
    """Yield (month_start, month_end) for every complete month between
    `start` and today, at most `months_back` months into the past."""
    earliest = (today.replace(day=1) - timedelta(days=months_back * 31)).replace(day=1)
    cursor = max(start.replace(day=1), earliest)
    current_month_start = today.replace(day=1)
    while cursor < current_month_start:
        m_start, m_end = _month_bounds(cursor.year, cursor.month)
        yield m_start, m_end
        cursor = (m_end + timedelta(days=1))


async def sync_livestock_carbon_logs(
    db: AsyncSession,
    *,
    months_back: int = 12,
    user_id: int | None = None,
) -> int:
    """Create any missing monthly livestock CarbonLogs. Returns how many were
    created. Safe to call on every startup and from the backfill endpoint."""
    today = date.today()

    factors_q = await db.execute(
        select(CarbonEmissionFactor).where(
            CarbonEmissionFactor.source_key.in_(list(ANIMAL_TYPE_FACTOR_KEYS.values())),
            CarbonEmissionFactor.is_active == True,  # noqa: E712
        )
    )
    factors_by_key = {f.source_key: f for f in factors_q.scalars().all()}
    if not factors_by_key:
        return 0  # factors not seeded yet (first boot ordering) — next run catches up

    groups = (await db.execute(select(AnimalGroup))).scalars().all()
    if not groups:
        return 0
    group_ids = [g.id for g in groups]

    intakes_q = await db.execute(
        select(AnimalIntakeLog.animal_group_id, AnimalIntakeLog.intake_date, AnimalIntakeLog.count)
        .where(AnimalIntakeLog.animal_group_id.in_(group_ids))
    )
    intakes_by_group: dict[int, list[tuple[date, int]]] = {}
    for gid, d, c in intakes_q.all():
        intakes_by_group.setdefault(gid, []).append((d, int(c or 0)))

    deaths_q = await db.execute(
        select(MortalityLog.animal_group_id, MortalityLog.death_date, MortalityLog.count)
        .where(MortalityLog.animal_group_id.in_(group_ids))
    )
    deaths_by_group: dict[int, list[tuple[date, int]]] = {}
    for gid, d, c in deaths_q.all():
        deaths_by_group.setdefault(gid, []).append((d, int(c or 0)))

    existing_q = await db.execute(
        select(CarbonLog.ref_id, CarbonLog.log_date).where(CarbonLog.ref_type == REF_TYPE)
    )
    existing = {(rid, ld) for rid, ld in existing_q.all()}

    created = 0
    for group in groups:
        factor_key = ANIMAL_TYPE_FACTOR_KEYS.get((group.animal_type or "").strip().lower())
        factor = factors_by_key.get(factor_key) if factor_key else None
        if factor is None:
            continue

        current_head = int(group.headcount or 0)
        g_intakes = intakes_by_group.get(group.id, [])
        g_deaths = deaths_by_group.get(group.id, [])

        def head_at(d: date) -> int:
            """Headcount at end of day `d`, reconstructed backwards from now."""
            intakes_after = sum(c for (dt, c) in g_intakes if dt > d)
            deaths_after = sum(c for (dt, c) in g_deaths if dt > d)
            return max(0, current_head - intakes_after + deaths_after)

        start = group.created_at.date() if group.created_at else today
        for m_start, m_end in _iter_complete_months(start, today, months_back):
            if (group.id, m_end) in existing:
                continue
            # Group retired before this month began → nothing to log.
            if group.archived_at and group.archived_at.date() < m_start:
                continue
            head_start = head_at(m_start - timedelta(days=1))
            head_end = head_at(m_end)
            avg_head = (head_start + head_end) / 2.0
            days = (m_end - m_start).days + 1
            qty = Decimal(str(round(avg_head * days, 3)))
            if qty <= 0:
                continue
            kg = (qty * factor.factor_kg_co2e_per_unit).quantize(Decimal("0.0001"))
            db.add(CarbonLog(
                factor_id=factor.id,
                farm_id=group.farm_id,
                user_id=user_id,
                log_date=m_end,
                quantity=qty,
                kg_co2e=kg,
                ref_type=REF_TYPE,
                ref_id=group.id,
                notes=(
                    f"Auto-logged livestock emissions — {group.name} "
                    f"({group.animal_type}), {m_start.strftime('%b %Y')}: "
                    f"avg {avg_head:g} head × {days} days"
                ),
            ))
            existing.add((group.id, m_end))
            created += 1

    if created:
        await db.commit()
    return created