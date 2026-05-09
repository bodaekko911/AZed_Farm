import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.middleware import get_trusted_client_ip
from app.core.permission_catalog import get_permission_catalog
from app.core.permissions import (
    get_effective_permissions,
    require_admin,
    serialize_permissions,
)
from app.core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    password_needs_rehash,
    try_refresh_access_token,
    verify_password,
)
from app.database import get_async_session
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.user import UserCreate, UserOut, UserLogin
from app.core.rate_limit import limiter

router = APIRouter(tags=["Auth"])


def _redis_client():
    import redis.asyncio as aioredis

    return aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT,
        socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
        retry_on_timeout=False,
    )


@router.get("/", response_class=HTMLResponse)
def login_page():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script src="/static/theme-init.js"></script>
<title>AZed ERP — Business Management Platform</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=Space+Grotesk:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:         #07101F;
  --surface:    #0D1A2E;
  --surface2:   #132035;
  --border:     rgba(255,255,255,0.07);
  --border2:    rgba(255,255,255,0.13);
  --accent:     #00D4FF;
  --accent-dim: rgba(0,212,255,0.10);
  --accent-glow:rgba(0,212,255,0.22);
  --accent2:    #4F8EF7;
  --text:       #EDF2FF;
  --sub:        rgba(237,242,255,0.60);
  --muted:      rgba(237,242,255,0.34);
  --positive:   #22D5A0;
  --warning:    #F0A43A;
  --danger:     #FF5370;
  --sans:       'DM Sans', sans-serif;
  --display:    'Space Grotesk', sans-serif;
  --mono:       'DM Mono', monospace;
  --r:          12px;
  --r-lg:       18px;
}
html[data-theme="light"] {
  --bg:         #EEF3FB;
  --surface:    #FFFFFF;
  --surface2:   #F4F7FF;
  --border:     rgba(0,0,0,0.07);
  --border2:    rgba(0,0,0,0.12);
  --accent:     #006DD9;
  --accent-dim: rgba(0,109,217,0.08);
  --accent-glow:rgba(0,109,217,0.18);
  --accent2:    #3B6FE0;
  --text:       #0B1526;
  --sub:        rgba(11,21,38,0.58);
  --muted:      rgba(11,21,38,0.35);
}

body {
  font-family: var(--sans);
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  overflow-x: hidden;
  transition: background .25s, color .25s;
}
button { cursor: pointer; font-family: inherit; }
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }

/* ─── BG SCENE ─────────────────────────── */
.bg-scene { position: fixed; inset: 0; pointer-events: none; z-index: 0; overflow: hidden; }
.bg-mesh {
  position: absolute; inset: 0;
  background:
    radial-gradient(ellipse 70% 50% at 10% 20%, rgba(0,212,255,.055) 0%, transparent 55%),
    radial-gradient(ellipse 50% 60% at 85% 10%, rgba(79,142,247,.06) 0%, transparent 55%),
    radial-gradient(ellipse 40% 40% at 60% 85%, rgba(0,109,180,.05) 0%, transparent 55%);
}
html[data-theme="light"] .bg-mesh {
  background:
    radial-gradient(ellipse 70% 50% at 10% 20%, rgba(0,109,217,.05) 0%, transparent 55%),
    radial-gradient(ellipse 50% 60% at 85% 10%, rgba(79,142,247,.04) 0%, transparent 55%);
}
.bg-grid {
  position: absolute; inset: 0;
  background-image: linear-gradient(var(--border) 1px,transparent 1px), linear-gradient(90deg,var(--border) 1px,transparent 1px);
  background-size: 56px 56px;
  mask-image: radial-gradient(ellipse 90% 90% at 50% 0%, black 30%, transparent 80%);
  opacity: .5;
}
.bg-orb { position: absolute; border-radius: 50%; filter: blur(90px); animation: orbDrift 16s ease-in-out infinite; pointer-events: none; }
.bg-orb-1 { width:480px;height:480px;background:rgba(0,212,255,.05);top:-120px;left:-120px;animation-delay:0s; }
.bg-orb-2 { width:360px;height:360px;background:rgba(79,142,247,.06);bottom:-80px;right:-60px;animation-delay:-7s; }
html[data-theme="light"] .bg-orb-1 { background:rgba(0,109,217,.05); }
html[data-theme="light"] .bg-orb-2 { background:rgba(79,142,247,.05); }
@keyframes orbDrift { 0%,100%{transform:translate(0,0)} 40%{transform:translate(28px,-22px)} 70%{transform:translate(-18px,14px)} }

/* ─── NAV ───────────────────────────────── */
nav {
  position: sticky; top: 0; z-index: 100;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 48px; height: 66px;
  background: rgba(7,16,31,.82);
  backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
  border-bottom: 1px solid var(--border);
  transition: background .25s;
}
html[data-theme="light"] nav { background: rgba(238,243,251,.88); }

.nav-logo {
  height: 42px; width: auto; object-fit: contain;
  mix-blend-mode: screen; display: block;
}
html[data-theme="light"] .nav-logo { mix-blend-mode: normal; filter: invert(1) hue-rotate(180deg); }

.nav-actions { display:flex; align-items:center; gap:10px; }

.nav-theme-btn {
  width:36px; height:36px; border-radius:8px;
  border:1px solid var(--border2); background:var(--surface);
  color:var(--sub); display:flex; align-items:center; justify-content:center;
  transition: color .2s, background .2s;
}
.nav-theme-btn:hover { color:var(--accent); background:var(--accent-dim); }

