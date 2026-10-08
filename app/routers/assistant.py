"""Ask — questions about the business data, answered from the app's reports."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.navigation import render_app_header
from app.core.permissions import get_current_user, has_permission, require_admin, require_permission
from app.database import get_async_session
from app.models.user import User
from app.services import assistant_service

router = APIRouter(
    prefix="/assistant",
    tags=["Assistant"],
    dependencies=[Depends(require_permission("page_assistant"))],
)

TOOL_LABELS = {
    "sales_summary": "Sales", "product_profitability": "Product profitability", "profit_and_loss": "P&L",
    "expenses": "Expenses", "products": "Products & stock", "b2b_balances": "B2B balances",
    "payroll": "Payroll", "farm_harvest": "Farm harvest", "sales_trend": "Sales trend",
    "stock": "Inventory", "spoilage": "Spoilage", "suppliers": "Suppliers", "pos_customers": "POS customers",
    "account_balances": "Account balances",
}


class AskRequest(BaseModel):
    question: str = Field(..., max_length=2000)
    history: Optional[list[dict]] = None


class LimitRequest(BaseModel):
    limit: Optional[int] = Field(None, ge=0, le=1000)   # None = back to the default; 0 = no limit


@router.get("/api/status")
async def status(db: AsyncSession = Depends(get_async_session), user: User = Depends(get_current_user)):
    used, limit = await assistant_service.limit_state(db, user)
    return {
        "configured": assistant_service.is_configured(),
        "daily_limit": limit or None,
        "questions_left": max(limit - used, 0) if limit else None,
        "can_look_up": [TOOL_LABELS[t] for t in assistant_service.allowed_tools(user)],
        "is_admin": user.role == "admin",
    }


@router.post("/api/ask")
async def ask(data: AskRequest, db: AsyncSession = Depends(get_async_session), user: User = Depends(get_current_user)):
    result = await assistant_service.ask(db, user, data.question, data.history)
    result["lookups"] = [TOOL_LABELS.get(l["tool"], l["tool"]) for l in result["lookups"]]
    _used, limit = await assistant_service.limit_state(db, user)
    result["daily_limit"] = limit or None
    return result


# ── Admin: usage and limits ─────────────────────────────────────────────────

async def _target(db: AsyncSession, user_id: int) -> User:
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    return target


@router.get("/api/admin/usage")
async def admin_usage(db: AsyncSession = Depends(get_async_session), _admin: User = Depends(require_admin)):
    users = (await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.name))).scalars().all()
    usage = await assistant_service.usage_today(db)
    rows = []
    for u in users:
        r = usage.get(u.id)
        if not r and not has_permission(u, "page_assistant"):
            continue
        r = r or (await assistant_service.usage_today(db, [u.id]))[u.id]
        rows.append({
            "user_id": u.id, "name": u.name, "role": u.role,
            "used": r["used"], "asked_today": r["asked_today"], "tokens": r["tokens"],
            "limit": r["limit"] or None, "custom_limit": r["custom_limit"],
            "questions_left": max(r["limit"] - r["used"], 0) if r["limit"] else None,
            "last_question_at": r["last_question_at"].isoformat() if r["last_question_at"] else None,
        })
    rows.sort(key=lambda x: (-x["asked_today"], x["name"].lower()))
    return {"default_limit": assistant_service.default_limit() or None, "users": rows}


@router.post("/api/admin/reset/{user_id}")
async def admin_reset(user_id: int, db: AsyncSession = Depends(get_async_session), admin: User = Depends(require_admin)):
    target = await _target(db, user_id)
    await assistant_service.reset_limit(db, admin, target)
    return {"ok": True, "message": f"{target.name} can ask again today."}


@router.post("/api/admin/reset-all")
async def admin_reset_all(db: AsyncSession = Depends(get_async_session), admin: User = Depends(require_admin)):
    await assistant_service.reset_limit(db, admin, None)
    return {"ok": True, "message": "Today's questions were reset for everyone."}


@router.post("/api/admin/limit/{user_id}")
async def admin_set_limit(user_id: int, data: LimitRequest, db: AsyncSession = Depends(get_async_session),
                          admin: User = Depends(require_admin)):
    target = await _target(db, user_id)
    await assistant_service.set_limit(db, admin, target, data.limit)
    return {"ok": True}


@router.get("/", response_class=HTMLResponse)
def assistant_ui(current_user: User = Depends(require_permission("page_assistant"))):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script src="/static/theme-init.js"></script>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ask — AZed Farm</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&family=Outfit:wght@400;600;800&family=Cairo:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#08090c;--card:#0f1311;--card2:#151a17;--border:rgba(255,255,255,.07);--border2:rgba(255,255,255,.13);
      --green:#7ecb6f;--lime:#84cc16;--blue:#6a9fd4;--warn:#e0a84a;--danger:#e06c75;--text:#e8eae0;--sub:#a3aa98;--muted:#6b7363;
      --sans:'DM Sans','Cairo',sans-serif;--mono:'DM Mono',monospace}
body.light{--bg:#f4f5ef;--card:#ffffff;--card2:#eef0e8;--border:rgba(0,0,0,.08);--border2:rgba(0,0,0,.14);
      --green:#0f8a43;--text:#1a1e14;--sub:#4a5040;--muted:#8a9080}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh}
.wrap{max-width:860px;margin:0 auto;padding:28px 16px 150px}
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.title{font-family:'Outfit',sans-serif;font-size:26px;font-weight:800}
.sub{color:var(--sub);font-size:13px;margin-top:4px;line-height:1.5}
.ghost{background:var(--card);border:1px solid var(--border2);color:var(--sub);border-radius:10px;padding:7px 12px;font-size:12.5px;
       cursor:pointer;font-family:var(--sans);white-space:nowrap}
.ghost:hover{color:var(--text);border-color:var(--lime)}
.ghost:disabled{opacity:.5;cursor:default}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}
.chip{background:var(--card);border:1px solid var(--border);color:var(--sub);border-radius:20px;padding:7px 13px;font-size:12.5px;cursor:pointer;font-family:var(--sans)}
.chip:hover{border-color:var(--border2);color:var(--text)}
.msg{margin:14px 0;display:flex}
.msg.user{justify-content:flex-end}
.bubble{max-width:85%;padding:12px 15px;border-radius:14px;line-height:1.65;font-size:14px;word-wrap:break-word;overflow-x:auto}
.user .bubble,.err .bubble,.thinking .bubble{white-space:pre-wrap}
.user .bubble{background:color-mix(in srgb,var(--lime) 16%,transparent);border:1px solid color-mix(in srgb,var(--lime) 30%,transparent)}
.bot .bubble{background:var(--card);border:1px solid var(--border)}
.md p{margin:0 0 8px}.md p:last-child{margin-bottom:0}
.md h3,.md h4{font-family:'Outfit',sans-serif;font-size:15px;margin:10px 0 6px}
.md ul,.md ol{margin:4px 0 8px;padding-inline-start:22px}
.md li{margin:2px 0}
.md strong{color:var(--text);font-weight:700}
.md code{font-family:var(--mono);font-size:12.5px;background:var(--card2);padding:1px 5px;border-radius:5px}
.md table{border-collapse:collapse;margin:8px 0;font-size:13px;width:100%}
.md th,.md td{border:1px solid var(--border2);padding:5px 9px;text-align:start;vertical-align:top}
.md th{background:var(--card2);font-weight:600}
.md td.num{font-family:var(--mono);text-align:end;white-space:nowrap}
/* Charts in answers — validated categorical slots 1–3 for each theme's card surface */
.ask-chart{--chart-1:#3987e5;--chart-2:#d95926;--chart-3:#199e70;--chart-grid:rgba(255,255,255,.07);--chart-axis:rgba(255,255,255,.2);
           position:relative;margin:10px 0 12px;min-width:240px}
body.light .ask-chart{--chart-1:#2a78d6;--chart-2:#eb6834;--chart-3:#1baf7a;--chart-grid:rgba(0,0,0,.07);--chart-axis:rgba(0,0,0,.22)}
.ask-chart svg{display:block;overflow:visible}
.bubble.has-chart{width:85%}
.ask-chart-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.ask-chart-title{font-size:13px;font-weight:600;color:var(--text)}
.ask-chart-legend{display:flex;gap:12px;flex-wrap:wrap;font-size:11.5px;color:var(--sub)}
.ask-chart-legend>span{display:inline-flex;align-items:center;gap:5px}
.ask-chart-key{display:inline-block;width:10px;height:10px;border-radius:3px;flex:none}
.ask-chart-tip{position:absolute;pointer-events:none;background:var(--card2);border:1px solid var(--border2);border-radius:8px;
               padding:7px 10px;font-size:12px;color:var(--text);box-shadow:0 6px 18px rgba(0,0,0,.25);z-index:3;white-space:nowrap}
.ask-chart-tip-head{color:var(--muted);margin-bottom:3px}
.ask-chart-tip-row{display:flex;align-items:center;gap:6px}
.ask-chart-tip-row b{margin-left:auto;padding-left:12px;font-family:var(--mono);font-weight:500}
.meta{font-size:11px;color:var(--muted);margin-top:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.copy{background:none;border:none;color:var(--muted);cursor:pointer;font-size:11px;font-family:var(--sans);text-decoration:underline}
.copy:hover{color:var(--text)}
.err .bubble{border-color:color-mix(in srgb,var(--danger) 40%,transparent);color:var(--danger)}
.thinking .bubble{color:var(--muted);font-size:13px}
.bar{position:fixed;left:0;right:0;bottom:0;background:linear-gradient(transparent,var(--bg) 30%);padding:24px 16px 18px}
.bar-inner{max-width:860px;margin:0 auto;display:flex;gap:8px}
textarea{flex:1;resize:none;height:52px;background:var(--card);border:1px solid var(--border2);border-radius:12px;color:var(--text);
         font-family:var(--sans);font-size:14px;padding:14px;outline:none;overflow:hidden}
textarea:focus{border-color:var(--lime)}
button.send{background:linear-gradient(135deg,var(--lime),var(--green));border:none;border-radius:12px;padding:0 20px;font-weight:700;
            color:#0a1a00;cursor:pointer;font-family:var(--sans)}
button.send:disabled{opacity:.5;cursor:default}
.left{font-size:11.5px;color:var(--muted);max-width:860px;margin:6px auto 0}
.notice{background:color-mix(in srgb,var(--warn) 9%,transparent);border:1px solid color-mix(in srgb,var(--warn) 30%,transparent);color:var(--warn);
        border-radius:10px;padding:11px 14px;font-size:13px;margin-top:14px}
.admin{margin-top:16px;background:var(--card);border:1px solid var(--border);border-radius:12px}
.admin summary{cursor:pointer;padding:11px 14px;font-size:13px;font-weight:600;color:var(--sub)}
.admin[open] summary{border-bottom:1px solid var(--border)}
.admin-body{padding:12px 14px;overflow-x:auto}
.admin-tools{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;font-size:12px;color:var(--muted);flex-wrap:wrap}
.admin table{width:100%;border-collapse:collapse;font-size:12.5px}
.admin th{color:var(--muted);font-weight:500;text-align:start;padding:6px 8px;border-bottom:1px solid var(--border)}
.admin td{padding:7px 8px;border-bottom:1px solid var(--border);vertical-align:middle}
.admin td.n{font-family:var(--mono);white-space:nowrap}
.admin .full{color:var(--danger);font-weight:600}
.admin input{width:64px;background:var(--card2);border:1px solid var(--border2);border-radius:7px;color:var(--text);padding:4px 6px;font-family:var(--mono);font-size:12px}
.admin .acts{display:flex;gap:6px;white-space:nowrap}
.admin .ghost{padding:4px 9px;font-size:11.5px}
.toast{position:fixed;top:18px;left:50%;transform:translateX(-50%);background:var(--card2);border:1px solid var(--lime);color:var(--text);
       padding:9px 16px;border-radius:10px;font-size:13px;z-index:50;display:none}
</style>
<script src="/static/auth-guard.js"></script>
<script src="/static/ask-chart.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_assistant") + r"""
<div class="wrap">
    <div class="top">
        <div>
            <div class="title">Ask</div>
            <div class="sub">Ask about sales and trends, profit, expenses, stock, spoilage, suppliers, customers, B2B balances,
                payroll or harvest — in Arabic or English. Answers come from the same reports you can open yourself; nothing is changed.
                <span id="scope"></span></div>
        </div>
        <button class="ghost" id="newchat" title="Start a new conversation">+ New chat</button>
    </div>
    <div id="notice"></div>
    <details class="admin" id="admin" style="display:none">
        <summary>Usage today · admin</summary>
        <div class="admin-body">
            <div class="admin-tools">
                <span id="admin-default"></span>
                <button class="ghost" id="reset-all">Reset everyone's questions</button>
            </div>
            <table>
                <thead><tr><th>User</th><th>Used today</th><th>Tokens</th><th>Last question</th><th>Own limit</th><th></th></tr></thead>
                <tbody id="admin-rows"><tr><td colspan="6">Loading…</td></tr></tbody>
            </table>
        </div>
    </details>
    <div class="chips" id="chips">
        <button class="chip">How much did we sell this month compared to last month?</button>
        <button class="chip">Which products lost money last month?</button>
        <button class="chip">كم صرفنا على مشروع SPC هذا الشهر؟</button>
        <button class="chip">What was our best sales day in the last 30 days?</button>
        <button class="chip">Which B2B clients owe us the most?</button>
        <button class="chip">ما المنتجات التي قاربت على النفاد؟</button>
        <button class="chip">How much did spoilage cost us this month, and on which products?</button>
        <button class="chip">How much do we owe suppliers?</button>
    </div>
    <div id="log"></div>
