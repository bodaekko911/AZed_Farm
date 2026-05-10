let currentRange = localStorage.getItem("dashboard:range") || "mtd";
let customStart = null;
let customEnd = null;
let lastUpdatedAt = null;
let elapsedTimer = null;
let refreshTimer = null;
let salesChart = null;
let topProductsTab = "revenue";
let activityFilter = "all";
let b2bClientsTab = "revenue";
let dashboardData = null;
let currentUser = null;
let dashboardAbortController = null;
let dashboardRequestId = 0;
let dashboardHasLoaded = false;
let dashboardIsStale = false;
let errorBannerDismissed = false;

const SWR_MAX_AGE_MS = 5 * 60 * 1000;

function escHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));
}

function formatMoney(value) {
  return `EGP ${Math.round(Number(value || 0)).toLocaleString("en-GB")}`;
}

function signedMoney(value) {
  const number = Number(value || 0);
  return `${number < 0 ? "-" : ""}${formatMoney(Math.abs(number))}`;
}

function formatMoneyPrecise(value) {
  return `EGP ${Number(value || 0).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("en-GB");
}

function percentText(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return `${Number(value).toFixed(1).replace(".0", "")}%`;
}

function ratioOf(value, total) {
  const denominator = Math.max(Math.abs(Number(total || 0)), 1);
  return Math.min(100, Math.max(4, Math.round((Math.abs(Number(value || 0)) / denominator) * 100)));
}

function setHTML(el, html) {
  if (!el) return;
  if (el.innerHTML !== html) el.innerHTML = html;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el && el.textContent !== text) el.textContent = text;
}

function longDateLabel() {
  return new Date().toLocaleDateString("en-GB", {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function greetingForHour(hour) {
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

function setGreeting() {
  const name = (currentUser?.name || "there").split(" ")[0];
  const hour = new Date().getHours();
  setText("greeting", `${greetingForHour(hour)}, ${name}`);
  setText("date-display", longDateLabel());
}

function injectDashboardUpgradeStyles() {
  if (document.getElementById("dashboard-upgrade-styles")) return;

  const style = document.createElement("style");
  style.id = "dashboard-upgrade-styles";
  style.textContent = `
    #profit-summary-card,
    #top-b2b-card {
      overflow: hidden;
      position: relative;
      isolation: isolate;
    }

    #profit-summary-card::before,
    #top-b2b-card::before {
      content: "";
      position: absolute;
      inset: 0 0 auto 0;
      height: 4px;
      background: linear-gradient(90deg, var(--accent), var(--positive), var(--warning));
      opacity: .9;
      z-index: -1;
    }

    #profit-summary-card .panel-head,
    #top-b2b-card .panel-head {
      align-items: flex-start;
      gap: 12px;
    }

    .profit-summary-shell,
    .b2b-summary-shell {
      display: grid;
      gap: 16px;
    }

    .profit-hero-card,
    .b2b-hero-card {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 16px;
      align-items: start;
      padding: 16px;
      border: 1px solid color-mix(in srgb, var(--border-strong) 76%, transparent);
      border-radius: 18px;
      background:
        radial-gradient(circle at 100% 0%, color-mix(in srgb, var(--accent) 16%, transparent), transparent 38%),
        color-mix(in srgb, var(--surface-raised) 42%, transparent);
    }

    .profit-hero-label,
    .b2b-hero-label,
    .mini-kpi-label,
    .profit-flow-label,
    .b2b-stat-label {
      display: block;
      color: var(--text-muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
    }

    .profit-hero-value,
    .b2b-hero-value {
      display: block;
      margin-top: 4px;
      color: var(--text);
      font-family: var(--font-display, var(--font-sans));
      font-size: clamp(28px, 4vw, 42px);
      line-height: 1;
      font-weight: 900;
      letter-spacing: -.06em;
      font-variant-numeric: tabular-nums;
    }

    .profit-hero-value.positive,
    .profit-final-value.positive,
    .profit-flow-value.positive,
    .mini-kpi-value.positive,
    .b2b-stat-value.positive,
    .b2b-row-money.positive {
      color: var(--positive);
    }

    .profit-hero-value.negative,
    .profit-final-value.negative,
    .profit-flow-value.negative,
    .mini-kpi-value.negative,
    .b2b-stat-value.negative,
    .b2b-row-money.negative {
      color: var(--negative);
    }

    .profit-hero-sub,
    .b2b-hero-sub {
      margin: 10px 0 0;
      color: var(--text-sub);
      font-size: 13px;
      line-height: 1.45;
    }

    .profit-status-badge,
    .b2b-status-badge,
    .b2b-debt-pill,
    .b2b-clean-pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 30px;
      padding: 0 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
      border: 1px solid transparent;
    }

    .profit-status-badge.up,
    .b2b-status-badge.good,
    .b2b-clean-pill {
      color: var(--positive);
      background: color-mix(in srgb, var(--positive) 12%, transparent);
      border-color: color-mix(in srgb, var(--positive) 28%, transparent);
    }

    .profit-status-badge.down,
    .b2b-debt-pill {
      color: var(--negative);
      background: color-mix(in srgb, var(--negative) 12%, transparent);
      border-color: color-mix(in srgb, var(--negative) 28%, transparent);
    }

    .profit-status-badge.neutral,
    .b2b-status-badge.neutral {
      color: var(--text-sub);
      background: color-mix(in srgb, var(--surface-raised) 58%, transparent);
      border-color: var(--border);
    }

    .profit-mini-grid,
    .b2b-stat-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }

    .mini-kpi,
    .b2b-stat-card {
      padding: 13px 14px;
      border: 1px solid var(--border);
      border-radius: 16px;
      background: color-mix(in srgb, var(--surface) 72%, transparent);
    }

    .mini-kpi-value,
    .b2b-stat-value {
      display: block;
      margin-top: 5px;
      color: var(--text);
      font-size: 18px;
      font-weight: 900;
      letter-spacing: -.035em;
      font-variant-numeric: tabular-nums;
    }

    .profit-waterfall {
      display: grid;
      gap: 10px;
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: 18px;
      background: color-mix(in srgb, var(--surface) 70%, transparent);
    }

    .profit-flow-row {
      display: grid;
      grid-template-columns: 132px minmax(80px, 1fr) 96px 48px;
      gap: 10px;
      align-items: center;
    }

    .profit-flow-value,
    .profit-flow-pct,
    .profit-final-value {
      text-align: end;
      font-weight: 800;
      color: var(--text);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }

    .profit-flow-pct {
      color: var(--text-muted);
      font-size: 12px;
    }

    .profit-track {
      position: relative;
      height: 9px;
      border-radius: 999px;
      overflow: hidden;
      background: color-mix(in srgb, var(--border) 72%, transparent);
    }

    .profit-fill {
      display: block;
      height: 100%;
      width: 0;
      border-radius: inherit;
      transition: width .28s ease;
    }

    .profit-fill.revenue { background: linear-gradient(90deg, var(--accent), var(--blue)); }
    .profit-fill.cogs { background: linear-gradient(90deg, var(--warning), color-mix(in srgb, var(--warning) 54%, transparent)); }
    .profit-fill.gross { background: linear-gradient(90deg, var(--positive), color-mix(in srgb, var(--positive) 54%, transparent)); }
    .profit-fill.opex { background: linear-gradient(90deg, var(--rose), var(--negative)); }
    .profit-fill.net { background: linear-gradient(90deg, var(--accent), var(--positive)); }
    .profit-fill.net.negative { background: linear-gradient(90deg, var(--negative), var(--rose)); }

    .profit-final-row {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: center;
      padding: 14px;
      border-radius: 16px;
      background: color-mix(in srgb, var(--surface-raised) 46%, transparent);
      border: 1px solid var(--border);
    }

    .profit-final-title {
      display: block;
      color: var(--text);
      font-weight: 900;
      letter-spacing: -.02em;
    }

    .profit-final-sub {
      display: block;
      color: var(--text-muted);
      font-size: 12px;
      margin-top: 2px;
    }

    .profit-final-value {
      font-size: 22px;
      letter-spacing: -.045em;
    }

    .b2b-client-list {
      display: grid;
      gap: 10px;
    }

    .b2b-client-card {
      display: grid;
      grid-template-columns: 34px 40px minmax(0, 1fr) auto;
      align-items: center;
      gap: 11px;
      padding: 12px;
      border: 1px solid var(--border);
      border-radius: 16px;
      background: color-mix(in srgb, var(--surface) 72%, transparent);
      transition: transform .16s ease, border-color .16s ease, background .16s ease;
    }

    .b2b-client-card:hover {
      transform: translateY(-1px);
      border-color: color-mix(in srgb, var(--accent) 42%, var(--border));
      background: color-mix(in srgb, var(--surface-raised) 58%, transparent);
    }

    .b2b-rank-chip {
      width: 28px;
      height: 28px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      color: var(--text-muted);
      background: color-mix(in srgb, var(--surface-raised) 58%, transparent);
      border: 1px solid var(--border);
      font-weight: 900;
      font-size: 12px;
    }

    .b2b-avatar {
      width: 40px;
      height: 40px;
      border-radius: 14px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: #06111a;
      font-weight: 900;
      font-size: 13px;
      background: linear-gradient(135deg, var(--accent), var(--blue));
      box-shadow: 0 10px 22px color-mix(in srgb, var(--accent) 16%, transparent);
    }

    .b2b-avatar-success { background: linear-gradient(135deg, var(--positive), var(--accent)); }
    .b2b-avatar-warning { background: linear-gradient(135deg, var(--warning), var(--positive)); }
    .b2b-avatar-rose { background: linear-gradient(135deg, var(--rose), var(--warning)); }
    .b2b-avatar-secondary { background: linear-gradient(135deg, var(--blue), var(--accent)); }

    .b2b-main {
      min-width: 0;
    }

    .b2b-name-line {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }

    .b2b-name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--text);
      font-weight: 900;
      letter-spacing: -.02em;
    }

    .b2b-meta {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 3px;
      color: var(--text-muted);
      font-size: 12px;
      font-weight: 700;
    }

    .b2b-client-meter {
      display: flex;
      align-items: center;
      gap: 9px;
      min-width: 170px;
    }

    .b2b-client-track {
      width: 86px;
      height: 8px;
      border-radius: 999px;
      overflow: hidden;
      background: color-mix(in srgb, var(--border) 74%, transparent);
    }

    .b2b-client-fill {
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--accent), var(--positive));
    }

    .b2b-client-fill.warning {
      background: linear-gradient(90deg, var(--warning), var(--negative));
    }

    .b2b-row-money {
      min-width: 88px;
      text-align: end;
      color: var(--text);
      font-weight: 900;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }

    .dashboard-upgrade-empty {
      display: grid;
      gap: 8px;
      place-items: center;
      text-align: center;
      min-height: 180px;
      color: var(--text-sub);
      padding: 24px 14px;
      border: 1px dashed var(--border-strong);
      border-radius: 18px;
      background: color-mix(in srgb, var(--surface) 68%, transparent);
    }

    .dashboard-upgrade-empty strong {
      color: var(--text);
      font-size: 15px;
    }

    @media (max-width: 1120px) {
      .profit-mini-grid,
      .b2b-stat-grid {
        grid-template-columns: 1fr;
      }
      .profit-flow-row {
        grid-template-columns: 108px minmax(80px, 1fr) 86px 44px;
      }
      .b2b-client-card {
        grid-template-columns: 32px 38px minmax(0, 1fr);
      }
      .b2b-client-meter {
        grid-column: 1 / -1;
        width: 100%;
      }
      .b2b-client-track {
        flex: 1;
        width: auto;
      }
    }

    @media (max-width: 640px) {
      .profit-hero-card,
      .b2b-hero-card,
      .profit-final-row {
        grid-template-columns: 1fr;
      }
      .profit-flow-row {
        grid-template-columns: 1fr;
        gap: 6px;
        padding: 8px 0;
      }
      .profit-flow-value,
      .profit-flow-pct,
      .profit-final-value {
        text-align: start;
      }
    }
  `;
  document.head.appendChild(style);
}

function setTheme(theme) {
  if (window.__appTheme) {
    window.__appTheme.set(theme);
    return;
  }
  document.documentElement.dataset.theme = theme;
  document.documentElement.setAttribute("data-theme", theme);
  document.body.dataset.theme = theme;
  document.body.setAttribute("data-theme", theme);
  document.body.classList.toggle("light", theme === "light");
  localStorage.setItem("colorMode", theme);
  const btn = document.getElementById("mode-btn");
  if (btn) btn.innerHTML = theme === "light" ? "&#9728;&#65039;" : "&#127769;";
  if (salesChart) salesChart.update("none");
}

function toggleTheme() {
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}

function initTheme() {
  if (window.__appTheme) {
    window.__appTheme.sync();
    return;
  }
  setTheme(localStorage.getItem("colorMode") || "dark");
}

function refreshThemeUi() {
  const theme = window.__appTheme ? window.__appTheme.get() : (document.documentElement.dataset.theme || "dark");
  const btn = document.getElementById("mode-btn");
  if (btn) btn.innerHTML = theme === "light" ? "&#9728;&#65039;" : "&#127769;";
  if (salesChart && dashboardData) renderChart();
}

function readCssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function getChartPalette() {
  const themePalette = window.__appTheme?.palette?.() || window.__appThemePalette || {};
  return {
    primary: themePalette.chartPrimary || themePalette.accent || readCssVar("--accent", "#00E5FF"),
    secondary: themePalette.chartSecondary || readCssVar("--blue", "#38bdf8"),
    negative: themePalette.chartNegative || readCssVar("--negative", "#f87171"),
    text: themePalette.textSub || readCssVar("--text-sub", "#cbd5e1"),
    muted: themePalette.textMuted || readCssVar("--text-muted", "#94A3B8"),
    border: themePalette.border || readCssVar("--border", "#334155"),
    surface: themePalette.surface || readCssVar("--surface", "#1E293B"),
  };
}

function updateRangeButtons() {
  document.querySelectorAll(".range-btn").forEach((button) => {
    button.classList.toggle("active", button.dataset.range === currentRange);
  });
}

function openCustomRangePicker() {
  document.getElementById("custom-range-modal")?.classList.remove("hidden");
  const startInput = document.getElementById("custom-range-start");
  const endInput = document.getElementById("custom-range-end");
  if (startInput) startInput.value = customStart || "";
  if (endInput) endInput.value = customEnd || "";
  setCustomRangeError("");
}

function closeCustomRangePicker() {
  document.getElementById("custom-range-modal")?.classList.add("hidden");
  setCustomRangeError("");
}

function setCustomRangeError(message) {
  const error = document.getElementById("custom-range-error");
  if (!error) return;
  error.hidden = !message;
  error.textContent = message;
}

function applyCustomRange() {
  const start = document.getElementById("custom-range-start")?.value;
  const end = document.getElementById("custom-range-end")?.value;
  if (!start || !end) {
    setCustomRangeError("Choose both dates.");
    return;
  }
  if (start > end) {
    setCustomRangeError("Start date must come first.");
    return;
  }
  customStart = start;
  customEnd = end;
  currentRange = "custom";
  localStorage.setItem("dashboard:range", currentRange);
  updateRangeButtons();
  closeCustomRangePicker();
  loadDashboard();
}

function markUpdated(stale = false) {
  clearInterval(elapsedTimer);
  lastUpdatedAt = Date.now();
  const node = document.getElementById("last-updated");
  if (node) {
    node.classList.remove("last-updated-error");
    node.classList.toggle("last-updated-stale", stale);
  }
  const tick = () => {
    if (!node) return;
    if (dashboardIsStale) {
      node.textContent = "cached · refreshing…";
      return;
    }
    const seconds = Math.max(0, Math.round((Date.now() - lastUpdatedAt) / 1000));
    node.textContent = seconds < 10 ? "just now" : `Updated ${seconds}s ago`;
  };
  tick();
  elapsedTimer = setInterval(tick, 5000);
}

function numberDeltaText(_metric, data) {
  if (data?.delta_pct === null || data?.delta_pct === undefined) return "No comparison yet";
  const rounded = Math.abs(Number(data.delta_pct)).toFixed(1).replace(".0", "");
  const direction = Number(data.delta_pct) >= 0 ? "up" : "down";
  return `${direction === "up" ? "↑" : "↓"} ${rounded}% vs last period`;
}

function tooltipForCard(key) {
  const tips = {
    sales: "Total money coming in from completed sales, after refunds. Does not include unpaid invoices.",
    clients_owe: "B2B clients with unpaid or partially-paid invoices. The overdue number counts those more than 30 days old.",
    spent: "All recorded expenses for the period - electricity, rent, supplies, salaries, and more.",
    stock_alerts: "Products that are out of stock or nearly out.",
    sales_today: "Money taken by the current cashier today.",
  };
  return tips[key] || "";
}

function cardSpec(key) {
  const rangeLabel = dashboardData?.range?.label || "this period";
  if (key === "sales") {
    return {
      label: dashboardData?.range?.label === "Today" ? "Sales today" : `Sales ${rangeLabel.toLowerCase()}`,
      value: formatMoney(dashboardData?.numbers?.sales?.value || 0),
      meta: numberDeltaText("sales", dashboardData?.numbers?.sales),
      sparkline: dashboardData?.numbers?.sales?.sparkline || [],
      tooltip: tooltipForCard("sales"),
    };
  }
  if (key === "clients_owe" && !(dashboardData?.viewer?.can_view_b2b)) {
    return {
      label: "Sales today",
      value: formatMoney(dashboardData?.viewer?.alt_sales_today?.value || 0),
      meta: "Your shift total so far",
      sparkline: [],
      tooltip: tooltipForCard("sales_today"),
    };
  }
  if (key === "clients_owe") {
    return {
      label: "Money clients owe you",
      value: formatMoney(dashboardData?.numbers?.clients_owe?.value || 0),
      meta: `${formatNumber(dashboardData?.numbers?.clients_owe?.overdue_count || 0)} overdue`,
      sparkline: [],
      tooltip: tooltipForCard("clients_owe"),
    };
  }
  if (key === "spent") {
    return {
      label: dashboardData?.range?.label === "Today" ? "Money you've spent today" : `Money you've spent ${rangeLabel.toLowerCase()}`,
      value: formatMoney(dashboardData?.numbers?.spent?.value || 0),
      meta: numberDeltaText("spent", dashboardData?.numbers?.spent),
      sparkline: dashboardData?.numbers?.spent?.sparkline || [],
      tooltip: tooltipForCard("spent"),
    };
  }
  if (key === "b2b_cash") {
    const val = dashboardData?.numbers?.b2b_cash?.value || 0;
    const periodLabel = currentRange === "today" ? "today" : rangeLabel.toLowerCase();
    return {
      label: `B2B cash collected ${periodLabel}`,
      value: formatMoney(val),
      meta: "",
      sparkline: [],
      tooltip: "Total cash actually collected from B2B clients (payments received on invoices).",
    };
  }
  return {
    label: "Stock alerts",
    value: `${formatNumber(dashboardData?.numbers?.stock_alerts?.value || 0)} items`,
    meta: `${formatNumber(dashboardData?.numbers?.stock_alerts?.out_count || 0)} out · ${formatNumber(dashboardData?.numbers?.stock_alerts?.low_count || 0)} low`,
    sparkline: [],
    tooltip: tooltipForCard("stock_alerts"),
  };
}

