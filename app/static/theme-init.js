/**
 * theme-init.js
 *
 * Pre-paint theme shim. Must be loaded as the very first <script> in <head>,
 * before any stylesheet, on every authenticated page. Reads localStorage
 * synchronously and sets the data-theme attribute and the "light" class on
 * <html> before the browser paints, eliminating theme-flash (FOUC).
 *
 * The richer theme.js / auth-guard.js logic continues to handle toggle and
 * cross-tab sync after parse — this file just gets the colors right at t=0.
 */
(function () {
  var THEME_KEY = "colorMode";
  var LEGACY_KEYS = ["dashboard:theme", "expenses-theme", "refund-theme"];

  // ──────────────────────────────────────────────────────────────────────
  // EARLY THEME + SPLASH (runs synchronously in <head>, before <body>)
  // ──────────────────────────────────────────────────────────────────────
  // Decide theme immediately.
  var _earlyTheme = "dark";
  try {
    var _stored = localStorage.getItem(THEME_KEY);
    if (!_stored) {
      for (var _i = 0; _i < LEGACY_KEYS.length; _i += 1) {
        var _legacy = localStorage.getItem(LEGACY_KEYS[_i]);
        if (_legacy) { _stored = _legacy; break; }
      }
    }
    if (_stored === "light") _earlyTheme = "light";
  } catch (_e) {}

  // Paint <html> with the theme color so there's never a white flash.
  var _earlyBg = _earlyTheme === "light" ? "#f4f5ef" : "#060810";
  var _earlyFg = _earlyTheme === "light" ? "#1a1e14" : "#f0f4ff";
  try {
    document.documentElement.style.background = _earlyBg;
    document.documentElement.style.color = _earlyFg;
    document.documentElement.style.colorScheme = _earlyTheme;
    document.documentElement.setAttribute("data-theme", _earlyTheme);
    if (_earlyTheme === "light") document.documentElement.classList.add("light");
  } catch (_e) {}

  // Inject splash CSS into <head> immediately.
  try {
    var _splashStyle = document.createElement("style");
    _splashStyle.id = "app-splash-style";
    _splashStyle.textContent =
      // Hide everything in <body> except the splash, until .app-ready.
      "html:not(.app-ready) body > *:not(#app-splash){visibility:hidden!important;}" +
      // Body opacity transition (in case page content uses fade later).
      "html:not(.app-ready) body{opacity:1;}" +
      // Splash overlay.
      "#app-splash{position:fixed;inset:0;z-index:2147483600;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:28px;" +
      "background:" + _earlyBg + ";opacity:1;transition:opacity 380ms ease-out;}" +
      "#app-splash.app-splash-hide{opacity:0;pointer-events:none;}" +
      "#app-splash .splash-logo{min-width:220px;height:96px;padding:0 28px;border-radius:22px;" +
      "background:" + (_earlyTheme === "light" ? "rgba(255,255,255,0.85)" : "rgba(255,255,255,0.04)") + ";" +
      "border:1px solid " + (_earlyTheme === "light" ? "rgba(0,0,0,0.06)" : "rgba(255,255,255,0.08)") + ";" +
      "display:flex;align-items:center;justify-content:center;" +
      "box-shadow:0 20px 50px rgba(0,0,0,0.35);" +
      "animation:splashLogoIn 520ms cubic-bezier(.22,.9,.3,1.2) both;}" +
      "#app-splash .splash-logo img{height:56px;width:auto;object-fit:contain;display:block;}" +
      "#app-splash .splash-name{font-family:'Outfit',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;" +
      "font-size:13px;letter-spacing:4px;font-weight:600;text-transform:uppercase;" +
      "color:" + (_earlyTheme === "light" ? "rgba(26,30,20,0.55)" : "rgba(240,244,255,0.55)") + ";" +
      "animation:splashFadeUp 600ms ease-out 120ms both;}" +
      "#app-splash .splash-bar{width:140px;height:2px;border-radius:2px;" +
      "background:linear-gradient(90deg,transparent,#00E5FF,#d97706,transparent);background-size:200% 100%;" +
      "animation:splashBar 1400ms ease-in-out infinite,splashFadeUp 600ms ease-out 220ms both;opacity:.85;}" +
      "@keyframes splashLogoIn{0%{opacity:0;transform:scale(0.86) translateY(6px);}100%{opacity:1;transform:scale(1) translateY(0);}}" +
      "@keyframes splashFadeUp{0%{opacity:0;transform:translateY(6px);}100%{opacity:1;transform:translateY(0);}}" +
      "@keyframes splashBar{0%{background-position:100% 50%;}100%{background-position:-100% 50%;}}" +
      "@media print{#app-splash{display:none!important;}html:not(.app-ready) body > *:not(#app-splash){visibility:visible!important;}}";
    (document.head || document.documentElement).appendChild(_splashStyle);
  } catch (_e) {}

  // Mount splash element. Body might not exist yet, so poll with rAF.
  function _mountSplash() {
    if (document.getElementById("app-splash")) return;
    var s = document.createElement("div");
    s.id = "app-splash";
    s.setAttribute("aria-hidden", "true");
    s.innerHTML =
      '<div class="splash-logo"><img src="/static/ERP_logo.png" alt="AZed ERP" /></div>' +
      '<div class="splash-name">Enterprise Resource Planning</div>' +
      '<div class="splash-bar"></div>';
    if (document.body) {
      document.body.insertBefore(s, document.body.firstChild);
    } else {
      requestAnimationFrame(_mountSplash);
    }
  }
  try { _mountSplash(); } catch (_e) {}

  // GUARANTEED REVEAL: regardless of anything else, after MAX_SPLASH_MS,
  // mark the page ready and fade the splash out. This is the safety net
  // that makes sure the user always sees the page.
  var MAX_SPLASH_MS = 1000;
  function _revealPage() {
    if (document.documentElement.classList.contains("app-ready")) return;
    document.documentElement.classList.add("app-ready");
    var s = document.getElementById("app-splash");
    if (s) {
      s.classList.add("app-splash-hide");
      setTimeout(function () {
        if (s && s.parentNode) s.parentNode.removeChild(s);
      }, 450);
    }
  }
  // Fire reveal after MAX_SPLASH_MS, no matter what.
  setTimeout(_revealPage, MAX_SPLASH_MS);
  // Also expose globally so other code can reveal early if it wants.
  window.__appRevealPage = _revealPage;
  // ──────────────────────────────────────────────────────────────────────
  var palettes = {
    dark: {
      bg: "#0B1120",
      surface: "#1E293B",
      surfaceRaised: "#334155",
      border: "#334155",
      borderStrong: "#475569",
      text: "#F8FAFC",
      textSub: "#cbd5e1",
      textMuted: "#94A3B8",
      accent: "#00E5FF",
      accentSoft: "rgba(0, 229, 255, 0.14)",
      chartPrimary: "#00E5FF"
    },
    light: {
      bg: "#ffffff",
      surface: "#f8fafc",
      surfaceRaised: "#e2e8f0",
      border: "#e2e8f0",
      borderStrong: "#cbd5e1",
      text: "#0f172a",
      textSub: "#334155",
      textMuted: "#64748b",
      accent: "#00B8D4",
      accentSoft: "rgba(0, 184, 212, 0.12)",
      chartPrimary: "#00B8D4"
    }
  };
  var appliedTheme = "dark";

  function installDashboardPaletteStyle() {
    if (document.getElementById("app-dashboard-palette-style")) return;

    var darkVars = [
      "--bg:#0B1120",
      "--surface:#1E293B",
      "--surface-raised:#334155",
      "--card:#1E293B",
      "--card2:#334155",
      "--border:#334155",
      "--border2:#475569",
      "--border-strong:#475569",
      "--text:#F8FAFC",
      "--text-sub:#cbd5e1",
      "--text-muted:#94A3B8",
      "--sub:#cbd5e1",
      "--muted:#94A3B8",
      "--accent:#00E5FF",
      "--accent-soft:rgba(0,229,255,.14)",
      "--blue:#38bdf8",
      "--green:#22c55e",
      "--green2:#22c55e",
      "--positive:#22c55e",
      "--negative:#f87171",
      "--danger:#f87171",
      "--rose:#fb7185",
      "--rose2:#fb7185",
      "--warn:#f59e0b",
      "--warning:#f59e0b",
      "--amber:#f59e0b",
      "--amber2:#d97706",
      "--orange:#fb923c",
      "--teal:#2dd4bf",
      "--purple:#a855f7",
      "--lime:#84cc16"
    ];
    var lightVars = [
      "--bg:#ffffff",
      "--surface:#f8fafc",
      "--surface-raised:#e2e8f0",
      "--card:#f8fafc",
      "--card2:#e2e8f0",
      "--border:#e2e8f0",
      "--border2:#cbd5e1",
      "--border-strong:#cbd5e1",
      "--text:#0f172a",
      "--text-sub:#334155",
      "--text-muted:#64748b",
      "--sub:#334155",
      "--muted:#64748b",
      "--accent:#00B8D4",
      "--accent-soft:rgba(0,184,212,.12)",
      "--blue:#0284c7",
      "--green:#15803d",
      "--green2:#15803d",
      "--positive:#15803d",
      "--negative:#dc2626",
      "--danger:#dc2626",
      "--rose:#e11d48",
      "--rose2:#e11d48",
      "--warn:#b45309",
      "--warning:#b45309",
      "--amber:#b45309",
      "--amber2:#92400e",
      "--orange:#ea580c",
      "--teal:#0f766e",
      "--purple:#7e22ce",
      "--lime:#4d7c0f"
    ];

    var style = document.createElement("style");
    style.id = "app-dashboard-palette-style";
    style.textContent = 'html[data-theme="dark"],body[data-theme="dark"]{' +
      darkVars.map(function (item) { return item + " !important;"; }).join("") +
      "}" +
      'html[data-theme="light"],body[data-theme="light"],body.light{' +
      lightVars.map(function (item) { return item + " !important;"; }).join("") +
      "}";
    (document.head || document.documentElement).appendChild(style);
  }

  function installDashboardBackgroundStyle() {
    if (document.getElementById("app-dashboard-background-style")) return;
    var style = document.createElement("style");
    style.id = "app-dashboard-background-style";
    style.textContent = [
      ".app-bg-layer{position:fixed;inset:0;z-index:0;overflow:hidden;transform:translateZ(0);pointer-events:none;}",
      ".app-bg-orb{position:absolute;border-radius:50%;filter:blur(80px);opacity:.18;animation:appBgOrbFloat 18s ease-in-out infinite alternate;}",
      ".app-bg-orb:nth-child(1){width:700px;height:500px;top:-10%;left:-15%;background:radial-gradient(circle,#00E5FF,transparent 70%);animation-duration:20s;}",
      ".app-bg-orb:nth-child(2){width:500px;height:600px;top:30%;right:-10%;background:radial-gradient(circle,#38bdf8,transparent 70%);animation-duration:25s;animation-delay:-8s;}",
      ".app-bg-orb:nth-child(3){width:400px;height:400px;bottom:-10%;left:30%;background:radial-gradient(circle,#64748b,transparent 70%);animation-duration:22s;animation-delay:-4s;}",
      ".app-bg-grain{position:fixed;inset:0;z-index:1;background-image:url(\"data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.04'/%3E%3C/svg%3E\");pointer-events:none;opacity:.4;}",
      "html[data-theme=\"light\"] .app-bg-orb{opacity:.08;}",
      "html[data-theme=\"light\"] .app-bg-grain{opacity:.15;}",
      "body>:where(:not(.app-bg-layer):not(.app-bg-grain):not(.bg-layer):not(.bg-grain):not(script):not(style)){position:relative;z-index:2;}",
      "@keyframes appBgOrbFloat{0%{transform:translate(0,0);}33%{transform:translate(30px,-20px);}66%{transform:translate(-20px,30px);}100%{transform:translate(10px,10px);}}",
      "@media print{.app-bg-layer,.app-bg-grain{display:none!important;}}"
    ].join("");
    (document.head || document.documentElement).appendChild(style);
  }

  function mountDashboardBackground() {
    if (!document.body) return;
    if (
      document.documentElement.getAttribute("data-app-bg") === "off" ||
      document.body.getAttribute("data-app-bg") === "off"
    ) return;
    if (document.querySelector(".bg-layer,.app-bg-layer")) return;

    var layer = document.createElement("div");
    layer.className = "app-bg-layer";
    layer.setAttribute("aria-hidden", "true");
    for (var i = 0; i < 3; i += 1) {
      var orb = document.createElement("div");
      orb.className = "app-bg-orb";
      layer.appendChild(orb);
    }

    var grain = document.createElement("div");
    grain.className = "app-bg-grain";
    grain.setAttribute("aria-hidden", "true");
    document.body.insertBefore(grain, document.body.firstChild);
    document.body.insertBefore(layer, grain);
  }

  function installLogoThemeStyle() {
    if (document.getElementById("app-logo-theme-style")) return;
    var style = document.createElement("style");
    style.id = "app-logo-theme-style";
    style.textContent = [
      ':where(.navbar-brand,.logo,.app-nav-brand) > img[src="/static/ERP_logo.png"]{transition:filter .18s ease;}',
      'html[data-theme="light"] :where(.navbar-brand,.logo,.app-nav-brand) > img[src="/static/ERP_logo.png"],',
      'body.light :where(.navbar-brand,.logo,.app-nav-brand) > img[src="/static/ERP_logo.png"]{filter:invert(1) hue-rotate(180deg);}',
      'html[data-theme="dark"] :where(.navbar-brand,.logo,.app-nav-brand) > img[src="/static/ERP_logo.png"]{filter:none;}'
    ].join("");
    (document.head || document.documentElement).appendChild(style);
  }

  function installControlThemeStyle() {
    if (document.getElementById("app-control-theme-style")) return;
    var style = document.createElement("style");
    style.id = "app-control-theme-style";
    style.textContent = [
      ":root{--app-control-bg:#1E293B;--app-control-bg-hover:#334155;--app-control-border:#334155;--app-control-border-hover:#475569;--app-control-text:#cbd5e1;--app-control-text-strong:#F8FAFC;--app-control-muted:#94A3B8;--app-control-shadow:0 24px 50px rgba(0,0,0,.35);}",
      'html[data-theme="light"]{--app-control-bg:#f8fafc;--app-control-bg-hover:#e2e8f0;--app-control-border:#e2e8f0;--app-control-border-hover:#cbd5e1;--app-control-text:#334155;--app-control-text-strong:#0f172a;--app-control-muted:#64748b;--app-control-shadow:0 18px 44px rgba(15,23,42,.12);}',
      ":where(.mode-btn,.app-theme-toggle,#themeToggle){display:inline-flex!important;align-items:center!important;justify-content:center!important;width:36px!important;height:36px!important;min-width:36px!important;min-height:36px!important;padding:0!important;border-radius:10px!important;border:1px solid var(--app-control-border)!important;background:var(--app-control-bg)!important;color:var(--app-control-text)!important;box-shadow:none!important;font-size:16px!important;line-height:1!important;transition:border-color .18s ease,color .18s ease,background .18s ease,transform .18s ease!important;}",
      ":where(.mode-btn,.app-theme-toggle,#themeToggle):hover{border-color:var(--app-control-border-hover)!important;background:var(--app-control-bg-hover)!important;color:var(--app-control-text-strong)!important;transform:scale(1.06)!important;}",
      ":where(.user-pill){display:flex!important;align-items:center!important;gap:10px!important;min-height:36px!important;padding:7px 16px 7px 10px!important;border-radius:40px!important;border:1px solid var(--app-control-border)!important;background:var(--app-control-bg)!important;color:var(--app-control-text)!important;box-shadow:none!important;}",
      ":where(.user-pill:hover,.user-pill.open){border-color:var(--app-control-border-hover)!important;background:var(--app-control-bg-hover)!important;color:var(--app-control-text-strong)!important;}",
      ":where(.user-avatar){width:28px!important;height:28px!important;border-radius:50%!important;display:flex!important;align-items:center!important;justify-content:center!important;flex:0 0 28px!important;background:linear-gradient(135deg,#00B8D4,#f59e0b)!important;color:#0a0c08!important;font-size:12px!important;font-weight:700!important;}",
      ":where(.user-name){font-size:13px!important;font-weight:500!important;color:currentColor!important;}",
      ":where(.menu-caret){font-size:11px!important;color:var(--app-control-muted)!important;}",
      ":where(.account-dropdown){min-width:220px!important;background:var(--app-control-bg)!important;border:1px solid var(--app-control-border-hover)!important;border-radius:14px!important;padding:8px!important;box-shadow:var(--app-control-shadow)!important;color:var(--app-control-text)!important;}",
      ":where(.account-head){border-bottom:1px solid var(--app-control-border)!important;}",
      ":where(.account-label){color:var(--app-control-muted)!important;}",
      ":where(.account-email){color:var(--app-control-text)!important;}",
      ":where(.account-item){color:var(--app-control-text)!important;border-radius:10px!important;}",
      ":where(.account-item:hover){background:var(--app-control-bg-hover)!important;color:var(--app-control-text-strong)!important;}"
    ].join("");
    (document.head || document.documentElement).appendChild(style);
  }

  function installHeaderThemeStyle() {
    if (document.getElementById("app-header-theme-style")) return;
    var style = document.createElement("style");
    style.id = "app-header-theme-style";
    style.textContent = [
      'html[data-theme="light"] :where(.app-nav,.top-nav,#topbar,.site-nav){background:rgba(255,255,255,.92)!important;color:#0f172a!important;border-color:#e2e8f0!important;}',
      'html[data-theme="light"] :where(.app-nav a,.app-nav summary,.top-nav a,.top-nav button,#topbar a,#topbar button,.site-nav a,.site-nav button,.nav-link,.nav-links a){color:#334155!important;}',
      'html[data-theme="light"] :where(.app-nav a:hover,.app-nav summary:hover,.top-nav a:hover,.top-nav button:hover,#topbar a:hover,#topbar button:hover,.site-nav a:hover,.site-nav button:hover,.nav-link:hover,.nav-links a:hover){color:#0f172a!important;}',
      'html[data-theme="light"] :where(.app-nav-menu,.account-dropdown){background:#f8fafc!important;color:#334155!important;border-color:#cbd5e1!important;}',
      'html[data-theme="light"] :where(.app-nav-menu-item:hover,.account-item:hover){background:#e2e8f0!important;color:#0f172a!important;}',
      'html[data-theme="dark"] :where(.app-nav,.top-nav,#topbar,.site-nav){color:#F8FAFC;}'
    ].join("");
    (document.head || document.documentElement).appendChild(style);
  }

  function syncRootTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.style.colorScheme = theme;
    document.documentElement.classList.toggle("light", theme === "light");
  }

  function syncBodyTheme(theme) {
    if (!document.body) return;
    document.body.dataset.theme = theme;
    document.body.setAttribute("data-theme", theme);
    document.body.classList.toggle("light", theme === "light");
  }

  function normalizeTheme(theme) {
    return theme === "light" ? "light" : "dark";
  }

  function readStoredTheme() {
    try {
      var stored = localStorage.getItem(THEME_KEY);
      if (stored) return normalizeTheme(stored);

      for (var i = 0; i < LEGACY_KEYS.length; i += 1) {
        var legacy = localStorage.getItem(LEGACY_KEYS[i]);
        if (legacy) {
          localStorage.setItem(THEME_KEY, normalizeTheme(legacy));
          return normalizeTheme(legacy);
        }
      }
    } catch (_) {}

    return normalizeTheme(document.documentElement.getAttribute("data-theme"));
  }

  function persistTheme(theme) {
    try {
      localStorage.setItem(THEME_KEY, normalizeTheme(theme));
      for (var i = 0; i < LEGACY_KEYS.length; i += 1) {
        localStorage.removeItem(LEGACY_KEYS[i]);
      }
    } catch (_) {}
  }

  function syncThemeButtons(theme) {
    var next = normalizeTheme(theme);
    var icon = next === "light" ? "&#9728;&#65039;" : "&#127769;";
    var label = next === "light" ? "Switch to dark theme" : "Switch to light theme";
    document.querySelectorAll("[data-theme-toggle],#mode-btn,#themeToggle,.app-theme-toggle").forEach(function (button) {
      if (!button.querySelector("svg")) button.innerHTML = icon;
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
      button.setAttribute("aria-pressed", next === "light" ? "true" : "false");
    });
  }

  function applyTheme(theme, options) {
    var settings = options || {};
    var next = normalizeTheme(theme);
    appliedTheme = next;
    syncRootTheme(next);
    syncBodyTheme(next);
    syncThemeButtons(next);
    window.__appThemePalette = palettes[next];
    if (settings.persist !== false) persistTheme(next);
    if (settings.dispatch !== false) {
      try {
        window.dispatchEvent(new CustomEvent("app:themechange", { detail: { theme: next } }));
      } catch (_) {}
    }
    return next;
  }

  if (!window.__appTheme) {
    window.__appTheme = {
      get: function () {
        return normalizeTheme(document.documentElement.getAttribute("data-theme") || readStoredTheme());
      },
      set: function (theme) {
        return applyTheme(theme);
      },
      toggle: function () {
        return applyTheme(this.get() === "light" ? "dark" : "light");
      },
      sync: function () {
        return applyTheme(readStoredTheme(), { persist: false, dispatch: false });
      },
      palette: function (theme) {
        return palettes[normalizeTheme(theme || this.get())];
      },
      key: THEME_KEY
    };
  }

  if (!window.__appThemeModeButtonBound) {
    window.__appThemeModeButtonBound = true;
    document.addEventListener("click", function (event) {
      var trigger = event.target && event.target.closest && event.target.closest("[data-theme-toggle],#mode-btn,#themeToggle,.app-theme-toggle");
      if (!trigger || !document.contains(trigger)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      window.__appTheme.toggle();
    }, true);
  }

  window.addEventListener("storage", function (event) {
    if ([THEME_KEY].concat(LEGACY_KEYS).indexOf(event.key || "") === -1) return;
    applyTheme(readStoredTheme(), { persist: false, dispatch: true });
  });

  try {
    var theme = readStoredTheme();
    appliedTheme = theme;
    installDashboardPaletteStyle();
    installDashboardBackgroundStyle();
    installLogoThemeStyle();
    installControlThemeStyle();
    installHeaderThemeStyle();
    applyTheme(theme, { persist: false, dispatch: false });
    window.__appThemePalette = palettes[theme];
  } catch (_) {
    installDashboardPaletteStyle();
    installDashboardBackgroundStyle();
    installLogoThemeStyle();
    installControlThemeStyle();
    installHeaderThemeStyle();
    applyTheme("dark", { persist: false, dispatch: false });
    window.__appThemePalette = palettes.dark;
  }
  syncBodyTheme(appliedTheme);
  document.addEventListener("DOMContentLoaded", function () {
    var theme = document.documentElement.getAttribute("data-theme") || appliedTheme;
    applyTheme(theme, { persist: false, dispatch: false });
    mountDashboardBackground();
  });
})();