</div>
<div class="bar">
    <div class="bar-inner">
        <textarea id="q" placeholder="Ask a question… / اسأل سؤالاً…" dir="auto" maxlength="1000"></textarea>
        <button class="send" id="send">Ask</button>
    </div>
    <div class="left" id="left"></div>
</div>
<div class="toast" id="toast"></div>
<script>
const STORE = "azed-ask-chat";
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const isArabic = s => /[؀-ۿ]/.test(s || "");
const log = document.getElementById("log");
let history = [];   // {role, content} sent back for follow-ups
let shown = [];     // what is on screen, kept for a page refresh

function save(){ try{ sessionStorage.setItem(STORE, JSON.stringify({history, shown})); }catch(e){} }
function toast(text){
    const t = document.getElementById("toast"); t.innerText = text; t.style.display = "block";
    clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 2200);
}

// Small, safe Markdown: everything is escaped first, then only tables, lists, headings, bold and code are formatted.
function inline(s){
    return s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}
function cells(line){ return line.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim()); }
const isNum = s => /^[-+]?[\d,.]+\s*(%|EGP|ج\.م)?$/.test(s.replace(/<[^>]+>/g, "").trim());
function md(text){
    const lines = esc(text).replace(/\r/g, "").split("\n");
    let out = "", i = 0;
    while(i < lines.length){
        const line = lines[i];
        if(/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i+1])){
            const head = cells(line); i += 2;
            let rows = "";
            while(i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])){
                rows += "<tr>" + cells(lines[i]).map(c => `<td${isNum(c) ? ' class="num"' : ""}>${inline(c)}</td>`).join("") + "</tr>"; i++;
            }
            out += `<table><thead><tr>${head.map(h => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table>`;
            continue;
        }
        if(/^\s*[-*•]\s+/.test(line)){
            let items = "";
            while(i < lines.length && /^\s*[-*•]\s+/.test(lines[i])){ items += `<li>${inline(lines[i].replace(/^\s*[-*•]\s+/, ""))}</li>`; i++; }
            out += `<ul>${items}</ul>`; continue;
        }
        if(/^\s*\d+[.)]\s+/.test(line)){
            let items = "";
            while(i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])){ items += `<li>${inline(lines[i].replace(/^\s*\d+[.)]\s+/, ""))}</li>`; i++; }
            out += `<ol>${items}</ol>`; continue;
        }
        const h = line.match(/^\s*(#{1,4})\s+(.*)$/);
        if(h){ out += `<h${h[1].length > 2 ? 4 : 3}>${inline(h[2])}</h${h[1].length > 2 ? 4 : 3}>`; i++; continue; }
        if(!line.trim()){ i++; continue; }
        let para = [];
        while(i < lines.length && lines[i].trim() && !/^\s*([-*•]\s|\d+[.)]\s|#{1,4}\s|\|)/.test(lines[i])){ para.push(inline(lines[i])); i++; }
        if(!para.length){ para.push(inline(line)); i++; }
        out += `<p>${para.join("<br>")}</p>`;
    }
    return out;
}

function add(role, text, meta, cls, keep){
    const div = document.createElement("div");
    div.className = `msg ${role} ${cls||""}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble" + (role === "bot" && !cls ? " md" : "");
    bubble.dir = isArabic(text) ? "rtl" : "ltr";
    let charts = [];
    if(role === "bot" && !cls){
        const parts = window.AskChart ? AskChart.extract(text) : {text, specs: []};
        charts = parts.specs;
        bubble.innerHTML = window.AskChart ? AskChart.slots(md(parts.text)) : md(parts.text);
    }else bubble.innerHTML = esc(text);
    if(role === "bot" && !cls){
        const m = document.createElement("div"); m.className = "meta"; m.dir = "ltr";
        m.innerHTML = (meta ? `<span>${esc(meta)}</span>` : "") + `<button class="copy">Copy</button>`;
        m.querySelector(".copy").onclick = async e => {
            try{ await navigator.clipboard.writeText(window.AskChart ? AskChart.strip(text) : text); e.target.innerText = "Copied"; setTimeout(() => e.target.innerText = "Copy", 1500); }catch(_){}
        };
        bubble.appendChild(m);
    }
    div.appendChild(bubble);
    log.appendChild(div);
    if(charts.length){ bubble.classList.add("has-chart"); AskChart.mount(bubble, charts); }
    div.scrollIntoView({behavior:"smooth", block:"end"});
    if(keep){ shown.push({role, text, meta, cls}); save(); }
    return div;
}
function showLeft(n, limit){
    document.getElementById("left").innerText = (n === null || n === undefined) ? "" : `${n} of ${limit} questions left today`;
}
async function loadStatus(){
    try{
        const s = await (await fetch("/assistant/api/status")).json();
        showLeft(s.questions_left, s.daily_limit);
        document.getElementById("scope").innerText = s.can_look_up.length ? `You can ask about: ${s.can_look_up.join(", ")}.` : "";
        if(!s.configured) document.getElementById("notice").innerHTML =
            `<div class="notice">The assistant isn't set up yet. An admin needs to add ASSISTANT_API_KEY and ASSISTANT_MODEL to the server settings.</div>`;
        if(s.is_admin) document.getElementById("admin").style.display = "";
    }catch(e){}
}
async function send(text){
    const q = (text ?? document.getElementById("q").value).trim();
    if(!q) return;
    document.getElementById("q").value = "";
    document.getElementById("chips").style.display = "none";
    const btn = document.getElementById("send"); btn.disabled = true;
    add("user", q, "", "", true);
    const wait = add("bot", isArabic(q) ? "جارٍ البحث في التقارير…" : "Looking it up…", "", "thinking");
    try{
        const r = await fetch("/assistant/api/ask", {method:"POST", headers:{"Content-Type":"application/json"},
                                                      body: JSON.stringify({question:q, history: history.slice(-10)})});
        const data = await r.json().catch(()=>({}));
        wait.remove();
        if(!r.ok){ add("bot", data.detail || "Something went wrong.", "", "err"); return; }
        // Charts stay out of the history sent back: the model doesn't need its own chart data again.
        history.push({role:"user", content:q}, {role:"assistant", content: window.AskChart ? AskChart.strip(data.answer) : data.answer});
        add("bot", data.answer, data.lookups.length ? `Looked up: ${[...new Set(data.lookups)].join(", ")}` : "", "", true);
        if(data.questions_left !== null) showLeft(data.questions_left, data.daily_limit);
        if(document.getElementById("admin").open) loadUsage();
    }catch(e){
        wait.remove(); add("bot", "Network error — try again.", "", "err");
    }finally{ btn.disabled = false; document.getElementById("q").focus(); }
}
function newChat(){
    history = []; shown = []; save();
    log.innerHTML = "";
    document.getElementById("chips").style.display = "";
    document.getElementById("q").focus();
}
function restore(){
    try{
        const saved = JSON.parse(sessionStorage.getItem(STORE) || "null");
        if(!saved || !saved.shown || !saved.shown.length) return;
        history = saved.history || [];
        saved.shown.forEach(m => add(m.role, m.text, m.meta, m.cls, false));
        shown = saved.shown;
        document.getElementById("chips").style.display = "none";
    }catch(e){}
}