function sparklineBars(values) {
  if (!values?.length) return "";
  const max = Math.max(...values, 1);
  return values.map((value) => `<span style="height:${Math.max(6, Math.round((value / max) * 40))}px"></span>`).join("");
}

function renderNumbers() {
  ["sales", "clients_owe", "b2b_cash", "spent", "stock_alerts"].forEach((key) => {
    const node = document.querySelector(`[data-card="${key}"]`);
    if (!node) return;
    const spec = cardSpec(key);
    let btn = node.querySelector(".number-card-button");
    if (!btn) {
      node.innerHTML = `
        <div class="number-card-button">
          <span class="number-label"></span>
          <strong class="number-value"></strong>
          <span class="number-meta"></span>
          <div class="number-extra"></div>
        </div>`;
      btn = node.querySelector(".number-card-button");
    }
    btn.dataset.tooltip = spec.tooltip || "";
    btn.querySelector(".number-label").textContent = spec.label || "";
    btn.querySelector(".number-value").textContent = spec.value || "";
    btn.querySelector(".number-meta").textContent = spec.meta || "";
    const extra = btn.querySelector(".number-extra");
    if (extra) {
      if (spec.sparkline && spec.sparkline.length) {
        extra.className = "number-extra sparkline-bars";
        extra.innerHTML = sparklineBars(spec.sparkline);
      } else {
        extra.className = "number-extra";
        extra.textContent = "";
      }
    }
  });
}

