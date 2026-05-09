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

    var style = document.createElement("style");
    style.id = "app-dashboard-palette-style";
    style.textContent = 'html[data-theme="dark"],body[data-theme="dark"]{' +
      darkVars.map(function (item) { return item + " !important;"; }).join("") +
      "}";
    (document.head || document.documentElement).appendChild(style);
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

  try {
    var stored = localStorage.getItem("colorMode");
    // Legacy keys older builds may still have:
    if (!stored) {
      var legacy = ["dashboard:theme", "expenses-theme", "refund-theme"];
      for (var i = 0; i < legacy.length; i += 1) {
        var v = localStorage.getItem(legacy[i]);
        if (v) { stored = v; break; }
      }
    }
    var theme = stored === "light" ? "light" : "dark";
    appliedTheme = theme;
    installDashboardPaletteStyle();
    installLogoThemeStyle();
    syncRootTheme(theme);
    window.__appThemePalette = palettes[theme];
  } catch (_) {
    installDashboardPaletteStyle();
    installLogoThemeStyle();
    syncRootTheme("dark");
    window.__appThemePalette = palettes.dark;
  }
  syncBodyTheme(appliedTheme);
  document.addEventListener("DOMContentLoaded", function () {
    var theme = document.documentElement.getAttribute("data-theme") || appliedTheme;
    syncRootTheme(theme);
    syncBodyTheme(theme);
  });
})();