// ── Admin: usage and limits ──
async function adminPost(url, body){
    const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body || {})});
    const data = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(data.detail || "Failed");
    return data;
}
async function loadUsage(){
    const tbody = document.getElementById("admin-rows");
    try{
        const d = await (await fetch("/assistant/api/admin/usage")).json();
        document.getElementById("admin-default").innerText =
            d.default_limit ? `Default: ${d.default_limit} questions per user per day. Leave "own limit" empty to use it; 0 = no limit.`
                            : `No default limit is set. "Own limit" 0 = no limit.`;
        if(!d.users.length){ tbody.innerHTML = `<tr><td colspan="6">No users can use Ask.</td></tr>`; return; }
        tbody.innerHTML = d.users.map(u => {
            const used = u.limit ? `${u.used} / ${u.limit}` : `${u.used}`;
            const full = u.limit && u.used >= u.limit;
            const extra = u.asked_today !== u.used ? ` <span style="color:var(--muted)">(${u.asked_today} asked)</span>` : "";
            const last = u.last_question_at ? new Date(u.last_question_at).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}) : "—";
            return `<tr>
                <td>${esc(u.name)} <span style="color:var(--muted)">· ${esc(u.role)}</span></td>
                <td class="n ${full ? "full" : ""}">${used}${extra}</td>
                <td class="n">${u.tokens ? u.tokens.toLocaleString() : "—"}</td>
                <td class="n">${last}</td>
                <td><input type="number" min="0" max="1000" placeholder="default" value="${u.custom_limit ?? ""}" data-limit="${u.user_id}"></td>
                <td class="acts">
                    <button class="ghost" data-save="${u.user_id}">Save limit</button>
                    <button class="ghost" data-reset="${u.user_id}" ${u.used ? "" : "disabled"}>Reset</button>
                </td></tr>`;
        }).join("");
        tbody.querySelectorAll("[data-reset]").forEach(b => b.onclick = async () => {
            b.disabled = true;
            try{ toast((await adminPost(`/assistant/api/admin/reset/${b.dataset.reset}`)).message); await loadUsage(); loadStatus(); }
            catch(e){ toast(e.message); b.disabled = false; }
        });
        tbody.querySelectorAll("[data-save]").forEach(b => b.onclick = async () => {
            const raw = tbody.querySelector(`[data-limit="${b.dataset.save}"]`).value.trim();
            try{
                await adminPost(`/assistant/api/admin/limit/${b.dataset.save}`, {limit: raw === "" ? null : Number(raw)});
                toast("Limit saved"); await loadUsage(); loadStatus();
            }catch(e){ toast(e.message); }
        });
    }catch(e){ tbody.innerHTML = `<tr><td colspan="6">Couldn't load usage.</td></tr>`; }
}
document.getElementById("reset-all").onclick = async () => {
    if(!confirm("Reset today's questions for every user?")) return;
    try{ toast((await adminPost("/assistant/api/admin/reset-all")).message); await loadUsage(); loadStatus(); }
    catch(e){ toast(e.message); }
};
document.getElementById("admin").addEventListener("toggle", e => { if(e.target.open) loadUsage(); });

document.getElementById("send").onclick = () => send();
document.getElementById("newchat").onclick = newChat;
document.getElementById("q").addEventListener("keydown", e => { if(e.key === "Enter" && !e.shiftKey){ e.preventDefault(); send(); } });
document.querySelectorAll(".chip").forEach(c => c.onclick = () => send(c.innerText));
restore();
loadStatus();
</script>
</body>
</html>
"""
