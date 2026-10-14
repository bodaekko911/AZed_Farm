"""Vocabulary — the farm's local names, nicknames and abbreviations for products, clients, farms and so on.

Seeing it needs page_vocabulary (admins and managers): aliases include client names and employee nicknames.
Changing it needs action_vocabulary_manage (admins). Every change is written to the activity log.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.navigation import render_app_header
from app.core.permissions import has_permission, require_permission
from app.database import get_async_session
from app.models.user import User
from app.services import vocabulary_service as vocab

router = APIRouter(prefix="/vocabulary", tags=["Vocabulary"],
                   dependencies=[Depends(require_permission("page_vocabulary"))])

MANAGE = "action_vocabulary_manage"


class EntryIn(BaseModel):
    type: str = Field(..., max_length=30)
    alias: str = Field(..., max_length=vocab.MAX_ALIAS)
    record_id: Optional[int] = None
    group: Optional[str] = Field(None, max_length=150)
    new_group: Optional[str] = Field(None, max_length=150)
    note: Optional[str] = Field(None, max_length=500)


class EntryEdit(BaseModel):
    alias: str = Field(..., max_length=vocab.MAX_ALIAS)
    record_id: Optional[int] = None
    group: Optional[str] = Field(None, max_length=150)
    new_group: Optional[str] = Field(None, max_length=150)
    note: Optional[str] = Field(None, max_length=500)


def _types() -> list[dict]:
    return ([{"key": k, "label": v[0]} for k, v in vocab.ENTITY_TYPES.items()]
            + [{"key": vocab.AREA, "label": "Area"}])


@router.get("/api/entries")
async def entries(type: Optional[str] = None, q: Optional[str] = None, db: AsyncSession = Depends(get_async_session)):
    if type and type not in vocab.ALL_TYPES:
        raise HTTPException(status_code=400, detail="Unknown type")
    return {"entries": await vocab.list_entries(db, type or None, q)}


@router.get("/api/area-groups")
async def area_groups(db: AsyncSession = Depends(get_async_session)):
    return {"groups": await vocab.area_group_names(db)}


@router.get("/api/records")
async def records(type: str, q: str = "", db: AsyncSession = Depends(get_async_session),
                  user: User = Depends(require_permission(MANAGE))):
    try:
        return {"records": await vocab.search_records(db, type, q[:80])}
    except vocab.VocabularyError as exc:
        raise vocab.http_error(exc)


@router.post("/api/entries")
async def add(data: EntryIn, db: AsyncSession = Depends(get_async_session),
              user: User = Depends(require_permission(MANAGE))):
    try:
        entry, warnings = await vocab.create_entry(db, user, kind=data.type, alias=data.alias, record_id=data.record_id,
                                                   group=data.group, new_group=data.new_group, note=data.note)
    except vocab.VocabularyError as exc:
        raise vocab.http_error(exc)
    return {"ok": True, "id": entry.id, "warnings": warnings}


@router.put("/api/entries/{entry_id}")
async def edit(entry_id: int, data: EntryEdit, db: AsyncSession = Depends(get_async_session),
               user: User = Depends(require_permission(MANAGE))):
    try:
        _entry, warnings = await vocab.update_entry(db, user, entry_id, alias=data.alias, record_id=data.record_id,
                                                    group=data.group, new_group=data.new_group, note=data.note)
    except vocab.VocabularyError as exc:
        raise vocab.http_error(exc)
    return {"ok": True, "warnings": warnings}


@router.delete("/api/entries/{entry_id}")
async def remove(entry_id: int, db: AsyncSession = Depends(get_async_session),
                 user: User = Depends(require_permission(MANAGE))):
    try:
        await vocab.delete_entry(db, user, entry_id)
    except vocab.VocabularyError as exc:
        raise vocab.http_error(exc)
    return {"ok": True}


class CsvIn(BaseModel):
    csv: str = Field(..., max_length=2_000_000)


@router.get("/api/export")
async def export(db: AsyncSession = Depends(get_async_session)):
    """Every alias as CSV: alias, type, record (its name; the group for an area), note."""
    from fastapi.responses import Response
    return Response(content="﻿" + await vocab.export_csv(db), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="vocabulary.csv"'})


@router.post("/api/import/preview")
async def import_preview(data: CsvIn, db: AsyncSession = Depends(get_async_session),
                         user: User = Depends(require_permission(MANAGE))):
    """What importing the file would do, row by row. Nothing is saved."""
    try:
        rows = await vocab.preview_import(db, data.csv)
    except vocab.VocabularyError as exc:
        raise vocab.http_error(exc)
    keep = ("line", "alias", "type", "record", "note", "status", "message", "target")
    return {"rows": [{k: r[k] for k in keep} for r in rows]}


@router.post("/api/import")
async def import_commit(data: CsvIn, db: AsyncSession = Depends(get_async_session),
                        user: User = Depends(require_permission(MANAGE))):
    try:
        return await vocab.import_csv(db, user, data.csv)
    except vocab.VocabularyError as exc:
        raise vocab.http_error(exc)


@router.get("/", response_class=HTMLResponse)
def page(current_user: User = Depends(require_permission("page_vocabulary"))):
    cfg = json.dumps({"canManage": has_permission(current_user, MANAGE), "types": _types()}).replace("</", "<\\/")
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vocabulary — AZed Farm</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&family=Outfit:wght@400;600;800&family=Cairo:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#08090c;--card:#0f1311;--card2:#151a17;--border:rgba(255,255,255,.07);--border2:rgba(255,255,255,.13);
      --green:#7ecb6f;--lime:#84cc16;--blue:#6a9fd4;--warn:#e0a84a;--danger:#e06c75;--text:#e8eae0;--sub:#a3aa98;--muted:#6b7363;
      --sans:'DM Sans','Cairo',sans-serif;--mono:'DM Mono',monospace}
body.light{--bg:#f4f5ef;--card:#ffffff;--card2:#eef0e8;--border:rgba(0,0,0,.08);--border2:rgba(0,0,0,.14);
      --green:#0f8a43;--text:#1a1e14;--sub:#4a5040;--muted:#8a9080}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh}
.wrap{max-width:1100px;margin:0 auto;padding:28px 16px 80px}
.title{font-family:'Outfit',sans-serif;font-size:26px;font-weight:800}
.sub{color:var(--sub);font-size:13px;margin-top:4px;line-height:1.5;max-width:760px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:18px 0 12px}
input,select,textarea{background:var(--card2);border:1px solid var(--border2);border-radius:8px;color:var(--text);
      padding:7px 9px;font-family:var(--sans);font-size:13px}
.bar input{flex:1 1 220px}
.btn{background:var(--lime);color:#0b0f08;border:none;border-radius:8px;padding:8px 14px;font-weight:700;font-size:13px;
     cursor:pointer;font-family:var(--sans)}
.btn.ghost{background:transparent;color:var(--sub);border:1px solid var(--border2)}
.btn.danger{background:transparent;color:var(--danger);border:1px solid var(--border2)}
.btn:disabled{opacity:.5;cursor:default}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:start;padding:9px 12px;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}
.muted{color:var(--muted);font-size:12px}
.chip{display:inline-block;border:1px solid var(--border2);border-radius:10px;padding:1px 8px;font-size:11.5px;color:var(--sub)}
.chip.learned{color:var(--blue)}.chip.import{color:var(--warn)}.chip.seed{color:var(--muted)}
.empty{color:var(--muted);font-size:13px;padding:22px 12px;text-align:center}
.modalbg{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;justify-content:center;z-index:50;padding:16px}
.modalbg.open{display:flex}
.modal{background:var(--card);border:1px solid var(--border2);border-radius:14px;padding:18px;width:100%;max-width:460px;
       display:flex;flex-direction:column;gap:10px}
.modal h3{font-family:'Outfit',sans-serif;font-size:18px}
.modal label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--sub)}
.picklist{max-height:180px;overflow:auto;border:1px solid var(--border);border-radius:8px}
.picklist button{display:block;width:100%;text-align:start;background:none;border:none;border-bottom:1px solid var(--border);
       color:var(--text);padding:7px 9px;font-family:var(--sans);font-size:13px;cursor:pointer}
.picklist button:hover,.picklist button.on{background:var(--card2)}
.msg{font-size:12.5px;line-height:1.5}.msg.err{color:var(--danger)}.msg.warn{color:var(--warn)}
.actions{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}
a.btn{text-decoration:none;display:inline-block}
.st-add{color:var(--green)}.st-duplicate{color:var(--muted)}.st-conflict{color:var(--warn)}.st-unmatched{color:var(--danger)}
</style>
<script src="/static/auth-guard.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_vocabulary") + """
<div class="wrap">
    <div class="title">Vocabulary</div>
    <div class="sub">Local names, nicknames and abbreviations people use for products, clients, farms and more. Ask, PDF
        invoices, documents and the field inbox all understand them. An alias means one record of its type.</div>
    <div class="bar">
        <input id="q" type="search" placeholder="Search aliases, records or notes…">
        <select id="type"><option value="">All types</option></select>
        <a class="btn ghost" id="export" href="/vocabulary/api/export">Export CSV</a>
        <button class="btn ghost" id="import" hidden>Import CSV</button>
        <input type="file" id="import-file" accept=".csv,text/csv,.txt" hidden>
        <button class="btn" id="add" hidden>+ Add alias</button>
    </div>
    <div class="card"><table>
        <thead><tr><th>Alias</th><th>Type</th><th>Means</th><th>Note</th><th>Source</th><th id="act-h"></th></tr></thead>
        <tbody id="rows"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody>
    </table></div>
