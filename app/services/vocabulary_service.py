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


# ── CSV: alias, type, record (name or id; the group for an area), note ───────

CSV_COLUMNS = ("alias", "type", "record", "note")
MAX_IMPORT_ROWS = 2000


def _type_of(text: Any) -> Optional[str]:
    """A type as written in a file: its key ("b2b_client") or its label ("B2B client")."""
    key = normalize(text).replace(" ", "_")
    for kind in ALL_TYPES:
        label = ENTITY_TYPES[kind][0] if kind in ENTITY_TYPES else "Area"
        if key in (kind, normalize(label).replace(" ", "_")):
            return kind
    return None


async def export_csv(db) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for e in await list_entries(db):
        w.writerow([e["alias"], e["type"], e["target"], e["note"]])
    return buf.getvalue()


def _read_csv(text: str) -> list[dict]:
    import csv
    import io
    text = (text or "").lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any((c or "").strip() for c in r)]
    if not rows:
        raise VocabularyError("The file is empty.")
    head = [normalize(c).replace(" ", "_") for c in rows[0]]
    if "alias" not in head or "type" not in head:
        raise VocabularyError("The first row must be the header: alias, type, record, note.")
    if len(rows) - 1 > MAX_IMPORT_ROWS:
        raise VocabularyError(f"At most {MAX_IMPORT_ROWS} rows per file.")
    idx = {name: head.index(name) for name in CSV_COLUMNS if name in head}
    cell = lambda r, name: (r[idx[name]].strip() if name in idx and idx[name] < len(r) else "")
    return [{"line": n, **{name: cell(r, name) for name in CSV_COLUMNS}} for n, r in enumerate(rows[1:], start=2)]


async def preview_import(db, text: str) -> list[dict]:
    """Every row of the file with what importing it would do — nothing is saved. Status is one of:
    add · duplicate (already there, or twice in the file) · conflict (the alias already means another record) ·
    unmatched (unknown type, no such record, several records of that name, or no alias)."""
    rows = _read_csv(text)
    existing = {(e.entity_type, e.alias_key): e for e in (await db.execute(select(VocabularyEntry))).scalars()}
    groups = set(await area_group_names(db))
    names: dict[str, dict[str, list[tuple[int, str]]]] = {}
    ids: dict[str, dict[int, str]] = {}

    async def records_of(kind: str):
        if kind not in names:
            model = model_for(kind)
            names[kind], ids[kind] = {}, {}
            for rid, name in (await db.execute(select(model.id, model.name))).all():
                names[kind].setdefault(normalize(name), []).append((rid, name))
                ids[kind][rid] = name
        return names[kind], ids[kind]

    seen: set = set()
    out = []
    for r in rows:
        row = {**r, "status": "add", "message": "", "kind": None, "record_id": None, "group": None, "target": ""}
        out.append(row)
        kind = _type_of(r["type"])
        key = normalize(r["alias"])
        if not key:
            row.update(status="unmatched", message="No alias")
            continue
        if kind is None:
            row.update(status="unmatched", message=f"Unknown type '{r['type']}'")
            continue
        row["kind"] = kind
        if kind == AREA:
            group = normalize(r["record"])
            if not group:
                row.update(status="unmatched", message="No area group")
                continue
            owner = existing.get((AREA, group))
            if group not in groups and owner is not None and owner.target_key != group:
                row.update(status="unmatched",
                           message=f"'{r['record']}' is a spelling in the area group '{owner.target_key}'")
                continue
            row.update(group=group, target=group)
            if group not in groups:
                row["message"] = f"New area group '{group}'"
            target_match = lambda e: e.target_key == group
        else:
            by_name, by_id = await records_of(kind)
            text_ref = r["record"]
            if text_ref.isdigit() and int(text_ref) in by_id:
                rid, rname = int(text_ref), by_id[int(text_ref)]
            else:
                hits = by_name.get(normalize(text_ref), [])
                if not hits:
                    row.update(status="unmatched", message=f"No {ENTITY_TYPES[kind][0].lower()} called '{text_ref}'")
                    continue
                if len(hits) > 1:
                    row.update(status="unmatched",
                               message=f"{len(hits)} records are called '{text_ref}' — use the id instead")
                    continue
                rid, rname = hits[0]
            row.update(record_id=rid, target=rname)
            target_match = lambda e, rid=rid: e.record_id == rid
        if (kind, key) in seen:
            row.update(status="duplicate", message="Already earlier in this file")
            continue
        taken = existing.get((kind, key))
        if taken is not None:
            if target_match(taken):
                row.update(status="duplicate", message="Already in the vocabulary")
            else:
                where = taken.target_key if kind == AREA else (ids.get(kind, {}).get(taken.record_id)
                                                               or f"#{taken.record_id}")
                row.update(status="conflict", message=f"'{r['alias']}' already means '{where}'")
            continue
        seen.add((kind, key))
        if kind != AREA:
            other = [n for rid2, n in (await records_of(kind))[0].get(key, []) if rid2 != row["record_id"]]
            if other:
                row["message"] = f"Also the real name of '{other[0]}'"
    return out