function renderBriefing() {
  const briefing = dashboardData?.briefing || {};
  setText("briefing-lead", briefing.lead || "You haven't recorded any sales yet for this period.");
  setText("briefing-body", briefing.body || "");
  const actionsNode = document.getElementById("briefing-actions");
  const actions = briefing.actions || [];
  const html = actions.map((action) => (
    `<a class="briefing-action" href="${escHtml(action.link)}"><span>${escHtml(action.text)}</span><strong>${escHtml(action.cta)} →</strong></a>`
  )).join("");
  setHTML(actionsNode, html);
}

function topProductsTitle() {
  const label = dashboardData?.range?.label || "This period";
  return `Best-sellers ${label.toLowerCase()}`;
}

function renderTopProducts() {
  setText("top-products-title", topProductsTitle());
  const key = topProductsTab === "revenue" ? "top_products_by_revenue" : "top_products_by_qty";
  const products = dashboardData?.panels?.[key] || [];
  const maxValue = Math.max(...products.map((p) => topProductsTab === "revenue" ? Number(p.revenue || 0) : Number(p.qty || 0)), 1);
  const container = document.getElementById("top-products-list");
  const html = !products.length
    ? `<div class="empty-state">No products sold in this range.</div>`
    : products.map((product) => {
        const value = topProductsTab === "revenue" ? Number(product.revenue || 0) : Number(product.qty || 0);
        const label = topProductsTab === "revenue" ? formatMoney(value) : `${formatNumber(value)} units`;
        const width = Math.max(8, Math.round((value / maxValue) * 100));
        return `<div class="list-row top-product-row"><div class="row-main"><span class="row-title">${escHtml(product.name)}</span><span class="row-value">${escHtml(label)}</span></div><span class="row-bar"><span style="width:${width}%"></span></span></div>`;
      }).join("");
  setHTML(container, html);
}