</div>

<div class="modalbg" id="dlg"><div class="modal">
    <h3 id="dlg-title">Add alias</h3>
    <label>Type<select id="f-type"></select></label>
    <label>Alias<input id="f-alias" maxlength="150" placeholder="e.g. خس بلدي"></label>
    <div id="f-record-box">
        <label>Means<input id="f-search" type="search" placeholder="Search records…"></label>
        <div class="picklist" id="f-list"></div>
        <div class="muted" id="f-picked"></div>
    </div>
    <div id="f-area-box" hidden>
        <label>Area group<select id="f-group"></select></label>
        <label id="f-new-box" hidden>New group name<input id="f-new" maxlength="150" placeholder="e.g. sharm"></label>
    </div>
    <label>Note (optional)<input id="f-note" maxlength="500"></label>
    <div class="msg" id="f-msg"></div>
    <div class="actions">
        <button class="btn ghost" id="f-cancel">Cancel</button>
        <button class="btn" id="f-save">Save</button>
    </div>
</div></div>
<div class="modalbg" id="imp"><div class="modal" style="max-width:820px">
    <h3>Import preview</h3>
    <div class="muted">Nothing is saved until you press Import. Columns: alias, type, record (name or id; the group
        for an area), note.</div>
    <div class="muted" id="imp-sum"></div>
    <div class="card" style="max-height:52vh;overflow:auto"><table>
        <thead><tr><th>Row</th><th>Alias</th><th>Type</th><th>Means</th><th>Result</th></tr></thead>
        <tbody id="imp-rows"></tbody>
    </table></div>
    <div class="msg" id="imp-msg"></div>
    <div class="actions">
        <button class="btn ghost" id="imp-cancel">Cancel</button>
        <button class="btn" id="imp-go">Import</button>
    </div>
</div></div>
<script type="application/json" id="vocab-cfg">""" + cfg + """</script>
<script src="/static/vocabulary.js"></script>
</body>
</html>"""
