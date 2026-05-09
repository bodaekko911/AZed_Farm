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
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.style.colorScheme = theme;
    window.__appThemePalette = palettes[theme];
    if (theme === "light") {
      document.documentElement.classList.add("light");
    }
  } catch (_) {
    document.documentElement.setAttribute("data-theme", "dark");
    document.documentElement.style.colorScheme = "dark";
    window.__appThemePalette = palettes.dark;
  }
  syncBodyTheme(appliedTheme);
  document.addEventListener("DOMContentLoaded", function () {
    syncBodyTheme(document.documentElement.getAttribute("data-theme") || appliedTheme);
  });
})();
