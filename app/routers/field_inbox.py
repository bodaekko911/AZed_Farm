"""Field inbox — what workers sent from Telegram, waiting for a manager to confirm (record) or reject.

Confirming records through the same code as the web pages (document_reader.record_*_with), with every check and the
CONFIRMING manager's permissions: the inbox page and the review action, plus the permission for the record itself.
Nobody reviews what they sent themselves. The Telegram bot's admin settings live on this page too (admins only).
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.navigation import render_app_header
from app.core.permissions import get_current_user, has_permission, require_permission
from app.database import get_async_session
from app.models.user import User
from app.services import document_reader as reader
from app.services import field_service as field

router = APIRouter(prefix="/field-inbox", tags=["Field inbox"],
                   dependencies=[Depends(require_permission("page_field_inbox"))])

STATUSES = ("in_review", "confirmed", "rejected")


class RejectIn(BaseModel):
    reason: str = Field(..., max_length=300)


@router.get("/api/pending-count")
async def pending_count(db: AsyncSession = Depends(get_async_session)):
    return {"count": await field.pending_count(db)}


@router.get("/api/drafts")
async def drafts(status: str = "in_review", db: AsyncSession = Depends(get_async_session),
                 user: User = Depends(get_current_user)):
    if status not in STATUSES:
        raise HTTPException(status_code=400, detail="Unknown status")
    return {"drafts": await field.list_drafts(db, status), "can_review": has_permission(user, "action_field_drafts_review"),
            "is_admin": user.role == "admin"}


@router.get("/api/drafts/{draft_id}")
async def draft(draft_id: int, db: AsyncSession = Depends(get_async_session), user: User = Depends(get_current_user)):
    out = await field.get_draft(db, draft_id)
    own = out.pop("chat_user_id") == user.id
    # Your own submission: blocked, except for an admin (who is told it's their own).
    out["own"] = own and user.role != "admin"
    out["own_admin"] = own and user.role == "admin"
    out["is_admin"] = user.role == "admin"            # only an admin may change a line's product
    out["can_review"] = (has_permission(user, "action_field_drafts_review") and not out["own"] and out["kind"] in reader.KINDS
                         and all(has_permission(user, p) for p in reader.KINDS[out["kind"]]["permissions"]))
    return out


@router.post("/api/drafts/{draft_id}/confirm")
async def confirm(draft_id: int, data: dict, db: AsyncSession = Depends(get_async_session),
                  user: User = Depends(get_current_user)):
    return await field.confirm(db, user, draft_id, data)


@router.post("/api/drafts/{draft_id}/reject")
async def reject(draft_id: int, data: RejectIn, db: AsyncSession = Depends(get_async_session),
                 user: User = Depends(get_current_user)):
    return await field.reject(db, user, draft_id, data.reason)


@router.get("/api/search")
async def search(kind: str, q: str = "", db: AsyncSession = Depends(get_async_session),
                 user: User = Depends(get_current_user)):
    """Suppliers or products for the review cards' pickers — same rules as Ask's."""
    return {"results": await reader.search(db, user, kind, q)}


@router.get("/", response_class=HTMLResponse)
def page(current_user: User = Depends(get_current_user)):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Field inbox — AZed Farm</title>
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
.sub{color:var(--sub);font-size:13px;margin-top:4px;line-height:1.5}
.tabs{display:flex;gap:6px;margin:18px 0 12px;flex-wrap:wrap}
.tab{background:var(--card);border:1px solid var(--border);color:var(--sub);border-radius:20px;padding:6px 14px;font-size:13px;cursor:pointer;font-family:var(--sans)}
.tab.on{border-color:var(--lime);color:var(--text)}
.layout{display:grid;grid-template-columns:320px 1fr;gap:16px;align-items:start}
@media (max-width:820px){.layout{grid-template-columns:1fr}}
.list{display:flex;flex-direction:column;gap:8px}
.item{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 12px;cursor:pointer;text-align:start;color:var(--text);font-family:var(--sans)}
.item:hover,.item.on{border-color:var(--lime)}
.item-top{display:flex;justify-content:space-between;gap:8px;font-size:13px;font-weight:600}
.item-sub{color:var(--muted);font-size:12px;margin-top:3px}
.warn-chip{display:inline-block;background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn);border-radius:8px;padding:1px 7px;font-size:11px;margin:4px 4px 0 0}
.panel{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px 16px;min-height:200px}
.empty{color:var(--muted);font-size:13px;padding:20px 4px}
.meta{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--sub);margin-bottom:8px}
.quote{background:var(--card2);border:1px solid var(--border);border-radius:10px;padding:8px 10px;font-size:13px;margin:6px 0;white-space:pre-wrap}
.quote-label{color:var(--muted);font-size:11.5px;margin-bottom:2px}
.photos{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.photos img{height:120px;border-radius:8px;border:1px solid var(--border2);background:#fff;cursor:zoom-in}
.photos img.big{height:auto;max-width:100%;cursor:zoom-out}
.reject{margin-top:14px;padding-top:12px;border-top:1px solid var(--border);display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.reject input{flex:1;min-width:200px;background:var(--card2);border:1px solid var(--border2);border-radius:8px;color:var(--text);padding:7px 9px;font-family:var(--sans);font-size:13px}
.note{color:var(--warn);font-size:12.5px;margin:8px 0}
details.admin{margin-top:28px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 14px}
details.admin summary{cursor:pointer;color:var(--sub);font-size:13px}
.admin-body{margin-top:12px;font-size:13px;display:flex;flex-direction:column;gap:14px}
.admin-body h4{font-size:13px;margin-bottom:6px}
.admin-body table{width:100%;border-collapse:collapse;font-size:12.5px}
.admin-body th,.admin-body td{text-align:start;padding:5px 6px;border-bottom:1px solid var(--border);vertical-align:top}
.admin-body th{color:var(--muted);font-weight:500}
.admin-body select,.admin-body input{background:var(--card2);border:1px solid var(--border2);border-radius:7px;color:var(--text);padding:4px 6px;font-family:var(--sans);font-size:12.5px}
.kinds{display:flex;gap:10px;flex-wrap:wrap}
.kinds label{display:inline-flex;gap:4px;align-items:center}
.code{font-family:var(--mono);font-size:18px;letter-spacing:2px;color:var(--green)}
.ok{color:var(--green)}.bad{color:var(--danger)}
</style>
<link rel="stylesheet" href="/static/doc-cards.css">
<script src="/static/auth-guard.js"></script>
<script src="/static/ask-actions.js"></script>
<script src="/static/ask-documents.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_field_inbox") + r"""
<div class="wrap">
    <div class="title">Field inbox</div>
    <div class="sub">What workers sent from the Telegram bot. Nothing is recorded until you confirm it here — with the same
        checks as the web pages. Rejecting needs a short reason; the worker is told either way.</div>
    <div class="tabs" id="tabs">
        <button class="tab on" data-status="in_review">Waiting for review</button>
        <button class="tab" data-status="confirmed">Confirmed</button>
        <button class="tab" data-status="rejected">Rejected</button>
    </div>
    <div class="layout">
        <div class="list" id="list"><div class="empty">Loading…</div></div>
        <div class="panel" id="panel"><div class="empty">Pick a submission on the left.</div></div>
    </div>
    <details class="admin" id="admin" hidden>
        <summary>Telegram bot · admin</summary>
        <div class="admin-body">
            <div>
                <h4>Status</h4>
                <div id="bot-status">Loading…</div>
                <div class="act-buttons"><button class="act-btn" id="register">Register webhook</button></div>
                <div class="act-status" id="register-out"></div>
            </div>
            <div>
                <h4>Link a worker</h4>
                <div class="inv-row">
                    <select id="link-who"></select>
                    <select id="link-user" title="Their AZed login, if they have one — so they can't confirm their own submissions"></select>
                    <select id="link-lang"><option value="ar">العربية</option><option value="en">English</option></select>
                    <label>Daily limit <input type="number" id="link-limit" value="30" min="1" max="500" style="width:70px"></label>
                </div>
                <div class="kinds" id="link-kinds"></div>
                <div class="act-buttons"><button class="act-btn act-primary" id="link-make">Create a one-time code</button></div>
                <div class="act-status" id="link-out"></div>
            </div>
            <div>
                <h4>Linked chats</h4>
                <table><thead><tr><th>Person</th><th>Telegram</th><th>May send</th><th>Today</th><th>Limit</th><th>Language</th><th></th></tr></thead>
                    <tbody id="chats"></tbody></table>
            </div>
        </div>
    </details>
</div>
<script src="/static/field-inbox.js"></script>
</body>
</html>"""
