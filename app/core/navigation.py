from __future__ import annotations

from html import escape

from app.core.permissions import get_effective_permissions
from app.models.user import User


NAV_GROUPS = [
    {
        "label": "Work",
        "items": [
            {"label": "Dashboard", "href": "/dashboard", "permission": "page_dashboard"},
            {"label": "POS", "href": "/pos", "permission": "page_pos"},
            {"label": "B2B", "href": "/b2b/", "permission": "page_b2b"},
            {"label": "Reports", "href": "/reports/", "permission": "page_reports"},
        ],
    },
    {
        "label": "Stock",
        "items": [
            {"label": "Products", "href": "/products/", "permission": "page_products"},
            {"label": "Inventory", "href": "/inventory/", "permission": "page_inventory"},
            {"label": "Receive", "href": "/receive/", "permission": "page_receive_products"},
            {"label": "Import", "href": "/import", "permission": "page_import"},
            {"label": "Farm", "href": "/farm/", "permission": "page_farm"},
            {"label": "Production", "href": "/production/", "permission": "page_production"},
            {"label": "Carbon", "href": "/carbon/", "permission": "page_carbon"},
        ],
    },
    {
        "label": "Finance",
        "items": [
            {"label": "Accounting", "href": "/accounting/", "permission": "page_accounting"},
            {"label": "Expenses", "href": "/expenses/", "permission": "page_expenses"},
            {"label": "Customers", "href": "/customers-mgmt/", "permission": "page_customers"},
            {"label": "Suppliers", "href": "/suppliers/", "permission": "page_suppliers"},
        ],
    },
    {
        "label": "People",
        "items": [
            {"label": "HR", "href": "/hr/", "permission": "page_hr"},
            {"label": "Users", "href": "/users/", "admin_only": True},
        ],
    },
]


def _user_permissions(user: User) -> set[str]:
    return get_effective_permissions(user.role, getattr(user, "permissions", None))


def _can_see_item(user: User, permissions: set[str], item: dict) -> bool:
    if item.get("admin_only"):
        return user.role == "admin"
    return "*" in permissions or item["permission"] in permissions


def _is_active(item: dict, active_permission: str | None) -> bool:
    if item.get("admin_only"):
        return active_permission == "admin_users"
    return item.get("permission") == active_permission


def _render_group(user: User, permissions: set[str], group: dict, active_permission: str | None) -> str:
    visible_items = [
        item for item in group["items"]
        if _can_see_item(user, permissions, item)
    ]
    if not visible_items:
        return ""

    is_group_active = any(_is_active(item, active_permission) for item in visible_items)
    links = []
    for item in visible_items:
        active = _is_active(item, active_permission)
        links.append(
            f'<a class="app-nav-menu-item{" active" if active else ""}" '
            f'href="{escape(item["href"])}" role="menuitem"'
            f'{" aria-current=\"page\"" if active else ""}>{escape(item["label"])}</a>'
        )
    return (
        f'<details class="app-nav-group{" active" if is_group_active else ""}">'
        f'<summary>{escape(group["label"])}</summary>'
        f'<div class="app-nav-menu" role="menu">{"".join(links)}</div>'
        f'</details>'
    )