function renderRecentActivity() {
  const rows = (dashboardData?.panels?.recent_activity || []).filter((item) => activityFilter === "all" ? true : item.type === activityFilter);
  const tbody = document.getElementById("recent-activity");
  if (!tbody) return;
  const html = !rows.length
    ? `<tr><td colspan="4" class="empty-cell">No activity in this range.</td></tr>`
    : rows.map((item) => `
        <tr data-link="${escHtml(item.link || "#")}">
          <td class="mono">${escHtml(item.invoice_number || "-")}</td>
          <td>${escHtml(item.customer || "-")}</td>
          <td class="${item.type === "refund" ? "negative" : "positive"}">${escHtml(item.type === "refund" ? `-${formatMoney(Math.abs(item.total || 0))}` : formatMoney(item.total || 0))}</td>
          <td>${escHtml(item.time_relative || "-")}</td>
        </tr>`).join("");
  if (tbody.innerHTML !== html) {
    tbody.innerHTML = html;
    tbody.querySelectorAll("tr[data-link]").forEach((row) => {
      row.addEventListener("click", () => {
        const link = row.dataset.link;
        if (link && link !== "#") window.location.assign(link);
      });
    });
  }
}

function chartTitle() {
  const label = dashboardData?.range?.label || "This period";
  return `Sales over time — ${label}`;
}

