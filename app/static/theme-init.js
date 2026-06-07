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

  // NOTE: The full-screen splash overlay was removed. It hid all <body>
  // content behind a fixed 1s timer on EVERY page load, which on a
  // multi-page (full-reload) app reads as a flash/flicker on every
  // navigation. The pre-paint theme block above already prevents the
  // white/colour flash (the genuinely useful part), so no overlay is
  // needed. `.app-ready` is always set immediately so any leftover
  // `:not(.app-ready)` rules from cached pages still resolve.
  try { document.documentElement.classList.add("app-ready"); } catch (_e) {}

  // Kept as a harmless no-op for backward compatibility with any caller
  // that still references it.
  window.__appRevealPage = function () {
    try { document.documentElement.classList.add("app-ready"); } catch (_e) {}
  };
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
      ".app-bg-orb{position:absolute;border-radius:50%;filter:blur(80px);opacity:.18;}",
      ".app-bg-orb:nth-child(1){width:700px;height:500px;top:-10%;left:-15%;background:radial-gradient(circle,#00E5FF,transparent 70%);animation-duration:20s;}",
      ".app-bg-orb:nth-child(2){width:500px;height:600px;top:30%;right:-10%;background:radial-gradient(circle,#38bdf8,transparent 70%);animation-duration:25s;animation-delay:-8s;}",
      ".app-bg-orb:nth-child(3){width:400px;height:400px;bottom:-10%;left:30%;background:radial-gradient(circle,#64748b,transparent 70%);animation-duration:22s;animation-delay:-4s;}",
      ".app-bg-grain{position:fixed;inset:0;z-index:1;background-image:url(\"data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.04'/%3E%3C/svg%3E\");pointer-events:none;opacity:.4;}",
      "html[data-theme=\"light\"] .app-bg-orb{opacity:.08;}",
      "html[data-theme=\"light\"] .app-bg-grain{opacity:.15;}",
      "body>:where(:not(.app-bg-layer):not(.app-bg-grain):not(.bg-layer):not(.bg-grain):not(script):not(style)){position:relative;z-index:2;}",
      "@keyframes appBgOrbFloat{0%{transform:translate(0,0);}33%{transform:translate(30px,-20px);}66%{transform:translate(-20px,30px);}100%{transform:translate(10px,10px);}}",
      "@media print{.app-bg-layer,.app-bg-grain{display:none!important;}}",
      // FLICKER FIX: stop the continuously-animating background orbs. Large
      // blur(80px) elements animating forever sit behind the nav/cards that use
      // backdrop-filter, forcing Chrome to re-sample the blurred backdrop every
      // frame -> constant full-page pulsing. Static orbs keep the glow without
      // the flicker. Covers both the page-defined `.bg-orb` (dashboard.css,
      // home.py, etc.) and the injected `.app-bg-orb`. !important beats page CSS
      // regardless of stylesheet load order.
      ".bg-orb,.app-bg-orb{animation:none!important;will-change:auto!important;transform:none!important;}",
      // FLICKER FIX (round 2): a backdrop-filter element (the sticky nav) sitting
      // over a GPU-promoted fixed background can make Chromium re-sample the
      // blurred backdrop every frame -> constant pulsing even with no animation.
      // Remove the blur on the bars, make them opaque, and drop the forced
      // compositing layer on the background so it's painted normally.
      ".app-bg-layer,.bg-layer{transform:none!important;will-change:auto!important;}",
      ":where(.app-nav,.top-nav,#topbar,.site-nav,.topbar){backdrop-filter:none!important;-webkit-backdrop-filter:none!important;}",
      'html[data-theme="dark"] :where(.app-nav,.top-nav,#topbar,.site-nav,.topbar){background:#0B1120!important;}',
      'html[data-theme="light"] :where(.app-nav,.top-nav,#topbar,.site-nav,.topbar){background:#ffffff!important;}'
    ].join("");
    (document.head || document.documentElement).appendChild(style);
    // Visible/verifiable marker so the deployed file can be confirmed as live.
    try {
      document.documentElement.setAttribute("data-flicker-fix", "v3");
      if (window.console && console.info) console.info("theme-init flicker-fix v3 active");
    } catch (_e) {}
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