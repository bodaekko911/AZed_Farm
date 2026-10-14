"""The business vocabulary: local names, nicknames and abbreviations for records.

Every place that resolves a name asks this first. ``await ready(db)`` loads the
whole vocabulary with ONE query per request (per database session) into a
context variable; the matcher then reads it synchronously, so ``best_matches``
stays a plain function. Loading per request, not per process, means an edit is
seen by the very next request on every server worker, with nothing to
invalidate across processes.

Aliases are data. They are never put into a prompt as instructions.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select

from app.core.log import record
from app.core.text_norm import normalize
from app.models.vocabulary import VocabularyEntry

# type -> (label, module, model, name column)
ENTITY_TYPES: dict[str, tuple[str, str, str]] = {
    "product": ("Product", "app.models.product", "Product"),
    "b2b_client": ("B2B client", "app.models.b2b", "B2BClient"),
    "pos_customer": ("Customer", "app.models.customer", "Customer"),
    "supplier": ("Supplier", "app.models.supplier", "Supplier"),
    "farm": ("Farm", "app.models.farm", "Farm"),
    "employee": ("Employee", "app.models.hr", "Employee"),
    "expense_category": ("Expense category", "app.models.expense", "ExpenseCategory"),
}
AREA = "area"
ALL_TYPES = (*ENTITY_TYPES, AREA)
SOURCES = ("manual", "learned", "import", "seed")
MAX_ALIAS = 150


class VocabularyError(Exception):
    """A vocabulary change that cannot be saved; the message says why."""


def model_for(kind: str):
    import importlib
    _label, module, name = ENTITY_TYPES[kind]
    return getattr(importlib.import_module(module), name)


def is_live(row: Any) -> bool:
    """Whether a record may be matched: archived or inactive ones keep their
    aliases but are skipped. Records with no active flag (customers, suppliers)
    are always live."""
    value = row.get("is_active") if isinstance(row, dict) else getattr(row, "is_active", None)
    return value not in (False, 0, "0")


# ── Loading: one query per request ───────────────────────────────────────────

@dataclass
class _Snapshot:
    session_id: int
    records: dict[tuple[str, str], int] = field(default_factory=dict)   # (type, alias_key) -> record id
    areas: dict[str, set[str]] = field(default_factory=dict)            # group key -> its spellings


_CURRENT: contextvars.ContextVar[Optional[_Snapshot]] = contextvars.ContextVar("vocabulary", default=None)


async def ready(db) -> None:
    """Load the vocabulary for this request, once. Safe to call from every
    matcher: a second call on the same session costs nothing."""
    current = _CURRENT.get()
    if current is not None and current.session_id == id(db):
        return
    snap = _Snapshot(session_id=id(db))
    stmt = select(VocabularyEntry.entity_type, VocabularyEntry.alias_key,
                  VocabularyEntry.record_id, VocabularyEntry.target_key)
    try:
        nested = getattr(db, "begin_nested", None)
        if nested is None:
            rows = (await db.execute(stmt)).all()
        else:
            # A savepoint: should the query fail (a database not migrated yet), PostgreSQL would otherwise abort the
            # whole transaction and take the request's real work down with it.
            async with nested():
                rows = (await db.execute(stmt)).all()
    except Exception:
        # A session that cannot run the query (a test double, a database not yet
        # migrated) matches exactly as it did before the vocabulary existed.
        rows = []
    for kind, key, record_id, target in rows:
        if kind == AREA:
            group = target or key
            snap.areas.setdefault(group, {group}).add(key)
        elif record_id is not None:
            snap.records[(kind, key)] = record_id
    _CURRENT.set(snap)


def invalidate() -> None:
    _CURRENT.set(None)


def record_for(kind: Optional[str], text: Any) -> Optional[int]:
    """The record an alias names, or None. ``text`` is normalised here."""
    snap = _CURRENT.get()
    if snap is None or not kind:
        return None
    return snap.records.get((kind, normalize(text)))


def ids_matching(kind: str, text: Any) -> list[int]:
    """Records whose alias contains ``text`` — for the review screens' search boxes."""
    snap = _CURRENT.get()
    q = normalize(text)
    if snap is None or not q:
        return []
    return [rid for (k, key), rid in snap.records.items() if k == kind and q in key]


def area_groups() -> list[set[str]]:
    """Each area's spellings ("sharm", "شرم"), every one already normalised."""
    snap = _CURRENT.get()
    return list(snap.areas.values()) if snap else []


