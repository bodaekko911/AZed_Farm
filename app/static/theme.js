(function () {
  if (window.__appTheme) return;

  const THEME_KEY = "colorMode";
  const LEGACY_KEYS = ["dashboard:theme", "expenses-theme", "refund-theme"];
  const LIGHT = "light";
  const DARK = "dark";
  const PALETTES = {
    [DARK]: {
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
      chartPrimary: "#00E5FF",
      chartSecondary: "#38bdf8",
      chartNegative: "#f87171",
    },
    [LIGHT]: {
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
      chartPrimary: "#00B8D4",
      chartSecondary: "#0284c7",
      chartNegative: "#dc2626",
    },
  };

  function normalizeTheme(theme) {
    return theme === LIGHT ? LIGHT : DARK;
  }

  function getPalette(theme) {
    return PALETTES[normalizeTheme(theme)];
  }

  function readStoredTheme() {
    try {
      const stored = localStorage.getItem(THEME_KEY);
      if (stored) return normalizeTheme(stored);

      for (const key of LEGACY_KEYS) {
        const legacy = localStorage.getItem(key);
        if (legacy) return normalizeTheme(legacy);
      }
    } catch (_) {}

    const rootTheme = document.documentElement.getAttribute("data-theme");
    if (rootTheme) return normalizeTheme(rootTheme);
    return document.body && document.body.classList.contains(LIGHT) ? LIGHT : DARK;
  }

  function writeStoredTheme(theme) {
    try {
      localStorage.setItem(THEME_KEY, theme);
      for (const key of LEGACY_KEYS) {
        localStorage.removeItem(key);
      }
    } catch (_) {}
  }

  function updateButtons(theme) {
    const label = theme === LIGHT ? "&#9728;&#65039;" : "&#127769;";
    document.querySelectorAll("#mode-btn").forEach((button) => {
      button.innerHTML = label;
    });
  }

  function applyTheme(theme, options) {
    const settings = Object.assign({ persist: true, dispatch: true }, options);
    const nextTheme = normalizeTheme(theme);

    document.documentElement.dataset.theme = nextTheme;
    document.documentElement.setAttribute("data-theme", nextTheme);
    document.documentElement.style.colorScheme = nextTheme;
    document.documentElement.classList.toggle(LIGHT, nextTheme === LIGHT);
    if (document.body) {
      document.body.dataset.theme = nextTheme;
      document.body.setAttribute("data-theme", nextTheme);
      document.body.classList.toggle(LIGHT, nextTheme === LIGHT);
    }
    window.__appThemePalette = getPalette(nextTheme);

    updateButtons(nextTheme);

    if (settings.persist) {
      writeStoredTheme(nextTheme);
    }

    if (settings.dispatch) {
      window.dispatchEvent(new CustomEvent("app:themechange", { detail: { theme: nextTheme } }));
    }

    return nextTheme;
  }

  function ensureTheme(options) {
    return applyTheme(readStoredTheme(), options);
  }

  window.__appTheme = {
    get() {
      return normalizeTheme(document.documentElement.dataset.theme || readStoredTheme());
    },
    set(theme) {
      return applyTheme(theme);
    },
    toggle() {
      return applyTheme(this.get() === LIGHT ? DARK : LIGHT);
    },
    sync() {
      return ensureTheme({ persist: false, dispatch: false });
    },
    palette(theme) {
      return getPalette(theme || this.get());
    },
    key: THEME_KEY,
  };

  ensureTheme({ dispatch: false });

  document.addEventListener("DOMContentLoaded", () => {
    ensureTheme({ persist: false, dispatch: false });
  });

  window.addEventListener("storage", (event) => {
    if (![THEME_KEY, ...LEGACY_KEYS].includes(event.key || "")) return;
    ensureTheme({ persist: true, dispatch: true });
  });
})();