def app_nav_styles() -> str:
    return """
<style>
.app-nav{
  grid-column:1/-1;
  position:sticky;
  top:0;
  z-index:300;
  display:flex;
  align-items:center;
  gap:12px;
  min-height:64px;
  padding:8px 24px;
  background:color-mix(in srgb,var(--bg,#0B1120) 92%,transparent);
  backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border,rgba(255,255,255,.08));
  color:var(--text,#f0f4ff);
}
html[data-theme="light"] .app-nav,
body[data-theme="light"] .app-nav,
body.light .app-nav{
  background:rgba(255,255,255,.94);
}
.app-nav-brand{
  display:flex;
  align-items:center;
  gap:9px;
  min-width:max-content;
  text-decoration:none;
  font-size:17px;
  font-weight:900;
  color:var(--text,#f0f4ff);
}
.app-nav-brand img{flex:0 0 auto}
.app-nav-brand span{
  background:linear-gradient(135deg,var(--accent,#00E5FF),var(--blue,#38bdf8));
  -webkit-background-clip:text;
  -webkit-text-fill-color:transparent;
  background-clip:text;
}
html[data-theme="light"] .app-nav-brand img,
body[data-theme="light"] .app-nav-brand img,
body.light .app-nav-brand img{filter:invert(1) hue-rotate(180deg);}
.app-nav-main{display:flex;align-items:center;gap:6px;flex:1;min-width:0;}
.app-nav-group{position:relative}
.app-nav-group summary{
  list-style:none;
  display:flex;
  align-items:center;
  gap:7px;
  padding:8px 12px;
  border-radius:8px;
  color:var(--text-sub,#cbd5e1);
  font-size:12px;
  font-weight:800;
  cursor:pointer;
  white-space:nowrap;
  transition:background .16s,color .16s;
}
.app-nav-group summary::-webkit-details-marker{display:none}
.app-nav-group summary:after{
  content:"";
  width:6px;
  height:6px;
  border-right:1.5px solid currentColor;
  border-bottom:1.5px solid currentColor;
  transform:rotate(45deg) translateY(-2px);
  opacity:.75;
}
.app-nav-group:hover summary,
.app-nav-group[open] summary{
  background:color-mix(in srgb,var(--surface-raised,#334155) 44%,transparent);
  color:var(--text,#f0f4ff)
}
html[data-theme="light"] .app-nav-group:hover summary,
html[data-theme="light"] .app-nav-group[open] summary,
body[data-theme="light"] .app-nav-group:hover summary,
body[data-theme="light"] .app-nav-group[open] summary,
body.light .app-nav-group:hover summary,
body.light .app-nav-group[open] summary{
  background:rgba(15,23,42,.06);
}
.app-nav-group.active summary{
  background:color-mix(in srgb,var(--blue,#4d9fff) 14%,transparent);
  color:var(--blue,#4d9fff);
  box-shadow:inset 0 -2px 0 var(--blue,#4d9fff);
}
.app-nav-menu{
  position:absolute;
  left:0;
  top:calc(100% + 8px);
  min-width:190px;
  padding:8px;
  background:var(--surface,#1E293B);
  border:1px solid var(--border-strong,rgba(255,255,255,.11));
  border-radius:12px;
  box-shadow:0 22px 50px rgba(0,0,0,.34);
}
.app-nav-menu-item{
  display:flex;
  align-items:center;
  padding:10px 12px;
  border-radius:8px;
  color:var(--text-sub,#cbd5e1);
  font-size:13px;
  font-weight:700;
  text-decoration:none;
  white-space:nowrap;
}
.app-nav-menu-item:hover,
.app-nav-menu-item:focus-visible{
  background:var(--surface-raised,#334155);
  color:var(--text,#f0f4ff);
  outline:none;
}
.app-nav-menu-item.active{
  background:color-mix(in srgb,var(--blue,#4d9fff) 14%,transparent);
  color:var(--blue,#4d9fff);
}
.app-nav-actions{display:flex;align-items:center;gap:10px;margin-left:auto}
.app-nav .topbar-right{margin-left:auto}
.app-nav-mobile-toggle{
  display:none;
  align-items:center;
  justify-content:center;
  width:38px;
  height:38px;
  border-radius:10px;
  border:1px solid var(--border,rgba(255,255,255,.08));
  background:var(--surface,#1E293B);
  color:var(--text-sub,#cbd5e1);
  font-size:18px;
  cursor:pointer;
}
.mode-btn{
  display:flex;
  align-items:center;
  justify-content:center;
  width:36px;
  height:36px;
  border-radius:10px;
  border:1px solid var(--border,rgba(255,255,255,.08));
  background:var(--surface,#1E293B);
  color:var(--text-sub,#cbd5e1);
  font-size:16px;
  cursor:pointer;
  transition:all .2s;
  font-family:inherit;
}
.mode-btn:hover{
  border-color:var(--border-strong,rgba(255,255,255,.14));
  color:var(--text,#f0f4ff);
  transform:scale(1.06);
}
.account-menu{position:relative;}
.user-pill{
  display:flex;
  align-items:center;
  gap:10px;
  background:var(--surface,#1E293B);
  border:1px solid var(--border,rgba(255,255,255,.08));
  border-radius:40px;
  padding:7px 16px 7px 10px;
  cursor:pointer;
  transition:all .2s;
  color:var(--text-sub,#cbd5e1);
}
.user-pill:hover,
.user-pill.open{
  border-color:var(--border-strong,rgba(255,255,255,.14));
  color:var(--text,#f0f4ff);
}
.user-avatar{
  width:28px;
  height:28px;
  background:linear-gradient(135deg,var(--accent,#00E5FF),var(--warning,#f59e0b));
  border-radius:50%;
  display:flex;
  align-items:center;
  justify-content:center;
  font-size:12px;
  font-weight:700;
  color:#0a0c08;
  flex-shrink:0;
}
.user-name{font-size:13px;font-weight:500;color:currentColor;}
.menu-caret{font-size:11px;color:var(--text-muted,#94A3B8);}
.account-dropdown{
  position:absolute;
  right:0;
  top:calc(100% + 10px);
  min-width:220px;
  background:var(--surface,#1E293B);
  border:1px solid var(--border-strong,rgba(255,255,255,.14));
  border-radius:14px;
  padding:8px;
  box-shadow:0 24px 50px rgba(0,0,0,.35);
  display:none;
  z-index:600;
}
.account-dropdown.open{display:block;}
.account-head{
  padding:10px 12px 8px;
  border-bottom:1px solid var(--border,rgba(255,255,255,.08));
  margin-bottom:6px;
}
.account-label{
  font-size:11px;
  color:var(--text-muted,#94A3B8);
  text-transform:uppercase;
  letter-spacing:1px;
}
.account-email{
  font-size:12px;
  color:var(--text-sub,#cbd5e1);
  margin-top:4px;
  word-break:break-word;
}
.account-item{
  width:100%;
  display:flex;
  align-items:center;
  gap:10px;
  padding:10px 12px;
  border:none;
  background:transparent;
  border-radius:10px;
  color:var(--text-sub,#cbd5e1);
  font-family:inherit;
  font-size:13px;
  text-decoration:none;
  cursor:pointer;
  text-align:left;
}
.account-item:hover{
  background:var(--surface-raised,#334155);
  color:var(--text,#f0f4ff);
}
.account-item.danger:hover{color:var(--negative,#f87171);}
.app-nav .mode-btn{flex:0 0 auto}
.app-nav .account-menu{position:relative}
.app-nav .account-dropdown{z-index:600}
@media(max-width:900px){
  .app-nav{flex-wrap:wrap;padding:8px 14px}
  .app-nav-mobile-toggle{display:flex}
  .app-nav-main{display:none;order:3;flex-basis:100%;flex-direction:column;align-items:stretch;gap:6px;padding-top:8px}
  .app-nav.open .app-nav-main{display:flex}
  .app-nav-group{width:100%}
  .app-nav-group summary{justify-content:space-between;padding:12px 13px;background:color-mix(in srgb,var(--surface-raised,#334155) 34%,transparent)}
  .app-nav-menu{position:static;box-shadow:none;margin-top:6px;width:100%}
  .app-nav-actions{margin-left:auto}
  .app-nav .user-name{display:none}
}
@media(max-width:520px){
  .app-nav{gap:8px}
  .app-nav-brand span{max-width:116px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .app-nav-actions{gap:6px}
  .app-nav .user-pill{padding:6px 9px}
  .app-nav .menu-caret{display:none}
}
</style>
"""