function chartLabels(buckets) {
  const granularity = dashboardData?.range?.granularity || "day";
  return buckets.map((bucket) => {
    const date = new Date(`${bucket.date}T12:00:00`);
    if (granularity === "month") return date.toLocaleDateString("en-GB", { month: "short", year: "numeric" });
    if (granularity === "week") return `Week of ${date.toLocaleDateString("en-GB", { day: "numeric", month: "short" })}`;
    return date.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  });
}

function renderChart() {
  const buckets = dashboardData?.chart?.buckets || [];
  setText("chart-title", chartTitle());
  setHTML(document.getElementById("chart-table"), `
    <tr><th>Date</th><th>POS</th><th>B2B</th><th>Refunds</th><th>Orders</th></tr>
    ${buckets.map((bucket) => `<tr><td>${bucket.date}</td><td>${formatMoneyPrecise(bucket.pos)}</td><td>${formatMoneyPrecise(bucket.b2b)}</td><td>${formatMoneyPrecise(bucket.refunds)}</td><td>${bucket.orders}</td></tr>`).join("")}
  `);

  if (typeof Chart === "undefined") return;

  const chartPalette = getChartPalette();
  const chartData = {
    labels: chartLabels(buckets),
    datasets: [
      { label: "POS", data: buckets.map((b) => b.pos), backgroundColor: chartPalette.primary, stack: "sales" },
      { label: "B2B", data: buckets.map((b) => b.b2b), backgroundColor: chartPalette.secondary, stack: "sales" },
      { label: "Refunds", data: buckets.map((b) => b.refunds), backgroundColor: chartPalette.negative, stack: "sales" },
    ],
  };
  const tooltipAfterBody = (items) => {
    const bucket = buckets[items[0]?.dataIndex || 0];
    return [`Transactions: ${bucket?.orders || 0}`];
  };

  if (!salesChart) {
    salesChart = new Chart(document.getElementById("sales-chart"), {
      type: "bar",
      data: chartData,
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: true,
            position: "top",
            align: "end",
            labels: { color: chartPalette.text },
          },
          tooltip: {
            backgroundColor: chartPalette.surface,
            borderColor: chartPalette.border,
            borderWidth: 1,
            titleColor: chartPalette.text,
            bodyColor: chartPalette.text,
            callbacks: { afterBody: tooltipAfterBody },
          },
        },
        scales: {
          x: {
            stacked: true,
            ticks: { color: chartPalette.muted },
            grid: { display: false },
            border: { color: chartPalette.border },
          },
          y: {
            stacked: true,
            grid: { color: chartPalette.border },
            ticks: { display: false, color: chartPalette.muted },
            border: { display: false },
          },
        },
      },
    });
    return;
  }

  salesChart.data.labels = chartData.labels;
  salesChart.data.datasets.forEach((dataset, i) => {
    if (chartData.datasets[i]) {
      dataset.data = chartData.datasets[i].data;
      dataset.backgroundColor = chartData.datasets[i].backgroundColor;
    }
  });
  salesChart.options.plugins.legend.labels.color = chartPalette.text;
  salesChart.options.plugins.tooltip.backgroundColor = chartPalette.surface;
  salesChart.options.plugins.tooltip.borderColor = chartPalette.border;
  salesChart.options.plugins.tooltip.titleColor = chartPalette.text;
  salesChart.options.plugins.tooltip.bodyColor = chartPalette.text;
  salesChart.options.scales.x.ticks.color = chartPalette.muted;
  salesChart.options.scales.x.border.color = chartPalette.border;
  salesChart.options.scales.y.grid.color = chartPalette.border;
  salesChart.options.scales.y.ticks.color = chartPalette.muted;
  salesChart.options.plugins.tooltip.callbacks.afterBody = tooltipAfterBody;
  salesChart.update("none");
}