async def import_csv(db, user, text: str) -> dict:
    """Add the file's "add" rows — re-checked here, never trusted from the preview — in one transaction."""
    rows = await preview_import(db, text)
    added = 0
    for r in rows:
        if r["status"] != "add":
            continue
        if r["kind"] == AREA:
            groups = await area_group_names(db)
            await create_entry(db, user, kind=AREA, alias=r["alias"], note=r["note"] or None, source="import",
                               commit=False, **({"group": r["group"]} if r["group"] in groups
                                                else {"new_group": r["group"]}))
        else:
            await create_entry(db, user, kind=r["kind"], alias=r["alias"], record_id=r["record_id"],
                               note=r["note"] or None, source="import", commit=False)
        added += 1
    if added:
        record(db, "Vocabulary", "import", f"Imported {added} alias(es) from a CSV file", user=user,
               ref_type="vocabulary")
    await db.commit()
    invalidate()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"added": added, "counts": counts}


MANAGE = "action_vocabulary_manage"

# What each review screen may teach: the types it lets the user correct.
LEARNABLE = {
    "pdf_invoice": ("pos_customer", "product"),
    "expense_receipt": ("expense_category", "farm"),
    "delivery_note": ("supplier", "product"),
    "harvest_sheet": ("product", "farm"),
    "spoilage": ("product",),
    "b2b_sale": ("b2b_client", "product"),
}
MAX_LEARN = 60


def may_learn(user) -> bool:
    from app.core.permissions import has_permission
    return bool(user) and has_permission(user, MANAGE)


async def learn(db, user, remember: Any, screen: str) -> dict:
    """Save the "Remember 'X' as Y" choices ticked on a review screen — called only AFTER the invoice or document was
    recorded, so nothing is learned from a review that was abandoned or refused.

    Quietly does nothing for someone without the vocabulary permission (the option is not shown to them). Each
    choice is checked like one typed on the Vocabulary page; one that would clash with an existing alias is not
    saved and comes back as a note, never as an error — the record itself is already saved."""
    out = {"learned": 0, "notes": []}
    if not isinstance(remember, list) or not remember or not may_learn(user):
        return out
    allowed = LEARNABLE.get(screen, ())
    from app.services.assistant_actions import without_size
    for item in remember[:MAX_LEARN]:
        if not isinstance(item, dict) or item.get("type") not in allowed:
            continue
        kind, alias = item["type"], str(item.get("alias") or "").strip()
        if kind == "product":
            alias = " ".join(without_size(alias).split())      # matched without its pack size, so stored without it
        try:
            record_id = int(item.get("record_id"))
        except (TypeError, ValueError):
            continue
        key = normalize(alias)
        if not key:
            continue
        target = await _record(db, kind, record_id)
        if target is None or normalize(target.name) == key:
            continue                                            # gone, or the alias IS its name: nothing to learn
        existing = (await db.execute(select(VocabularyEntry).where(
            VocabularyEntry.entity_type == kind, VocabularyEntry.alias_key == key))).scalars().first()
        if existing is not None and existing.record_id == record_id:
            continue                                            # already known
        try:
            await create_entry(db, user, kind=kind, alias=alias[:MAX_ALIAS], record_id=record_id,
                               note="Learned from a correction", source="learned", commit=False)
            out["learned"] += 1
        except VocabularyError as exc:
            out["notes"].append(f"Not remembered: {exc}")
    if out["learned"]:
        await db.commit()
    invalidate()
    return out


def http_error(exc: VocabularyError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))