def app_nav_script() -> str:
    return """
<script>
(function(){
  var THEME_KEY = "colorMode";
  var LEGACY_KEYS = ["dashboard:theme", "expenses-theme", "refund-theme"];
  var TOGGLE_SELECTOR = "[data-theme-toggle], #mode-btn, #themeToggle, .app-theme-toggle";

  function normalizeTheme(theme){
    return theme === "light" ? "light" : "dark";
  }

  function readStoredTheme(){
    try{
      var stored = localStorage.getItem(THEME_KEY);
      if(stored) return normalizeTheme(stored);
      for(var i = 0; i < LEGACY_KEYS.length; i += 1){
        var legacy = localStorage.getItem(LEGACY_KEYS[i]);
        if(legacy) return normalizeTheme(legacy);
      }
    }catch(_){}
    var htmlTheme = document.documentElement.getAttribute("data-theme");
    if(htmlTheme) return normalizeTheme(htmlTheme);
    return document.body && document.body.classList.contains("light") ? "light" : "dark";
  }

  function persistTheme(theme){
    try{
      localStorage.setItem(THEME_KEY, theme);
      for(var i = 0; i < LEGACY_KEYS.length; i += 1){
        localStorage.removeItem(LEGACY_KEYS[i]);
      }
    }catch(_){}
  }

  function syncThemeButton(theme){
    var icon = theme === "light" ? "&#9728;&#65039;" : "&#127769;";
    var label = theme === "light" ? "Switch to dark theme" : "Switch to light theme";
    document.querySelectorAll(TOGGLE_SELECTOR).forEach(function(button){
      if(!button.querySelector("svg")) button.innerHTML = icon;
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
      button.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
    });
  }

  function applyTheme(theme, options){
    var settings = Object.assign({persist:true, dispatch:true}, options || {});
    var next = normalizeTheme(theme);

    document.documentElement.dataset.theme = next;
    document.documentElement.setAttribute("data-theme", next);
    document.documentElement.style.colorScheme = next;
    document.documentElement.classList.toggle("light", next === "light");

    if(document.body){
      document.body.dataset.theme = next;
      document.body.setAttribute("data-theme", next);
      document.body.classList.toggle("light", next === "light");
    }

    syncThemeButton(next);

    if(settings.persist) persistTheme(next);
    if(settings.dispatch){
      window.dispatchEvent(new CustomEvent("app:themechange", {detail:{theme:next}}));
    }
    return next;
  }

  function ensureTheme(options){
    return applyTheme(readStoredTheme(), Object.assign({persist:false, dispatch:false}, options || {}));
  }

  function closeOtherGroups(current){
    document.querySelectorAll(".app-nav-group[open]").forEach(function(group){
      if(group !== current) group.removeAttribute("open");
    });
  }

  if(!window.__appTheme){
    window.__appTheme = {
      get: function(){
        return normalizeTheme(document.documentElement.dataset.theme || readStoredTheme());
      },
      set: function(theme){
        return applyTheme(theme);
      },
      toggle: function(){
        return applyTheme(this.get() === "light" ? "dark" : "light");
      },
      sync: function(){
        return ensureTheme();
      },
      key: THEME_KEY
    };
  }

  window.__appNav = {
    toggleAccount: function(event){
      if(event) event.stopPropagation();
      var trigger = document.getElementById("account-trigger");
      var dropdown = document.getElementById("account-dropdown");
      if(!trigger || !dropdown) return;
      var open = dropdown.classList.toggle("open");
      trigger.classList.toggle("open", open);
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
    },
    signOut: async function(){
      await fetch("/auth/logout", {method:"POST"});
      window.location.href = "/";
    },
    toggleTheme: function(){
      if(window.__appTheme && typeof window.__appTheme.toggle === "function"){
        return window.__appTheme.toggle();
      }
      return applyTheme(readStoredTheme() === "light" ? "dark" : "light");
    }
  };

  ensureTheme();

  document.addEventListener("DOMContentLoaded", function(){
    ensureTheme();
  });

  window.addEventListener("storage", function(event){
    if([THEME_KEY].concat(LEGACY_KEYS).indexOf(event.key || "") === -1) return;
    applyTheme(readStoredTheme(), {persist:false, dispatch:true});
  });

  window.addEventListener("app:themechange", function(event){
    var next = event && event.detail ? event.detail.theme : readStoredTheme();
    syncThemeButton(normalizeTheme(next));
  });

  document.addEventListener("click", function(event){
    var account = document.getElementById("account-dropdown");
    var trigger = document.getElementById("account-trigger");
    if(account && trigger && !account.contains(event.target) && !trigger.contains(event.target)){
      account.classList.remove("open");
      trigger.classList.remove("open");
      trigger.setAttribute("aria-expanded", "false");
    }
    if(!event.target.closest(".app-nav-group")) closeOtherGroups(null);
  });

  document.addEventListener("toggle", function(event){
    if(event.target.classList && event.target.classList.contains("app-nav-group") && event.target.open){
      closeOtherGroups(event.target);
    }
  }, true);
})();
</script>
"""