function renderProfitSummary() {
  const el = document.getElementById("profit-summary");
  if (!el) return;

  const profit = dashboardData?.numbers?.profit;
  const revenue = Number(dashboardData?.numbers?.sales?.value || 0);
  if (!profit || profit.gross_profit === null || profit.gross_profit === undefined) {
    el.innerHTML = `
      <div class="dashboard-upgrade-empty">
        <strong>Profit data is not ready yet.</strong>
        <span>Add product costs and keep sales/expenses updated to unlock gross profit, net profit, and margin tracking.</span>
      </div>`;
    return;
  }

  const grossProfit = Number(profit.gross_profit || 0);
  const operatingExpenses = Number(profit.operating_expenses || 0);
  const netProfit = Number(profit.net_profit || 0);
  const cogs = Math.max(0, revenue - grossProfit);
  const grossMarginPct = profit.gross_margin_pct;
  const netMarginPct = profit.net_margin_pct;
  const cogsPct = revenue > 0 ? (cogs / revenue) * 100 : 0;
  const opexPct = revenue > 0 ? (operatingExpenses / revenue) * 100 : 0;
  const delta = profit.net_margin_delta_pts;

  let statusClass = "neutral";
  let statusText = "No comparison yet";
  if (delta !== null && delta !== undefined) {
    statusClass = Number(delta) >= 0 ? "up" : "down";
    statusText = `${Number(delta) >= 0 ? "+" : ""}${Number(delta).toFixed(1)} pp vs last period`;
  }

  const healthText = netProfit >= 0
    ? "Your selected period is profitable after operating expenses."
    : "Your selected period is running at a net loss after operating expenses.";

  el.innerHTML = `
    <div class="profit-summary-shell">
      <div class="profit-hero-card">
        <div>
          <span class="profit-hero-label">Net profit</span>
          <strong class="profit-hero-value ${netProfit >= 0 ? "positive" : "negative"}">${signedMoney(netProfit)}</strong>
          <p class="profit-hero-sub">${escHtml(healthText)}</p>
        </div>
        <span class="profit-status-badge ${statusClass}">${escHtml(statusText)}</span>
      </div>

      <div class="profit-mini-grid" aria-label="Profit key metrics">
        <div class="mini-kpi">
          <span class="mini-kpi-label">Gross profit</span>
          <strong class="mini-kpi-value ${grossProfit >= 0 ? "positive" : "negative"}">${signedMoney(grossProfit)}</strong>
        </div>
        <div class="mini-kpi">
          <span class="mini-kpi-label">Gross margin</span>
          <strong class="mini-kpi-value">${percentText(grossMarginPct)}</strong>
        </div>
        <div class="mini-kpi">
          <span class="mini-kpi-label">Net margin</span>
          <strong class="mini-kpi-value ${netProfit >= 0 ? "positive" : "negative"}">${percentText(netMarginPct)}</strong>
        </div>
      </div>

      <div class="profit-waterfall" aria-label="Profit breakdown">
        ${profitFlowRow("Revenue", revenue, 100, "revenue", 100)}
        ${profitFlowRow("Cost of goods", cogs, cogsPct, "cogs", ratioOf(cogs, revenue), true)}
        ${profitFlowRow("Gross profit", grossProfit, grossMarginPct, "gross", ratioOf(grossProfit, revenue))}
        ${profitFlowRow("Operating expenses", operatingExpenses, opexPct, "opex", ratioOf(operatingExpenses, revenue), true)}
        ${profitFlowRow("Net profit", netProfit, netMarginPct, `net ${netProfit < 0 ? "negative" : ""}`, ratioOf(netProfit, revenue))}
      </div>

      <div class="profit-final-row">
        <div>
          <span class="profit-final-title">Operating result</span>
          <span class="profit-final-sub">Revenue minus product costs and operating expenses</span>
        </div>
        <strong class="profit-final-value ${netProfit >= 0 ? "positive" : "negative"}">${signedMoney(netProfit)}</strong>
      </div>
    </div>`;
}

function profitFlowRow(label, amount, pct, colorClass, width, negative = false) {
  const amountClass = negative || amount < 0 ? "negative" : "positive";
  const sign = negative && amount > 0 ? "-" : "";
  return `
    <div class="profit-flow-row">
      <span class="profit-flow-label">${escHtml(label)}</span>
      <span class="profit-track"><span class="profit-fill ${escHtml(colorClass)}" style="width:${Math.max(4, Math.min(100, width))}%"></span></span>
      <span class="profit-flow-value ${amountClass}">${sign}${signedMoney(amount)}</span>
      <span class="profit-flow-pct">${percentText(pct)}</span>
    </div>`;
}

function renderTopB2BClients() {
  const el = document.getElementById("top-b2b-list");
  if (!el) return;

  const clients = dashboardData?.panels?.top_b2b_clients || [];
  if (!clients.length) {
    el.innerHTML = `
      <div class="dashboard-upgrade-empty">
        <strong>No B2B clients in this range.</strong>
        <span>Paid B2B invoices will appear here with revenue, invoice count, and outstanding balance.</span>
      </div>`;
    return;
  }

  const sorted = [...clients].sort((a, b) => {
    if (b2bClientsTab === "invoices") return Number(b.invoice_count || 0) - Number(a.invoice_count || 0);
    if (b2bClientsTab === "outstanding") return Number(b.outstanding || 0) - Number(a.outstanding || 0);
    return Number(b.revenue || 0) - Number(a.revenue || 0);
  });

  const topClient = sorted[0];
  const totalRevenue = sorted.reduce((sum, client) => sum + Number(client.revenue || 0), 0);
  const totalOutstanding = sorted.reduce((sum, client) => sum + Number(client.outstanding || 0), 0);
  const totalInvoices = sorted.reduce((sum, client) => sum + Number(client.invoice_count || 0), 0);
  const clientsWithOutstanding = sorted.filter((client) => Number(client.outstanding || 0) > 0).length;
  const valueForTab = (client) => (
    b2bClientsTab === "invoices" ? Number(client.invoice_count || 0) :
    b2bClientsTab === "outstanding" ? Number(client.outstanding || 0) :
    Number(client.revenue || 0)
  );
  const maxValue = Math.max(...sorted.map(valueForTab), 1);
  const heroValue = b2bClientsTab === "invoices" ? `${formatNumber(valueForTab(topClient))} invoices` : formatMoney(valueForTab(topClient));

  el.innerHTML = `
    <div class="b2b-summary-shell">
      <div class="b2b-hero-card">
        <div>
          <span class="b2b-hero-label">Leading client</span>
          <strong class="b2b-hero-value">${escHtml(topClient.name || "—")}</strong>
          <p class="b2b-hero-sub">${escHtml(heroValue)} · ${formatNumber(topClient.invoice_count || 0)} invoice${Number(topClient.invoice_count || 0) === 1 ? "" : "s"} · ${escHtml(topClient.payment_terms || "immediate")}</p>
        </div>
        <span class="b2b-status-badge ${clientsWithOutstanding ? "neutral" : "good"}">${clientsWithOutstanding ? `${clientsWithOutstanding} with balance` : "Clean balances"}</span>
      </div>

      <div class="b2b-stat-grid" aria-label="Top B2B client summary">
        <div class="b2b-stat-card">
          <span class="b2b-stat-label">Top revenue</span>
          <strong class="b2b-stat-value positive">${formatMoney(totalRevenue)}</strong>
        </div>
        <div class="b2b-stat-card">
          <span class="b2b-stat-label">Invoices</span>
          <strong class="b2b-stat-value">${formatNumber(totalInvoices)}</strong>
        </div>
        <div class="b2b-stat-card">
          <span class="b2b-stat-label">Outstanding</span>
          <strong class="b2b-stat-value ${totalOutstanding > 0 ? "negative" : "positive"}">${formatMoney(totalOutstanding)}</strong>
        </div>
      </div>

      <div class="b2b-client-list">
        ${sorted.map((client, index) => b2bClientRow(client, index, valueForTab(client), maxValue)).join("")}
      </div>
    </div>`;
}