.btn-signin-nav {
  height:36px; padding:0 20px; border-radius:8px;
  border:1px solid var(--accent); background:transparent;
  color:var(--accent); font-size:13px; font-weight:600; letter-spacing:.3px;
  transition: background .2s, color .2s, box-shadow .2s;
}
.btn-signin-nav:hover { background:var(--accent); color:#04111E; box-shadow:0 4px 16px var(--accent-glow); }
html[data-theme="light"] .btn-signin-nav:hover { color:#fff; }

/* ─── HERO ──────────────────────────────── */
.hero {
  position: relative; z-index: 1;
  display: flex; flex-direction: column; align-items: center;
  text-align: center; padding: 88px 24px 76px;
}
.hero-logo {
  height: 86px; width: auto; object-fit: contain;
  mix-blend-mode: screen;
  filter: drop-shadow(0 0 32px rgba(0,212,255,.25));
  margin-bottom: 32px;
  animation: fadeUp .6s ease 0s both;
}
html[data-theme="light"] .hero-logo { mix-blend-mode:normal; filter:invert(1) hue-rotate(180deg) drop-shadow(0 2px 12px rgba(0,109,217,.2)); }

.hero-badge {
  display:inline-flex; align-items:center; gap:7px;
  padding:5px 14px; border-radius:999px;
  border:1px solid var(--border2); background:var(--surface);
  font-size:12px; font-weight:500; color:var(--sub);
  margin-bottom:22px; animation: fadeUp .6s ease .08s both;
}
.hero-badge-dot { width:6px;height:6px;border-radius:50%;background:var(--positive);box-shadow:0 0 8px var(--positive); }

.hero-title {
  font-family:var(--display);
  font-size: clamp(36px,6vw,66px);
  font-weight:700; letter-spacing:-2px; line-height:1.08;
  max-width:760px; margin-bottom:20px;
  animation: fadeUp .6s ease .13s both;
}
.hero-title em { font-style:normal; color:var(--accent); }

.hero-sub {
  font-size:clamp(15px,2vw,17px); color:var(--sub);
  line-height:1.75; max-width:520px; margin-bottom:38px;
  animation: fadeUp .6s ease .18s both;
}
.hero-cta-group {
  display:flex; gap:12px; align-items:center; flex-wrap:wrap; justify-content:center;
  animation: fadeUp .6s ease .23s both;
}
.btn-primary {
  height:48px; padding:0 28px; border-radius:10px; border:none;
  background:var(--accent); color:#04111E;
  font-family:var(--display); font-size:15px; font-weight:700;
  transition: opacity .2s, box-shadow .2s, transform .15s;
}
html[data-theme="light"] .btn-primary { color:#fff; }
.btn-primary:hover { opacity:.9; box-shadow:0 6px 24px var(--accent-glow); transform:translateY(-1px); }

.btn-outline {
  height:48px; padding:0 28px; border-radius:10px;
  border:1px solid var(--border2); background:var(--surface);
  color:var(--sub); font-size:15px; font-weight:500;
  transition: color .2s, border-color .2s;
}
.btn-outline:hover { color:var(--text); border-color:var(--accent); }

.hero-stats {
  display:flex; gap:40px; margin-top:56px;
  animation: fadeUp .6s ease .28s both;
  flex-wrap:wrap; justify-content:center;
}
.hero-stat { text-align:center; }
.hero-stat-num { font-family:var(--display); font-size:28px; font-weight:700; letter-spacing:-.5px; }
.hero-stat-num span { color:var(--accent); }
.hero-stat-label { font-size:11px; color:var(--muted); margin-top:3px; letter-spacing:.5px; text-transform:uppercase; }

/* ─── SECTIONS ──────────────────────────── */
section { position:relative; z-index:1; }
.section-tag { display:inline-block; font-size:11px; font-weight:700; letter-spacing:1.5px; text-transform:uppercase; color:var(--accent); margin-bottom:12px; }
.section-title { font-family:var(--display); font-size:clamp(24px,4vw,36px); font-weight:700; letter-spacing:-.8px; line-height:1.15; margin-bottom:14px; }
.section-sub { font-size:16px; color:var(--sub); line-height:1.7; max-width:520px; }

.section-divider { max-width:1160px; margin:0 auto; padding:0 48px; border:none; border-top:1px solid var(--border); }

/* ─── MODULES ───────────────────────────── */
.modules { padding:80px 48px; max-width:1160px; margin:0 auto; }
.modules-header { text-align:center; margin-bottom:50px; }
.modules-header .section-sub { margin:0 auto; }

.modules-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:18px; }

.module-card {
  background:var(--surface); border:1px solid var(--border);
  border-radius:var(--r-lg); padding:26px;
  transition: border-color .2s, box-shadow .2s, transform .2s;
  position:relative; overflow:hidden;
}
.module-card::before {
  content:''; position:absolute; inset:0;
  background:linear-gradient(135deg,var(--accent-dim) 0%,transparent 60%);
  opacity:0; transition:opacity .3s; border-radius:inherit;
}
.module-card:hover { border-color:var(--border2); box-shadow:0 8px 32px rgba(0,0,0,.18); transform:translateY(-2px); }
.module-card:hover::before { opacity:1; }

.module-icon {
  width:42px; height:42px; border-radius:10px;
  background:var(--accent-dim); border:1px solid rgba(0,212,255,.15);
  display:flex; align-items:center; justify-content:center;
  margin-bottom:14px; color:var(--accent);
}
html[data-theme="light"] .module-icon { border-color:rgba(0,109,217,.15); }
.module-icon svg { width:19px; height:19px; }
.module-name { font-family:var(--display); font-size:15px; font-weight:600; margin-bottom:6px; letter-spacing:-.2px; }
.module-desc { font-size:13px; color:var(--sub); line-height:1.6; }
.module-tags { display:flex; flex-wrap:wrap; gap:5px; margin-top:14px; }
.module-tag { font-size:11px; font-weight:500; padding:3px 9px; border-radius:999px; border:1px solid var(--border2); color:var(--muted); font-family:var(--mono); }

/* ─── FEATURES ──────────────────────────── */
.features { padding:80px 48px; max-width:1160px; margin:0 auto; }
.features-inner { display:grid; grid-template-columns:1fr 1fr; gap:60px; align-items:center; }
.features-list { display:flex; flex-direction:column; gap:22px; margin-top:28px; }
.feature-item { display:flex; gap:14px; align-items:flex-start; }
.feature-icon {
  width:36px; height:36px; border-radius:9px;
  background:var(--surface2); border:1px solid var(--border);
  display:flex; align-items:center; justify-content:center;
  flex-shrink:0; color:var(--accent); margin-top:2px;
}
.feature-icon svg { width:16px; height:16px; }
.feature-text-title { font-size:14px; font-weight:600; margin-bottom:3px; }
.feature-text-body { font-size:13px; color:var(--sub); line-height:1.6; }

/* mock dashboard */
.features-visual {
  background:var(--surface); border:1px solid var(--border);
  border-radius:var(--r-lg); padding:24px; position:relative; overflow:hidden;
}
.features-visual::after {
  content:''; position:absolute; top:-60px; right:-60px;
  width:200px; height:200px; border-radius:50%;
  background:var(--accent-dim); filter:blur(60px); pointer-events:none;
}
.visual-header { display:flex; align-items:center; gap:7px; margin-bottom:16px; }
.visual-dot { width:10px;height:10px;border-radius:50%; }
.visual-dot-r{background:#FF5F56}.visual-dot-y{background:#FFBD2E}.visual-dot-g{background:#27C93F}
.visual-title { font-size:12px; color:var(--muted); font-family:var(--mono); margin-left:6px; }
.kpi-row { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:14px; }
.kpi-card { background:var(--surface2); border:1px solid var(--border); border-radius:10px; padding:12px 14px; }
.kpi-label { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:.7px; margin-bottom:4px; }
.kpi-value { font-family:var(--display); font-size:20px; font-weight:700; }
.kpi-change { font-size:11px; font-weight:600; margin-top:2px; }
.kpi-up{color:var(--positive)}.kpi-down{color:var(--danger)}
.mini-bar-wrap { margin-top:8px; }
.mini-bar-label { display:flex; justify-content:space-between; font-size:11px; color:var(--muted); margin-bottom:4px; }
.mini-bar-track { height:6px; background:var(--surface2); border-radius:3px; overflow:hidden; }
.mini-bar-fill { height:100%; border-radius:3px; background:linear-gradient(90deg,var(--accent),var(--accent2)); }

/* ─── SECURITY ──────────────────────────── */
.security { padding:72px 48px; max-width:1160px; margin:0 auto; display:grid; grid-template-columns:1fr 1fr; gap:60px; align-items:center; }
.security-badges { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.security-badge { background:var(--surface); border:1px solid var(--border); border-radius:var(--r); padding:18px; display:flex; flex-direction:column; gap:7px; }
.security-badge-icon { color:var(--accent); }
.security-badge-icon svg { width:19px; height:19px; }
.security-badge-title { font-size:13px; font-weight:600; }
.security-badge-body { font-size:12px; color:var(--sub); line-height:1.5; }

/* ─── FOOTER ────────────────────────────── */
footer {
  position:relative; z-index:1;
  border-top:1px solid var(--border);
  padding:26px 48px;
  display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:16px;
}
.footer-logo {
  height:30px; width:auto; object-fit:contain;
  mix-blend-mode:screen; opacity:.7; transition:opacity .2s, filter .2s;
}
html[data-theme="light"] .footer-logo { mix-blend-mode:normal; filter:invert(1) hue-rotate(180deg); opacity:.65; }
.footer-logo:hover { opacity:1; }
.footer-copy { font-size:13px; color:var(--muted); }

/* ══════════════════════════════════════════
   LOGIN MODAL
   ══════════════════════════════════════════ */
.modal-overlay {
  display: none;
  position: fixed; inset: 0; z-index: 999;
  background: rgba(4, 10, 22, 0.72);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  align-items: center;
  justify-content: center;
  padding: 20px;
  animation: none;
}
.modal-overlay.open {
  display: flex;
  animation: overlayIn .2s ease both;
}
@keyframes overlayIn { from{opacity:0} to{opacity:1} }

html[data-theme="light"] .modal-overlay { background: rgba(180,195,220,0.55); }

.modal-card {
  width: 100%;
  max-width: 420px;
  background: var(--surface);
  border: 1px solid var(--border2);
  border-radius: var(--r-lg);
  padding: 40px;
  position: relative;
  overflow: hidden;
  box-shadow: 0 32px 80px rgba(0,0,0,.45);
  animation: cardIn .25s cubic-bezier(.34,1.3,.64,1) both;
}
html[data-theme="light"] .modal-card { box-shadow:0 16px 56px rgba(0,0,0,.12); }
@keyframes cardIn { from{opacity:0;transform:translateY(24px) scale(.97)} to{opacity:1;transform:none} }

/* glow blob inside card */
.modal-card::before {
  content:''; position:absolute; top:-80px; right:-80px;
  width:220px; height:220px; border-radius:50%;
  background:var(--accent-dim); filter:blur(70px); pointer-events:none;
}

/* close button */
.modal-close {
  position: absolute; top:16px; right:16px;
  width:32px; height:32px; border-radius:8px;
  border:1px solid var(--border2); background:var(--surface2);
  color:var(--muted); display:flex; align-items:center; justify-content:center;
  transition: color .2s, background .2s;
  z-index: 2;
}
.modal-close:hover { color:var(--text); background:var(--border2); }
.modal-close svg { width:14px; height:14px; }

/* logo inside modal */
.modal-logo {
  width: 100%; max-width:260px;
  height: auto; display:block; margin:0 auto 18px;
  object-fit:contain;
  mix-blend-mode:screen;
  position:relative; z-index:1;
}
html[data-theme="light"] .modal-logo { mix-blend-mode:normal; filter:invert(1) hue-rotate(180deg); }

.modal-header { text-align:center; margin-bottom:28px; position:relative; z-index:1; }
.modal-subtitle { font-size:13px; color:var(--sub); }

/* fields */
.lfield { margin-bottom:16px; position:relative; z-index:1; }
.lfield-label { display:block; font-size:11px; font-weight:700; letter-spacing:.9px; text-transform:uppercase; color:var(--sub); margin-bottom:7px; }
.lfield-wrap { position:relative; display:flex; align-items:center; }
.lfield-icon { position:absolute; left:13px; color:var(--muted); pointer-events:none; display:flex; }
.lfield-icon svg { width:15px; height:15px; }
.lfield-input {
  width:100%; padding:12px 13px 12px 39px;
  background:rgba(255,255,255,.04); border:1px solid var(--border2);
  border-radius:10px; color:var(--text); font-family:var(--sans); font-size:14px;
  outline:none; transition:border-color .2s, box-shadow .2s, background .2s;
  -webkit-appearance:none;
}
html[data-theme="light"] .lfield-input { background:rgba(0,0,0,.025); }
.lfield-input::placeholder { color:var(--muted); }
.lfield-input:focus { border-color:var(--accent); background:var(--accent-dim); box-shadow:0 0 0 3px var(--accent-glow); }
.lfield-eye { position:absolute; right:12px; background:none; border:none; color:var(--muted); display:flex; padding:4px; transition:color .2s; }
.lfield-eye:hover { color:var(--text); }
.lfield-eye svg { width:14px; height:14px; }

/* notices */
.l-notice { display:none; padding:10px 13px; border-radius:9px; font-size:13px; line-height:1.5; margin-bottom:14px; position:relative; z-index:1; }
.l-notice.show { display:block; }
.l-notice-info { background:rgba(0,212,255,.07); border:1px solid rgba(0,212,255,.18); color:var(--sub); }
html[data-theme="light"] .l-notice-info { background:rgba(0,109,217,.07); border-color:rgba(0,109,217,.18); }
.l-notice-error { background:rgba(255,83,112,.07); border:1px solid rgba(255,83,112,.2); color:var(--danger); }
html[data-theme="light"] .l-notice-error { background:rgba(217,48,37,.06); border-color:rgba(217,48,37,.15); color:#c0392b; }

/* submit */
.btn-submit {
  width:100%; height:48px; border-radius:10px; border:none;
  background:var(--accent); color:#04111E;
  font-family:var(--display); font-size:15px; font-weight:700; letter-spacing:.2px;
  transition:opacity .2s, box-shadow .2s, transform .15s;
  position:relative; z-index:1; margin-top:4px;
  display:flex; align-items:center; justify-content:center; gap:8px;
}
html[data-theme="light"] .btn-submit { color:#fff; }
.btn-submit:hover:not(:disabled) { opacity:.9; box-shadow:0 6px 24px var(--accent-glow); transform:translateY(-1px); }
.btn-submit:disabled { opacity:.55; cursor:not-allowed; }
.btn-spinner {
  display:none; width:16px; height:16px;
  border:2px solid rgba(4,17,30,.25); border-top-color:#04111E;
  border-radius:50%; animation:spin .7s linear infinite;
}
html[data-theme="light"] .btn-spinner { border-color:rgba(255,255,255,.3); border-top-color:#fff; }
.btn-submit.loading .btn-spinner { display:block; }
.btn-submit.loading .btn-label { opacity:.7; }

.modal-footer-note { text-align:center; font-size:12px; color:var(--muted); margin-top:20px; line-height:1.6; position:relative; z-index:1; }

/* ─── ANIMATIONS ─────────────────────────── */
@keyframes fadeUp { from{opacity:0;transform:translateY(18px)} to{opacity:1;transform:translateY(0)} }
@keyframes spin { to{transform:rotate(360deg)} }
.anim { opacity:0; transform:translateY(20px); transition:opacity .6s ease, transform .6s ease; }
.anim.visible { opacity:1; transform:translateY(0); }

/* ─── RESPONSIVE ─────────────────────────── */
@media(max-width:900px){
  nav{padding:0 20px;}
  .modules,.features,.security{padding:60px 20px;}
  .features-inner,.security{grid-template-columns:1fr;gap:36px;}
  footer{padding:22px 20px;}
}
@media(max-width:600px){
  .hero{padding:60px 20px 52px;}
  .hero-stats{gap:24px;}
  .kpi-row{grid-template-columns:1fr;}
  .security-badges{grid-template-columns:1fr;}
  .modal-card{padding:30px 24px;}
}
</style>
</head>
<body>

<!-- BG -->
<div class="bg-scene" aria-hidden="true">
  <div class="bg-mesh"></div>
  <div class="bg-grid"></div>
  <div class="bg-orb bg-orb-1"></div>
  <div class="bg-orb bg-orb-2"></div>
</div>

<!-- ══ NAV ══ -->
<nav>
  <img src="/static/ERP_logo.png" alt="AZed ERP" class="nav-logo">
  <div class="nav-actions">
    <button class="nav-theme-btn" onclick="toggleTheme()" aria-label="Toggle theme">
      <svg id="iconSun" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display:none"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
      <svg id="iconMoon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    </button>
    <button class="btn-signin-nav" onclick="openModal()">Sign In</button>
  </div>
</nav>

<!-- ══ HERO ══ -->
<section class="hero">
  <img src="/static/ERP_logo.png" alt="AZed ERP" class="hero-logo">
  <div class="hero-badge"><div class="hero-badge-dot"></div>Enterprise Resource Planning</div>
  <h1 class="hero-title">Every part of your business,<br><em>unified.</em></h1>
  <p class="hero-sub">AZed ERP brings inventory, sales, HR, accounting, and operations into one cohesive platform — built for teams that demand reliability and speed.</p>
  <div class="hero-cta-group">
    <button class="btn-primary" onclick="openModal()">Sign In to Your Account</button>
    <button class="btn-outline" onclick="document.getElementById('modules').scrollIntoView({behavior:'smooth'})">Explore Modules</button>
  </div>
  <div class="hero-stats">
    <div class="hero-stat"><div class="hero-stat-num">15<span>+</span></div><div class="hero-stat-label">Modules</div></div>
    <div class="hero-stat"><div class="hero-stat-num">Real<span>-time</span></div><div class="hero-stat-label">Analytics</div></div>
    <div class="hero-stat"><div class="hero-stat-num">RBAC</div><div class="hero-stat-label">Access Control</div></div>
    <div class="hero-stat"><div class="hero-stat-num">24<span>/7</span></div><div class="hero-stat-label">Availability</div></div>
  </div>
</section>

<hr class="section-divider">

<!-- ══ MODULES ══ -->
<section id="modules" class="modules">
  <div class="modules-header anim">
    <div class="section-tag">Platform Modules</div>
    <h2 class="section-title">Everything you need, out of the box</h2>
    <p class="section-sub">Every module is tightly integrated — data flows freely across departments with no manual syncing.</p>
  </div>
  <div class="modules-grid">
    <div class="module-card anim">
      <div class="module-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg></div>
      <div class="module-name">Dashboard</div>
      <div class="module-desc">Real-time KPIs, sales trends, inventory alerts, and financial summaries in a single command center.</div>
      <div class="module-tags"><span class="module-tag">Analytics</span><span class="module-tag">KPIs</span><span class="module-tag">Charts</span></div>
    </div>
    <div class="module-card anim">
      <div class="module-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/><path d="M16 10a4 4 0 0 1-8 0"/></svg></div>
      <div class="module-name">Inventory</div>
      <div class="module-desc">Track stock levels, manage locations, set reorder thresholds, and receive product alerts automatically.</div>
      <div class="module-tags"><span class="module-tag">Stock</span><span class="module-tag">Locations</span><span class="module-tag">Reorder</span></div>
    </div>
    <div class="module-card anim">
      <div class="module-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg></div>
      <div class="module-name">Point of Sale</div>
      <div class="module-desc">Fast, intuitive POS interface with barcode support, receipt printing, and offline resilience.</div>
      <div class="module-tags"><span class="module-tag">POS</span><span class="module-tag">Barcode</span><span class="module-tag">Receipts</span></div>
    </div>
    <div class="module-card anim">
      <div class="module-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg></div>
      <div class="module-name">Customers & B2B</div>
      <div class="module-desc">CRM, B2B invoicing, client statements, credit management, and purchase history at a glance.</div>
      <div class="module-tags"><span class="module-tag">CRM</span><span class="module-tag">Invoicing</span><span class="module-tag">B2B</span></div>
    </div>
    <div class="module-card anim">
      <div class="module-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg></div>
      <div class="module-name">HR & Payroll</div>
      <div class="module-desc">Employee records, attendance tracking, leave management, payroll processing, and loan deductions.</div>
      <div class="module-tags"><span class="module-tag">Attendance</span><span class="module-tag">Payroll</span><span class="module-tag">Loans</span></div>
    </div>
    <div class="module-card anim">
      <div class="module-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg></div>
      <div class="module-name">Accounting</div>
      <div class="module-desc">Double-entry bookkeeping, chart of accounts, journal entries, P&L, and balance sheet reporting.</div>
      <div class="module-tags"><span class="module-tag">Ledger</span><span class="module-tag">P&L</span><span class="module-tag">Reports</span></div>
    </div>
    <div class="module-card anim">
      <div class="module-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg></div>
      <div class="module-name">Farm & Production</div>
      <div class="module-desc">Manage farm plots, crop cycles, production batches, employee assignments, and yield tracking.</div>
      <div class="module-tags"><span class="module-tag">Crops</span><span class="module-tag">Batches</span><span class="module-tag">Yield</span></div>
    </div>
    <div class="module-card anim">
      <div class="module-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg></div>
      <div class="module-name">Reports & Audit</div>
      <div class="module-desc">Comprehensive reports, exportable data, and a full audit log of every action across the system.</div>
      <div class="module-tags"><span class="module-tag">Export</span><span class="module-tag">Audit Log</span><span class="module-tag">History</span></div>
    </div>
  </div>
</section>

<hr class="section-divider">

<!-- ══ FEATURES ══ -->
<section class="features">
  <div class="features-inner">
    <div>
      <div class="anim">
        <div class="section-tag">Why AZed ERP</div>
        <h2 class="section-title">Built for real operations, not demos</h2>
        <p class="section-sub">Every feature is battle-tested against the complexity of real business workflows.</p>
      </div>
      <div class="features-list">
        <div class="feature-item anim">
          <div class="feature-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></div>
          <div><div class="feature-text-title">Role-Based Access Control</div><div class="feature-text-body">Fine-grained permissions per user and role. Every page, every action is guarded.</div></div>
        </div>
        <div class="feature-item anim">
          <div class="feature-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div>
          <div><div class="feature-text-title">Live Data, Always</div><div class="feature-text-body">Stock levels, sales, and financials update instantly across all modules — no stale caches.</div></div>
        </div>
        <div class="feature-item anim">
          <div class="feature-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg></div>
          <div><div class="feature-text-title">Full Audit Trail</div><div class="feature-text-body">Every create, update, and delete is logged with timestamp and user — full traceability.</div></div>
        </div>
        <div class="feature-item anim">
          <div class="feature-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg></div>
          <div><div class="feature-text-title">Mobile-Ready Interface</div><div class="feature-text-body">Responsive design across all modules — manage from any device, anywhere.</div></div>
        </div>
      </div>
    </div>
    <div class="features-visual anim">
      <div class="visual-header">
        <div class="visual-dot visual-dot-r"></div><div class="visual-dot visual-dot-y"></div><div class="visual-dot visual-dot-g"></div>
        <span class="visual-title">dashboard · live</span>
      </div>
      <div class="kpi-row">
        <div class="kpi-card"><div class="kpi-label">Revenue MTD</div><div class="kpi-value">248k</div><div class="kpi-change kpi-up">↑ 12.4%</div></div>
        <div class="kpi-card"><div class="kpi-label">Orders Today</div><div class="kpi-value">134</div><div class="kpi-change kpi-up">↑ 8 new</div></div>
        <div class="kpi-card"><div class="kpi-label">Stock Alerts</div><div class="kpi-value">3</div><div class="kpi-change kpi-down">↓ Low stock</div></div>
        <div class="kpi-card"><div class="kpi-label">Pending HR</div><div class="kpi-value">7</div><div class="kpi-change" style="color:var(--warning)">⚠ Requests</div></div>
      </div>
      <div class="mini-bar-wrap"><div class="mini-bar-label"><span>Inventory fill rate</span><span>84%</span></div><div class="mini-bar-track"><div class="mini-bar-fill" style="width:84%"></div></div></div>
      <div class="mini-bar-wrap" style="margin-top:10px"><div class="mini-bar-label"><span>Payroll processed</span><span>61%</span></div><div class="mini-bar-track"><div class="mini-bar-fill" style="width:61%;background:linear-gradient(90deg,var(--positive),#38d9a9)"></div></div></div>
      <div class="mini-bar-wrap" style="margin-top:10px"><div class="mini-bar-label"><span>B2B collection rate</span><span>92%</span></div><div class="mini-bar-track"><div class="mini-bar-fill" style="width:92%;background:linear-gradient(90deg,var(--accent2),var(--accent))"></div></div></div>
    </div>
  </div>
</section>

<hr class="section-divider">

<!-- ══ SECURITY ══ -->
<section class="security">
  <div class="anim">
    <div class="section-tag">Security</div>
    <h2 class="section-title">Enterprise-grade security by default</h2>
    <p class="section-sub">Your data is protected at every layer — from session management to network egress.</p>
  </div>
  <div class="security-badges">
    <div class="security-badge anim"><div class="security-badge-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="19" height="19"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></div><div class="security-badge-title">httpOnly Cookies</div><div class="security-badge-body">Tokens stored in httpOnly cookies — never exposed to JavaScript.</div></div>
    <div class="security-badge anim"><div class="security-badge-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="19" height="19"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></div><div class="security-badge-title">CSRF Protection</div><div class="security-badge-body">All state-changing requests protected with CSRF middleware.</div></div>
    <div class="security-badge anim"><div class="security-badge-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="19" height="19"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg></div><div class="security-badge-title">Rate Limiting</div><div class="security-badge-body">Login brute-force protection via Redis-backed per-IP limits.</div></div>
    <div class="security-badge anim"><div class="security-badge-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="19" height="19"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg></div><div class="security-badge-title">Auto Token Refresh</div><div class="security-badge-body">Silent token rotation keeps sessions alive without re-login.</div></div>
  </div>
</section>

<!-- ══ FOOTER ══ -->
<footer>
  <img src="/static/ERP_logo.png" alt="AZed ERP" class="footer-logo">
  <div class="footer-copy">&copy; 2026 AZed ERP. All rights reserved.</div>
</footer>

<!-- ══ LOGIN MODAL ══ -->
<div class="modal-overlay" id="loginModal" onclick="onOverlayClick(event)">
  <div class="modal-card" id="modalCard">
    <button class="modal-close" onclick="closeModal()" aria-label="Close">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>

    <div class="modal-header">
      <img src="/static/ERP_logo.png" alt="AZed ERP" class="modal-logo">
      <div class="modal-subtitle">Enter your credentials to access the system</div>
    </div>

    <div id="sessionNotice" class="l-notice l-notice-info">Your session expired — please sign in again.</div>
    <div id="errorMsg" class="l-notice l-notice-error" role="alert"></div>

    <div class="lfield">
      <label class="lfield-label" for="email">Email address</label>
      <div class="lfield-wrap">
        <span class="lfield-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg></span>
        <input class="lfield-input" type="email" id="email" placeholder="you@company.com" autocomplete="email" autocapitalize="none">
      </div>
    </div>

    <div class="lfield">
      <label class="lfield-label" for="password">Password</label>
      <div class="lfield-wrap">
        <span class="lfield-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></span>
        <input class="lfield-input" type="password" id="password" placeholder="••••••••" autocomplete="current-password">
        <button class="lfield-eye" type="button" onclick="togglePwd()" aria-label="Toggle password">
          <svg id="eyeOpen" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
          <svg id="eyeClosed" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display:none"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
        </button>
      </div>
    </div>

    <button class="btn-submit" id="signInBtn" onclick="login()">
      <span class="btn-label">Sign In</span>
      <div class="btn-spinner"></div>
    </button>

    <div class="modal-footer-note">Secured with httpOnly cookies &amp; CSRF protection.<br>Contact your administrator if you need access.</div>
  </div>
</div>

<script>
/* ── Theme ── */
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  document.getElementById('iconSun').style.display  = t==='light' ? 'block' : 'none';
  document.getElementById('iconMoon').style.display = t==='dark'  ? 'block' : 'none';
}
function toggleTheme() {
  var cur = document.documentElement.getAttribute('data-theme') || 'dark';
  var next = cur==='dark' ? 'light' : 'dark';
  try{ localStorage.setItem('colorMode', next); }catch(_){}
  applyTheme(next);
}
(function(){
  var s; try{ s=localStorage.getItem('colorMode'); }catch(_){}
  applyTheme(s==='light' ? 'light' : 'dark');
})();

/* ── Modal ── */
function openModal() {
  var m = document.getElementById('loginModal');
  m.classList.add('open');
  document.body.style.overflow = 'hidden';
  setTimeout(function(){ document.getElementById('email').focus(); }, 250);
  // show session notice if ?reason=expired
  var p = new URLSearchParams(window.location.search);
  if(p.get('reason')==='expired') document.getElementById('sessionNotice').classList.add('show');
}
function closeModal() {
  document.getElementById('loginModal').classList.remove('open');
  document.body.style.overflow = '';
}
function onOverlayClick(e) {
  if(e.target === document.getElementById('loginModal')) closeModal();
}
document.addEventListener('keydown', function(e){
  if(e.key==='Escape') closeModal();
  if(e.key==='Enter' && document.getElementById('loginModal').classList.contains('open')) login();
});

/* auto-open on session expired */
(function(){
  var p = new URLSearchParams(window.location.search);
  if(p.get('reason')==='expired') openModal();
})();

/* ── Password toggle ── */
function togglePwd() {
  var i=document.getElementById('password');
  var o=document.getElementById('eyeOpen'), c=document.getElementById('eyeClosed');
  if(i.type==='password'){i.type='text';o.style.display='none';c.style.display='block';}
  else{i.type='password';o.style.display='block';c.style.display='none';}
}

/* ── Safe redirect ── */
function isSafeUrl(u) {
  var bs=String.fromCharCode(92);
  return typeof u==='string'&&u.startsWith('/')&&!u.startsWith('//')&&u.indexOf(bs)===-1&&u.indexOf('\r')===-1&&u.indexOf('\n')===-1;
}

/* ── Login ── */
async function login() {
  var errEl=document.getElementById('errorMsg');
  errEl.classList.remove('show');
  var email=document.getElementById('email').value.trim();
  var password=document.getElementById('password').value;
  var btn=document.getElementById('signInBtn');
  if(!email||!password){ errEl.textContent='Please enter both email and password.'; errEl.classList.add('show'); return; }
  btn.disabled=true; btn.classList.add('loading');
  try{
    var res=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
    var data=await res.json();
    if(!res.ok){ errEl.textContent=data.detail||'Invalid email or password.'; errEl.classList.add('show'); btn.disabled=false; btn.classList.remove('loading'); return; }
    var perms=new Set((data.permissions||'').split(',').map(v=>v.trim()).filter(Boolean));
    var pages=[['/dashboard','page_dashboard'],['/pos','page_pos'],['/farm/','page_farm'],['/production/','page_production'],['/inventory/','page_inventory'],['/products/','page_products'],['/customers-mgmt/','page_customers'],['/suppliers/','page_suppliers'],['/receive/','page_receive_products'],['/import','page_import'],['/reports/','page_reports'],['/b2b/','page_b2b'],['/hr/','page_hr'],['/accounting/','page_accounting'],['/expenses/','page_expenses']];
    var defaultPage=data.role==='admin'?'/dashboard':((pages.find(p=>perms.has(p[1]))||['/home'])[0]);
    var rawNext=new URLSearchParams(window.location.search).get('next');
    window.location.href=isSafeUrl(rawNext)?rawNext:defaultPage;
  }catch(e){ errEl.textContent='Connection error. Please try again.'; errEl.classList.add('show'); btn.disabled=false; btn.classList.remove('loading'); }
}

/* ── Scroll reveal ── */
(function(){
  var els=document.querySelectorAll('.anim');
  if(!window.IntersectionObserver){ els.forEach(el=>el.classList.add('visible')); return; }
  var io=new IntersectionObserver(function(entries){
    entries.forEach(en=>{ if(en.isIntersecting){en.target.classList.add('visible');io.unobserve(en.target);} });
  },{threshold:.1});
  els.forEach(el=>io.observe(el));
})();
</script>
</body>
</html>
"""


@router.post("/auth/login")
@limiter.limit(settings.LOGIN_RATE_LIMIT)
async def login(
    data: UserLogin,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_async_session),
):
    from app.core.log import record

    # Brute-force protection: track failed attempts per IP in Redis
    import logging
    _brute_logger = logging.getLogger("erp")
    _client_ip = get_trusted_client_ip(request)
    _fail_key = f"login_fail:{_client_ip}"
    try:
        _redis = _redis_client()
        _fails = await _redis.get(_fail_key)
        if _fails and int(_fails) >= 5:
            await _redis.aclose()
            raise HTTPException(
                status_code=429,
                detail="Too many failed attempts. Try again in 15 minutes.",
            )
        await _redis.aclose()
    except HTTPException:
        raise
    except Exception:
        _brute_logger.warning("Redis unavailable for brute-force check — allowing login attempt")

    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.password):
        # Log failed attempt (no user object, store email)
        record(db, "Auth", "login_failed",
               f"Failed login attempt for email: {data.email}")
        await db.commit()
        try:
            _redis = _redis_client()
            await _redis.incr(_fail_key)
            await _redis.expire(_fail_key, 900)  # 15 minutes TTL
            await _redis.aclose()
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    if password_needs_rehash(user.password):
        user.password = hash_password(data.password)
    permissions = serialize_permissions(
        get_effective_permissions(user.role, user.permissions)
    )
    token = create_access_token(
        {"sub": user.id, "role": user.role, "permissions": permissions}
    )
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="logged_in",
        value="true",
        httponly=False,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    # Issue refresh token
    raw_rt = secrets.token_urlsafe(48)
    rt_hash = hashlib.sha256(raw_rt.encode()).hexdigest()
    rt_expires = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    db.add(RefreshToken(user_id=user.id, token_hash=rt_hash, expires_at=rt_expires))
    response.set_cookie(
        key="refresh_token",
        value=raw_rt,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )

    # Reset brute-force counter on successful login
    try:
        _redis = _redis_client()
        await _redis.delete(_fail_key)
        await _redis.aclose()
    except Exception:
        pass
    record(db, "Auth", "login",
           f"User logged in: {user.name} ({user.role})",
           user=user, ref_type="user", ref_id=user.id)
    await db.commit()
    # access_token is in the httpOnly cookie — not returned in body to prevent XSS
    return {
        "role": user.role,
        "name": user.name,
        "permissions": permissions,
    }


@router.get("/auth/me")
async def me(current_user: User = Depends(get_current_user)):
    permissions = serialize_permissions(
        get_effective_permissions(current_user.role, current_user.permissions)
    )
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "role": current_user.role,
        "is_active": current_user.is_active,
        "permissions": permissions,
    }


@router.get("/auth/permissions/catalog")
async def permissions_catalog(current_user: User = Depends(get_current_user)):
    return {
        "catalog": get_permission_catalog(),
        "role": current_user.role,
        "permissions": sorted(get_effective_permissions(current_user.role, current_user.permissions)),
    }


@router.post("/auth/register", response_model=UserOut, status_code=201)
async def register(
    data: UserCreate,
    db: AsyncSession = Depends(get_async_session),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.email == data.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(
        name=data.name,
        email=data.email,
        password=hash_password(data.password),
        role=data.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/auth/logout")
async def logout(
    response: Response,
    db: AsyncSession = Depends(get_async_session),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
):
    """Clear the auth cookie and invalidate the refresh token."""
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    response.delete_cookie("logged_in", path="/")
    if refresh_token:
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        _r = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        rt = _r.scalar_one_or_none()
        if rt:
            await db.delete(rt)
            await db.commit()
    return {"ok": True}


@router.post("/auth/refresh")
@limiter.limit(settings.REFRESH_RATE_LIMIT)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_async_session),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
):
    """Issue a new access token if a valid refresh token cookie is present."""
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")

    refreshed = await try_refresh_access_token(db, refresh_token)
    if not refreshed:
        raise HTTPException(status_code=401, detail="Refresh token expired or invalid")
    new_token, new_raw_rt = refreshed

    response.set_cookie(
        key="access_token",
        value=new_token,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="logged_in",
        value="true",
        httponly=False,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=new_raw_rt,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )
    return {"ok": True}