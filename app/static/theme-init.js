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
    var stored = localStorage.getItem(THEME_KEY);
    if (stored) return normalizeTheme(stored);

    for (var i = 0; i < LEGACY_KEYS.length; i += 1) {
      var legacy = localStorage.getItem(LEGACY_KEYS[i]);
      if (legacy) {
        localStorage.setItem(THEME_KEY, normalizeTheme(legacy));
        return normalizeTheme(legacy);
      }
    }

    return normalizeTheme(document.documentElement.getAttribute("data-theme"));
  }

  try {
    var theme = readStoredTheme();
    appliedTheme = theme;
    installDashboardPaletteStyle();
    installDashboardBackgroundStyle();
    installLogoThemeStyle();
    syncRootTheme(theme);
    window.__appThemePalette = palettes[theme];
  } catch (_) {
    installDashboardPaletteStyle();
    installDashboardBackgroundStyle();
    installLogoThemeStyle();
    syncRootTheme("dark");
    window.__appThemePalette = palettes.dark;
  }
  syncBodyTheme(appliedTheme);
  document.addEventListener("DOMContentLoaded", function () {
    var theme = document.documentElement.getAttribute("data-theme") || appliedTheme;
    syncRootTheme(theme);
    syncBodyTheme(theme);
    mountDashboardBackground();
  });
})();