function b2bClientRow(client, index, value, maxValue) {
  const colors = ["primary", "success", "warning", "rose", "secondary"];
  const initials = String(client.name || "?")
    .split(" ")
    .filter(Boolean)
    .map((word) => word[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  const outstanding = Number(client.outstanding || 0);
  const width = Math.max(7, Math.round((Number(value || 0) / Math.max(maxValue, 1)) * 100));
  const displayedValue = b2bClientsTab === "invoices" ? `${formatNumber(client.invoice_count || 0)} invoices` : formatMoney(value);
  const balancePill = outstanding > 0
    ? `<span class="b2b-debt-pill">Owes ${formatMoney(outstanding)}</span>`
    : `<span class="b2b-clean-pill">Paid</span>`;

  return `
    <article class="b2b-client-card">
      <span class="b2b-rank-chip">${index + 1}</span>
      <span class="b2b-avatar b2b-avatar-${colors[index % colors.length]}">${escHtml(initials)}</span>
      <div class="b2b-main">
        <div class="b2b-name-line">
          <span class="b2b-name">${escHtml(client.name || "Unknown client")}</span>
          ${balancePill}
        </div>
        <div class="b2b-meta">
          <span>${formatNumber(client.invoice_count || 0)} invoice${Number(client.invoice_count || 0) === 1 ? "" : "s"}</span>
          <span>${escHtml(client.payment_terms || "immediate")}</span>
          <span>${formatMoney(client.revenue || 0)} revenue</span>
        </div>
      </div>
      <div class="b2b-client-meter" aria-label="${escHtml(displayedValue)}">
        <span class="b2b-client-track"><span class="b2b-client-fill ${b2bClientsTab === "outstanding" ? "warning" : ""}" style="width:${width}%"></span></span>
        <strong class="b2b-row-money ${b2bClientsTab === "outstanding" && outstanding > 0 ? "negative" : ""}">${escHtml(displayedValue)}</strong>
      </div>
    </article>`;
}

const SECTION_LABELS = {
  "numbers": "Summary numbers",
  "numbers.margin": "Profit margin",
  "numbers.b2b_cash": "B2B cash collected",
  "chart": "Sales chart",
  "top_products": "Best-sellers",
  "briefing": "Daily briefing",
  "insights": "Insights",
  "insights.overdue": "Overdue insight",
  "insights.stockout": "Stock insight",
  "insights.pace": "Sales pace insight",
  "insights.margin": "Margin insight",
  "insights.weekday": "Day-of-week insight",
};

function renderErrorBanner() {
  const el = document.getElementById("error-banner");
  if (!el) return;

  const errors = dashboardData?._errors;
  if (!errors || errors.length === 0 || errorBannerDismissed) {
    el.innerHTML = "";
    el.classList.remove("error-banner-visible");
    return;
  }

  const labels = errors.map((e) => SECTION_LABELS[e.section] || e.section);
  const unique = [...new Set(labels)];

  let html;
  if (unique.length === 1) {
    html = `
      <div class="error-banner error-banner-single" role="alert" aria-live="polite">
        <span class="error-banner-icon" aria-hidden="true">⚠</span>
        <span class="error-banner-text">${escHtml(unique[0])} couldn't load — other sections are unaffected.</span>
        <button class="error-banner-close" aria-label="Dismiss warning" onclick="dismissErrorBanner()">✕</button>
      </div>`;
  } else {
    const chips = unique.map((l) => `<span class="error-banner-chip">${escHtml(l)}</span>`).join("");
    html = `
      <div class="error-banner error-banner-multi" role="alert" aria-live="polite">
        <span class="error-banner-icon" aria-hidden="true">⚠</span>
        <div class="error-banner-body">
          <span class="error-banner-title">Some data couldn't load</span>
          <span class="error-banner-detail">The figures below may be incomplete. Other sections loaded normally.</span>
          <div class="error-banner-chips">${chips}</div>
        </div>
        <button class="error-banner-close" aria-label="Dismiss warning" onclick="dismissErrorBanner()">✕</button>
      </div>`;
  }

  el.innerHTML = html;
  el.classList.add("error-banner-visible");
}

function dismissErrorBanner() {
  errorBannerDismissed = true;
  const el = document.getElementById("error-banner");
  if (!el) return;
  el.classList.add("error-banner-hiding");
  setTimeout(() => {
    el.innerHTML = "";
    el.classList.remove("error-banner-visible", "error-banner-hiding");
  }, 250);
}

function renderAll() {
  try { renderBriefing(); } catch (e) { console.error("renderBriefing", e); }
  try { renderNumbers(); } catch (e) { console.error("renderNumbers", e); }
  try { renderChart(); } catch (e) { console.error("renderChart", e); }
  try { renderTopProducts(); } catch (e) { console.error("renderTopProducts", e); }
  try { renderRecentActivity(); } catch (e) { console.error("renderRecentActivity", e); }
  try { renderProfitSummary(); } catch (e) { console.error("renderProfitSummary", e); }
  try { renderTopB2BClients(); } catch (e) { console.error("renderTopB2BClients", e); }
  try { renderErrorBanner(); } catch (e) { console.error("renderErrorBanner", e); }
}

function swrKey(range) {
  return `dash:swr:${range}`;
}

function swrRead(range) {
  try {
    const raw = localStorage.getItem(swrKey(range));
    if (!raw) return null;
    const { ts, data } = JSON.parse(raw);
    if (Date.now() - ts > SWR_MAX_AGE_MS) return null;
    return data;
  } catch {
    return null;
  }
}

function swrWrite(range, data) {
  try {
    localStorage.setItem(swrKey(range), JSON.stringify({ ts: Date.now(), data }));
  } catch {}
}

function showErrorState(message) {
  if (dashboardHasLoaded) {
    const node = document.getElementById("last-updated");
    if (node) {
      node.textContent = "Refresh failed — retrying";
      node.classList.add("last-updated-error");
    }
    return;
  }
  const loading = document.getElementById("loading");
  if (loading) {
    loading.classList.remove("hidden");
    loading.innerHTML = `<div class="load-error">${escHtml(message)}</div>`;
  }
}

async function loadDashboard() {
  if (currentRange === "custom" && (!customStart || !customEnd)) {
    currentRange = "mtd";
    localStorage.setItem("dashboard:range", currentRange);
    updateRangeButtons();
  }

  if (dashboardAbortController) dashboardAbortController.abort();
  dashboardAbortController = new AbortController();
  const requestId = ++dashboardRequestId;

  const rangeKey = currentRange === "custom" ? `custom:${customStart}:${customEnd}` : currentRange;
  const cached = swrRead(rangeKey);
  if (cached && !dashboardHasLoaded) {
    dashboardData = cached;
    dashboardIsStale = true;
    document.getElementById("loading")?.classList.add("hidden");
    dashboardHasLoaded = true;
    renderAll();
    markUpdated(true);
  }

  let url = `/dashboard/summary?range=${currentRange}&_=${Date.now()}`;
  if (currentRange === "custom" && customStart && customEnd) {
    url += `&start=${customStart}&end=${customEnd}`;
  }

  try {
    const response = await fetch(url, {
      credentials: "same-origin",
      signal: dashboardAbortController.signal,
    });
    if (!response.ok) throw new Error(`Dashboard request failed (${response.status})`);
    const nextData = await response.json();
    if (requestId !== dashboardRequestId) return;
    dashboardData = nextData;
    dashboardIsStale = false;
    if (!dashboardHasLoaded) {
      document.getElementById("loading")?.classList.add("hidden");
      dashboardHasLoaded = true;
    }
    errorBannerDismissed = false;
    renderAll();
    markUpdated(false);
    swrWrite(rangeKey, nextData);
  } catch (error) {
    if (error.name === "AbortError") return;
    if (dashboardHasLoaded) {
      dashboardIsStale = false;
      const node = document.getElementById("last-updated");
      if (node) {
        node.classList.add("last-updated-error");
        node.textContent = "refresh failed";
      }
      clearInterval(elapsedTimer);
      return;
    }
    showErrorState(error.message);
  }
}

async function initUser() {
  try {
    const response = await fetch("/auth/me");
    if (response.ok) currentUser = await response.json();
  } catch {}

  const name = currentUser?.name || "Admin";
  const email = currentUser?.email || "-";
  const avatar = (name.trim()[0] || "A").toUpperCase();
  setText("user-name", name);
  setText("user-email", email);
  setText("user-avatar", avatar);
  setGreeting();
}

function bindAccountMenuFallback() {
  const trigger = document.getElementById("account-trigger");
  const dropdown = document.getElementById("account-dropdown");
  const signout = document.getElementById("signout-btn");
  if (!trigger || !dropdown) return;

  trigger.addEventListener("click", (event) => {
    event.stopPropagation();
    const open = dropdown.classList.toggle("open");
    trigger.classList.toggle("open", open);
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
  });

  if (signout) {
    signout.addEventListener("click", async () => {
      await fetch("/auth/logout", { method: "POST" });
      window.location.href = "/";
    });
  }

  document.addEventListener("click", (event) => {
    if (dropdown.contains(event.target) || trigger.contains(event.target)) return;
    dropdown.classList.remove("open");
    trigger.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
  });
}

function bindEvents() {
  if (!window.__appNav) {
    document.getElementById("mode-btn")?.addEventListener("click", toggleTheme);
  }
  window.addEventListener("app:themechange", refreshThemeUi);

  if (!window.__appNav) bindAccountMenuFallback();

  document.querySelectorAll(".range-btn").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.range === "custom") {
        openCustomRangePicker();
        return;
      }
      currentRange = button.dataset.range;
      localStorage.setItem("dashboard:range", currentRange);
      updateRangeButtons();
      loadDashboard();
    });
  });

  document.getElementById("range-modal-close")?.addEventListener("click", closeCustomRangePicker);
  document.getElementById("range-cancel")?.addEventListener("click", closeCustomRangePicker);
  document.getElementById("range-apply")?.addEventListener("click", applyCustomRange);
  document.getElementById("custom-range-modal")?.addEventListener("click", (event) => {
    if (event.target.id === "custom-range-modal") closeCustomRangePicker();
  });

  document.querySelectorAll("[data-top-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      topProductsTab = button.dataset.topTab;
      document.querySelectorAll("[data-top-tab]").forEach((item) => item.classList.toggle("active", item === button));
      renderTopProducts();
    });
  });

  document.querySelectorAll("[data-activity-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      activityFilter = button.dataset.activityFilter;
      document.querySelectorAll("[data-activity-filter]").forEach((item) => item.classList.toggle("active", item === button));
      renderRecentActivity();
    });
  });

  document.querySelectorAll("[data-b2b-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      b2bClientsTab = button.dataset.b2bTab;
      document.querySelectorAll("[data-b2b-tab]").forEach((item) => item.classList.toggle("active", item === button));
      renderTopB2BClients();
    });
  });
}

function startAutoRefresh() {
  refreshTimer = setInterval(() => {
    if (!document.hidden) loadDashboard();
  }, 60000);
}

async function initDashboard() {
  injectDashboardUpgradeStyles();
  initTheme();
  refreshThemeUi();
  updateRangeButtons();
  bindEvents();
  await initUser();
  await loadDashboard();
  startAutoRefresh();
}

window.addEventListener("load", initDashboard);