async def real_name(db, kind: str, text: Any) -> Optional[str]:
    """The record name an alias stands for, for the lookups that filter by
    name: "خس بلدي" → "Local Lettuce". None when ``text`` is not an alias."""
    await ready(db)
    record_id = record_for(kind, text)
    if record_id is None:
        return None
    model = model_for(kind)
    row = (await db.execute(select(model).where(model.id == record_id))).scalar_one_or_none()
    return row.name if row is not None and is_live(row) else None


async def expand(db, kind: str, text: Any) -> Any:
    """``text``, or the real name when it is an alias."""
    if not str(text or "").strip():
        return text
    return (await real_name(db, kind, text)) or text


# ── Managing it ──────────────────────────────────────────────────────────────

async def _record(db, kind: str, record_id: int):
    model = model_for(kind)
    return (await db.execute(select(model).where(model.id == record_id))).scalar_one_or_none()


async def record_names(db, entries: list[VocabularyEntry]) -> dict[tuple[str, int], str]:
    """(type, id) -> name for the records a list of entries points at, one query per type."""
    out: dict[tuple[str, int], str] = {}
    by_kind: dict[str, set[int]] = {}
    for e in entries:
        if e.entity_type in ENTITY_TYPES and e.record_id is not None:
            by_kind.setdefault(e.entity_type, set()).add(e.record_id)
    for kind, ids in by_kind.items():
        model = model_for(kind)
        for rid, name in (await db.execute(select(model.id, model.name).where(model.id.in_(ids)))).all():
            out[(kind, rid)] = name
    return out


async def list_entries(db, kind: Optional[str] = None, q: Optional[str] = None) -> list[dict]:
    stmt = select(VocabularyEntry).order_by(VocabularyEntry.entity_type, VocabularyEntry.alias_key)
    if kind:
        stmt = stmt.where(VocabularyEntry.entity_type == kind)
    entries = (await db.execute(stmt)).scalars().all()
    names = await record_names(db, entries)
    needle = normalize(q)
    out = []
    for e in entries:
        target = (e.target_key if e.entity_type == AREA
                  else names.get((e.entity_type, e.record_id), f"#{e.record_id} (deleted)"))
        if needle and needle not in e.alias_key and needle not in normalize(target) \
                and needle not in normalize(e.note):
            continue
        out.append({
            "id": e.id, "alias": e.alias, "type": e.entity_type,
            "type_label": ENTITY_TYPES[e.entity_type][0] if e.entity_type in ENTITY_TYPES else "Area",
            "record_id": e.record_id, "target": target, "note": e.note or "",
            "source": e.source, "created_by": e.created_by_name or "",
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })
    return out


async def area_group_names(db) -> list[str]:
    rows = (await db.execute(select(VocabularyEntry.target_key).where(
        VocabularyEntry.entity_type == AREA).distinct())).all()
    return sorted({r[0] for r in rows if r[0]})


async def search_records(db, kind: str, q: str) -> list[dict]:
    """Records of a type for the page's picker."""
    if kind not in ENTITY_TYPES:
        raise VocabularyError("Unknown type.")
    model = model_for(kind)
    stmt = select(model).order_by(model.name).limit(20)
    if (q or "").strip():
        stmt = stmt.where(model.name.ilike(f"%{q.strip()}%"))
    return [{"id": r.id, "name": r.name, "active": is_live(r)} for r in (await db.execute(stmt)).scalars().all()]


async def _validate(db, *, kind: str, alias: str, record_id: Optional[int], group: Optional[str],
                    new_group: Optional[str], exclude_id: Optional[int] = None) -> tuple[str, Optional[int], Optional[str], list[str]]:
    """(alias_key, record_id, target_key, warnings), or VocabularyError."""
    if kind not in ALL_TYPES:
        raise VocabularyError("Pick what the alias is for (product, client, farm…).")
    alias = (alias or "").strip()
    key = normalize(alias)
    if not key:
        raise VocabularyError("Type the alias.")
    if len(alias) > MAX_ALIAS:
        raise VocabularyError(f"An alias can be at most {MAX_ALIAS} characters.")

    taken = (await db.execute(select(VocabularyEntry).where(
        VocabularyEntry.entity_type == kind, VocabularyEntry.alias_key == key))).scalars().first()
    if taken is not None and taken.id != exclude_id:
        if kind == AREA:
            where = f"the area group '{taken.target_key}'"
        else:
            hit = await _record(db, kind, taken.record_id)
            where = f"'{hit.name}'" if hit is not None else f"record #{taken.record_id}"
        raise VocabularyError(f"'{alias}' already means {where}. An alias can mean only one "
                              f"{(ENTITY_TYPES.get(kind) or ('area',))[0].lower()}.")

    warnings: list[str] = []
    if kind == AREA:
        groups = await area_group_names(db)
        if new_group and new_group.strip():
            target = normalize(new_group)
            if not target:
                raise VocabularyError("Name the new area group.")
            # The same area must not split into two groups: a "new" group that
            # normalises to an existing one IS that group.
            if target not in groups:
                owner = (await db.execute(select(VocabularyEntry.target_key).where(
                    VocabularyEntry.entity_type == AREA, VocabularyEntry.alias_key == target))).scalar()
                if owner and owner != target:
                    raise VocabularyError(f"'{new_group.strip()}' is already a spelling in the area group "
                                          f"'{owner}' — add it there instead.")
        else:
            target = normalize(group)
            if target not in groups:
                raise VocabularyError("Pick the area group, or choose New group.")
        return key, None, target, warnings

    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        raise VocabularyError("Pick the record the alias stands for.")
    target_row = await _record(db, kind, record_id)
    if target_row is None:
        raise VocabularyError("That record no longer exists.")
    model = model_for(kind)
    for other_id, other_name in (await db.execute(select(model.id, model.name))).all():
        if other_id != record_id and normalize(other_name) == key:
            warnings.append(f"'{alias}' is also the real name of '{other_name}'. Typing it will now "
                            f"mean '{target_row.name}'.")
            break
    if not is_live(target_row):
        warnings.append(f"'{target_row.name}' is inactive, so this alias is kept but not used for matching.")
    return key, record_id, None, warnings


