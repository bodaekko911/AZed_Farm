"""Ask — questions about the business data, answered from the app's reports."""

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.navigation import render_app_header
from app.core.permissions import get_current_user, require_permission
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
    "payroll": "Payroll", "farm_harvest": "Farm harvest",
}


class AskRequest(BaseModel):
    question: str = Field(..., max_length=2000)
    history: Optional[list[dict]] = None


@router.get("/api/status")
async def status(db: AsyncSession = Depends(get_async_session), user: User = Depends(get_current_user)):
    limit = int(settings.ASSISTANT_DAILY_LIMIT or 0)
    used = await assistant_service.questions_today(db, user)
    return {
        "configured": assistant_service.is_configured(),
        "daily_limit": limit or None,
        "questions_left": max(limit - used, 0) if limit else None,
        "can_look_up": [TOOL_LABELS[t] for t in assistant_service.allowed_tools(user)],
    }


@router.post("/api/ask")
async def ask(data: AskRequest, db: AsyncSession = Depends(get_async_session), user: User = Depends(get_current_user)):
    result = await assistant_service.ask(db, user, data.question, data.history)
    result["lookups"] = [TOOL_LABELS.get(l["tool"], l["tool"]) for l in result["lookups"]]
    return result


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
.wrap{max-width:860px;margin:0 auto;padding:28px 16px 140px}
.title{font-family:'Outfit',sans-serif;font-size:26px;font-weight:800}
.sub{color:var(--sub);font-size:13px;margin-top:4px;line-height:1.5}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}
.chip{background:var(--card);border:1px solid var(--border);color:var(--sub);border-radius:20px;padding:7px 13px;font-size:12.5px;cursor:pointer;font-family:var(--sans)}
.chip:hover{border-color:var(--border2);color:var(--text)}
.msg{margin:14px 0;display:flex}
.msg.user{justify-content:flex-end}
.bubble{max-width:85%;padding:12px 15px;border-radius:14px;line-height:1.65;font-size:14px;white-space:pre-wrap;word-wrap:break-word}
.user .bubble{background:color-mix(in srgb,var(--lime) 16%,transparent);border:1px solid color-mix(in srgb,var(--lime) 30%,transparent)}
.bot .bubble{background:var(--card);border:1px solid var(--border)}
.meta{font-size:11px;color:var(--muted);margin-top:6px}
.err .bubble{border-color:color-mix(in srgb,var(--danger) 40%,transparent);color:var(--danger)}
.thinking{color:var(--muted);font-size:13px}
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
</style>
<script src="/static/auth-guard.js"></script>
</head>
<body>
""" + render_app_header(current_user, "page_assistant") + """
<div class="wrap">
    <div class="title">Ask</div>
    <div class="sub">Ask about sales, profit, expenses, stock, B2B balances, payroll or harvest — in Arabic or English.
        Answers come from the same reports you can open yourself; nothing is changed.
        <span id="scope"></span></div>
    <div id="notice"></div>
    <div class="chips" id="chips">
        <button class="chip">How much did we sell this month?</button>
        <button class="chip">Which products lost money last month?</button>
        <button class="chip">كم صرفنا على مشروع SPC هذا الشهر؟</button>
        <button class="chip">Which B2B clients owe us the most?</button>
        <button class="chip">ما المنتجات التي قاربت على النفاد؟</button>
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
<script>
const history = [];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const isArabic = s => /[\\u0600-\\u06FF]/.test(s || "");
const log = document.getElementById("log");

function add(role, text, meta, cls){
    const dir = isArabic(text) ? "rtl" : "ltr";
    const div = document.createElement("div");
    div.className = `msg ${role} ${cls||""}`;
    div.innerHTML = `<div class="bubble" dir="${dir}">${esc(text)}${meta?`<div class="meta" dir="ltr">${esc(meta)}</div>`:""}</div>`;
    log.appendChild(div);
    div.scrollIntoView({behavior:"smooth", block:"end"});
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
    }catch(e){}
}
async function send(text){
    const q = (text ?? document.getElementById("q").value).trim();
    if(!q) return;
    document.getElementById("q").value = "";
    document.getElementById("chips").style.display = "none";
    const btn = document.getElementById("send"); btn.disabled = true;
    add("user", q);
    const wait = add("bot", isArabic(q) ? "جارٍ البحث في التقارير…" : "Looking it up…", "", "thinking");
    try{
        const r = await fetch("/assistant/api/ask", {method:"POST", headers:{"Content-Type":"application/json"},
                                                      body: JSON.stringify({question:q, history: history.slice(-6)})});
        const data = await r.json().catch(()=>({}));
        wait.remove();
        if(!r.ok){ add("bot", data.detail || "Something went wrong.", "", "err"); return; }
        add("bot", data.answer, data.lookups.length ? `Looked up: ${[...new Set(data.lookups)].join(", ")}` : "");
        history.push({role:"user", content:q}, {role:"assistant", content:data.answer});
        if(data.questions_left !== null) showLeft(data.questions_left, (await (await fetch("/assistant/api/status")).json()).daily_limit);
    }catch(e){
        wait.remove(); add("bot", "Network error — try again.", "", "err");
    }finally{ btn.disabled = false; document.getElementById("q").focus(); }
}
document.getElementById("send").onclick = () => send();
document.getElementById("q").addEventListener("keydown", e => { if(e.key === "Enter" && !e.shiftKey){ e.preventDefault(); send(); } });
document.querySelectorAll(".chip").forEach(c => c.onclick = () => send(c.innerText));
loadStatus();
</script>
</body>
</html>
"""