def render_app_header(user: User, active_permission: str | None = None) -> str:
    permissions = _user_permissions(user)
    groups = "".join(
        _render_group(user, permissions, group, active_permission)
        for group in NAV_GROUPS
    )
    name = escape(getattr(user, "name", None) or "User")
    email = escape(getattr(user, "email", None) or "")
    avatar = escape((name.strip()[:1] or "U").upper())
    return f"""
{app_nav_styles()}
<nav class="app-nav" id="app-nav" aria-label="Primary navigation">
  <a href="/home" class="app-nav-brand navbar-brand">
    <img src="/static/ERP_logo.png" alt="AZed ERP" style="height: 100%; max-height: 48px; width: auto; object-fit: contain; margin: 0; padding: 0;">
  </a>
  <button class="app-nav-mobile-toggle" type="button" aria-label="Toggle navigation" onclick="document.getElementById('app-nav').classList.toggle('open')">&#9776;</button>
  <div class="app-nav-main">{groups}</div>
  <div class="app-nav-actions topbar-right">
    <button class="mode-btn app-theme-toggle" id="mode-btn" type="button" data-theme-toggle title="Switch color theme" aria-label="Switch color theme" aria-pressed="false" onclick="window.__appNav.toggleTheme()">&#127769;</button>
    <div class="account-menu">
      <button class="user-pill" id="account-trigger" type="button" onclick="window.__appNav.toggleAccount(event)" aria-haspopup="menu" aria-expanded="false">
        <div class="user-avatar" id="user-avatar">{avatar}</div>
        <span class="user-name" id="user-name">{name}</span>
        <span class="menu-caret">&#9662;</span>
      </button>
      <div class="account-dropdown" id="account-dropdown" role="menu">
        <div class="account-head">
          <div class="account-label">Signed in as</div>
          <div class="account-email" id="user-email">{email}</div>
        </div>
        <a href="/users/password" class="account-item" role="menuitem">Change Password</a>
        <button class="account-item danger" id="signout-btn" type="button" onclick="window.__appNav.signOut()" role="menuitem">Sign out</button>
      </div>
    </div>
  </div>
</nav>
{app_nav_script()}
"""