def _describe(kind: str, alias: str, target: str) -> str:
    label = ENTITY_TYPES[kind][0].lower() if kind in ENTITY_TYPES else "area group"
    return f"'{alias}' = {label} '{target}'"


async def _target_text(db, kind: str, record_id: Optional[int], target_key: Optional[str]) -> str:
    if kind == AREA:
        return target_key or ""
    row = await _record(db, kind, record_id)
    return row.name if row is not None else f"#{record_id}"


async def create_entry(db, user, *, kind: str, alias: str, record_id=None, group=None, new_group=None,
                       note: Optional[str] = None, source: str = "manual", commit: bool = True) -> tuple[VocabularyEntry, list[str]]:
    key, rid, target, warnings = await _validate(db, kind=kind, alias=alias, record_id=record_id,
                                                 group=group, new_group=new_group)
    entry = VocabularyEntry(alias=alias.strip()[:MAX_ALIAS], alias_key=key, entity_type=kind, record_id=rid,
                            target_key=target, note=(note or "").strip()[:500] or None,
                            source=source if source in SOURCES else "manual",
                            created_by_id=getattr(user, "id", None),
                            created_by_name=getattr(user, "name", None))
    db.add(entry)
    await db.flush()
    record(db, "Vocabulary", "add_alias",
           f"Added {_describe(kind, entry.alias, await _target_text(db, kind, rid, target))} ({entry.source})",
           user=user, ref_type="vocabulary", ref_id=entry.id)
    if commit:
        await db.commit()
    invalidate()
    return entry, warnings


async def update_entry(db, user, entry_id: int, *, alias: str, record_id=None, group=None, new_group=None,
                       note: Optional[str] = None) -> tuple[VocabularyEntry, list[str]]:
    entry = await db.get(VocabularyEntry, entry_id)
    if entry is None:
        raise VocabularyError("That alias no longer exists.")
    before = _describe(entry.entity_type, entry.alias,
                       await _target_text(db, entry.entity_type, entry.record_id, entry.target_key))
    key, rid, target, warnings = await _validate(db, kind=entry.entity_type, alias=alias, record_id=record_id,
                                                 group=group, new_group=new_group, exclude_id=entry.id)
    entry.alias, entry.alias_key, entry.record_id, entry.target_key = alias.strip()[:MAX_ALIAS], key, rid, target
    entry.note = (note or "").strip()[:500] or None
    after = _describe(entry.entity_type, entry.alias, await _target_text(db, entry.entity_type, rid, target))
    record(db, "Vocabulary", "edit_alias", f"Changed {before} to {after}", user=user,
           ref_type="vocabulary", ref_id=entry.id)
    await db.commit()
    invalidate()
    return entry, warnings


async def delete_entry(db, user, entry_id: int) -> None:
    entry = await db.get(VocabularyEntry, entry_id)
    if entry is None:
        raise VocabularyError("That alias no longer exists.")
    what = _describe(entry.entity_type, entry.alias,
                     await _target_text(db, entry.entity_type, entry.record_id, entry.target_key))
    await db.delete(entry)
    record(db, "Vocabulary", "delete_alias", f"Deleted {what}", user=user, ref_type="vocabulary", ref_id=entry_id)
    await db.commit()
    invalidate()


def http_error(exc: VocabularyError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))
