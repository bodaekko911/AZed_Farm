/* ───────────────────────────────────────────────────────────────────
   Farm Dashboard — front-end controller.

   Talks to:   GET /farm-dashboard/summary?range=...&start=...&end=...
   Renders:    number cards, season chart, leaderboards, signals.
   Reuses the dashboard.css visual language for parity with the Sales
   dashboard.
   ─────────────────────────────────────────────────────────────────── */

(function () {
  "use strict";

  const FMT_INT   = new Intl.NumberFormat("en-GB");
  const FMT_QTY   = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 2 });
  const FMT_MONEY = new Intl.NumberFormat("en-GB", {
    style: "currency", currency: "EGP", maximumFractionDigits: 0,
  });

  const state = {
    range:  "30d",
    start:  null,
    end:    null,
    data:   null,
    activeFarmTab: "value",
    activeSpoilageTab: "reasons",
    activeExpTab: "category",
    chart:  null,
  };

  // ── helpers ─────────────────────────────────────────────────────
  function $(sel) { return document.querySelector(sel); }
  function $id(id) { return document.getElementById(id); }
  function setText(id, val) { const n = $id(id); if (n) n.textContent = val; }
  function fmtMoney(n) { return FMT_MONEY.format(Math.round(Number(n) || 0)); }
  function fmtQty(n)   { return FMT_QTY.format(Number(n) || 0); }
  function fmtInt(n)   { return FMT_INT.format(Math.round(Number(n) || 0)); }

  function fmtDelta(pct) {
    if (pct === null || pct === undefined) return "—";
    const sign = pct > 0 ? "▲" : pct < 0 ? "▼" : "•";
    return `${sign} ${Math.abs(pct).toFixed(1)}%`;
  }
  function deltaClass(pct, invert) {
    if (pct === null || pct === undefined || pct === 0) return "muted";
    const positive = invert ? pct < 0 : pct > 0;
    return positive ? "pos" : "neg";
  }

  function longDateLabel() {
    const opts = { weekday: "long", year: "numeric", month: "long", day: "numeric" };
    return new Date().toLocaleDateString("en-GB", opts);
  }

  // ── header strip ────────────────────────────────────────────────
  function paintHeader() {
    setText("date-display", longDateLabel());
    const n = $id("last-updated");
    if (n) {
      n.textContent = "Updated " + new Date().toLocaleTimeString("en-GB", {
        hour: "2-digit", minute: "2-digit",
      });
      n.classList.remove("last-updated-stale", "last-updated-error");
    }
  }

  // ── number cards ────────────────────────────────────────────────
  function renderNumberCards(d) {
    const dl = d.deliveries || {};
    const sp = d.spoilage   || {};
    const ex = d.expenses   || {};
    const cad = d.cadence   || {};

    const cards = {
      deliveries: {
        label: "Deliveries",
        big: fmtInt(dl.count || 0),
        sub: `${cad.deliveries_per_day || 0}/day · ${cad.active_days || 0} active days`,
        delta: dl.count_delta,
      },
      intake_qty: {
        label: "Intake quantity",
        big:   fmtQty(dl.qty || 0),
        sub: `${fmtQty(cad.qty_per_day || 0)} avg per day`,
        delta: dl.qty_delta,
      },
      intake_value: {
        label: "Intake value",
        big: fmtMoney(dl.value || 0),
        sub: `Estimated at product cost`,
        delta: dl.value_delta,
      },
      spoilage: {
        label: "Spoilage",
        big: fmtQty(sp.qty || 0),
        sub: `${sp.rate_pct || 0}% rate · ${fmtMoney(sp.value || 0)} lost`,
        delta: sp.qty_delta,
        invert: true,
      },
      farm_expenses: {
        label: "Farm expenses",
        big: fmtMoney(ex.farm_total || 0),
        sub: `${ex.farm_share_pct || 0}% of company spend (${fmtMoney(ex.company_total || 0)})`,
        delta: ex.farm_delta,
        invert: true,
      },
    };

    Object.keys(cards).forEach((key) => {
      const card = document.querySelector(`[data-card="${key}"]`);
      if (!card) return;
      const c = cards[key];
      const cls = deltaClass(c.delta, c.invert);
      card.innerHTML = `
        <div class="number-label">${c.label}</div>
        <div class="number-big">${c.big}</div>
        <div class="number-foot">
          <span class="number-sub">${c.sub}</span>
          <span class="number-delta ${cls}">${fmtDelta(c.delta)}</span>
        </div>
      `;
    });
  }

  // ── briefing card ───────────────────────────────────────────────
  function renderBriefing(d) {
    const dl = d.deliveries || {};
    const sp = d.spoilage   || {};
    const range = d.range || {};
    const sx = d.season_peaks || {};

    let lead = `${range.label || ""} — ${fmtQty(dl.qty || 0)} units intaken across ${fmtInt(dl.count || 0)} deliveries.`;
    if (dl.qty_delta !== null && dl.qty_delta !== undefined) {
      lead += ` Intake is ${dl.qty_delta >= 0 ? "up" : "down"} ${Math.abs(dl.qty_delta).toFixed(1)}% vs the previous window.`;
    }

    const bodyParts = [];
    if (sp.rate_pct != null) bodyParts.push(`Spoilage running at ${sp.rate_pct}%.`);
    if (sx.peak_month)       bodyParts.push(`Strongest month in the last 12: ${sx.peak_month}.`);
    if ((d.dormant_count || 0) > 0) bodyParts.push(`${d.dormant_count} active farm(s) had no deliveries.`);

    setText("briefing-lead", lead);
    setText("briefing-body", bodyParts.join(" "));
  }

  // ── season chart ────────────────────────────────────────────────
  function renderSeasonChart(d) {
    const ctx = $id("season-chart");
    if (!ctx || !window.Chart) return;
    const season = d.season || [];
    const labels = season.map((m) => m.label);
    const intake = season.map((m) => m.qty);
    const spoil  = season.map((m) => m.spoilage);
    const exp    = season.map((m) => m.expenses);

    if (state.chart) {
      try { state.chart.destroy(); } catch (_) {}
    }
    state.chart = new Chart(ctx, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Intake qty",
            data: intake,
            backgroundColor: "rgba(74, 222, 128, .55)",
            borderColor: "#4ade80",
            borderWidth: 1,
            yAxisID: "y",
            order: 2,
          },
          {
            label: "Spoilage qty",
            data: spoil,
            backgroundColor: "rgba(248, 113, 113, .55)",
            borderColor: "#f87171",
            borderWidth: 1,
            yAxisID: "y",
            order: 1,
          },
          {
            label: "Farm expenses (EGP)",
            data: exp,
            type: "line",
            borderColor: "#38bdf8",
            backgroundColor: "rgba(56, 189, 248, .12)",
            tension: .35,
            yAxisID: "y1",
            order: 0,
            fill: true,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "bottom", labels: { boxWidth: 12, font: { size: 11 } } },
        },
        scales: {
          y:  { beginAtZero: true, position: "left",  title: { display: true, text: "Quantity" } },
          y1: { beginAtZero: true, position: "right", title: { display: true, text: "EGP" }, grid: { drawOnChartArea: false } },
        },
      },
    });

    // Accessible table for screen readers
    const table = $id("chart-table");
    if (table) {
      table.innerHTML =
        "<thead><tr><th>Month</th><th>Intake</th><th>Spoilage</th><th>Expenses</th></tr></thead>" +
        "<tbody>" + season.map((m) =>
          `<tr><td>${m.label}</td><td>${fmtQty(m.qty)}</td><td>${fmtQty(m.spoilage)}</td><td>${fmtMoney(m.expenses)}</td></tr>`
        ).join("") + "</tbody>";
    }
  }

  // ── top farms panel ─────────────────────────────────────────────
  function renderTopFarms() {
    const d = state.data;
    if (!d) return;
    const list = state.activeFarmTab === "qty" ? (d.top_farms_by_qty || []) : (d.top_farms_by_value || []);
    const target = $id("top-farms-list");
    if (!target) return;
    if (!list.length) {
      target.innerHTML = `<div class="empty-state">No farm deliveries in this window.</div>`;
      return;
    }
    target.innerHTML = list.map((row, i) => {
      const primary   = state.activeFarmTab === "qty"
        ? `<span>${fmtQty(row.qty)}<span class="unit">units</span></span>`
        : fmtMoney(row.value);
      const secondary = state.activeFarmTab === "qty"
        ? fmtMoney(row.value)
        : `<span>${fmtQty(row.qty)}<span class="unit">units</span></span>`;
      return `
        <div class="farm-row">
          <div class="row-rank">${i + 1}</div>
          <div>
            <div class="row-name">${escapeHtml(row.farm)}</div>
            <div class="row-sub">${fmtInt(row.deliveries || 0)} deliveries</div>
          </div>
          <div class="row-metric">${primary}</div>
          <div class="row-metric muted">${secondary}</div>
        </div>
      `;
    }).join("");
  }

  // ── top crops panel ─────────────────────────────────────────────
  function renderTopCrops() {
    const list = (state.data && state.data.top_crops) || [];
    const target = $id("top-crops-list");
    if (!target) return;
    if (!list.length) {
      target.innerHTML = `<div class="empty-state">No crop intake in this window.</div>`;
      return;
    }
    target.innerHTML = list.map((row, i) => `
      <div class="crop-row">
        <div class="row-rank">${i + 1}</div>
        <div>
          <div class="row-name">${escapeHtml(row.name)}</div>
        </div>
        <div class="row-metric">${fmtMoney(row.value)}</div>
        <div class="row-metric muted">${fmtQty(row.qty)}<span class="unit">units</span></div>
      </div>
    `).join("");
  }

  // ── spoilage panel ──────────────────────────────────────────────
  function renderSpoilage() {
    const d = state.data; if (!d) return;
    const sp = d.spoilage || {};
    const target = $id("spoilage-list");
    if (!target) return;

    if (state.activeSpoilageTab === "reasons") {
      const list = sp.top_reasons || [];
      if (!list.length) {
        target.innerHTML = `<div class="empty-state">No spoilage records in this window.</div>`;
        return;
      }
      target.innerHTML = list.map((row) => `
        <div class="spoil-row">
          <div class="row-name">${escapeHtml(row.reason)}</div>
          <div class="row-metric">${fmtMoney(row.value)}</div>
          <div class="row-metric muted">${fmtQty(row.qty)}<span class="unit">units</span></div>
        </div>
      `).join("");
    } else {
      const list = sp.by_crop || [];
      if (!list.length) {
        target.innerHTML = `<div class="empty-state">No spoilage records in this window.</div>`;
        return;
      }
      const maxRate = Math.max(1, ...list.map((r) => r.rate_pct || 0));
      target.innerHTML = list.map((row) => {
        const widthPct = Math.min(100, (row.rate_pct / maxRate) * 100);
        return `
          <div class="spoil-row">
            <div>
              <div class="row-name">${escapeHtml(row.name)}</div>
              <div class="row-sub">${fmtQty(row.spoiled)} spoiled of ${fmtQty(row.delivered + row.spoiled)}</div>
            </div>
            <div class="rate-bar"><span style="width:${widthPct}%"></span></div>
            <div class="row-metric neg">${row.rate_pct}%</div>
          </div>
        `;
      }).join("");
    }
  }

  // ── expenses panel ──────────────────────────────────────────────
  function renderExpenses() {
    const d = state.data; if (!d) return;
    const ex = d.expenses || {};
    const target = $id("expenses-list");
    if (!target) return;
    const list = state.activeExpTab === "farm" ? (ex.by_farm || []) : (ex.by_category || []);
    if (!list.length) {
      target.innerHTML = `<div class="empty-state">No farm-tagged expenses in this window.</div>`;
      return;
    }
    target.innerHTML = list.map((row) => `
      <div class="exp-row">
        <div class="row-name">${escapeHtml(row.category || row.farm)}</div>
        <div class="row-metric">${fmtMoney(row.amount)}</div>
        <div class="row-metric muted">${fmtInt(row.count || 0)} entries</div>
      </div>
    `).join("");
  }

  // ── contribution panel ──────────────────────────────────────────
  function renderContribution() {
    const list = (state.data && state.data.contribution) || [];
    const target = $id("contribution-list");
    if (!target) return;
    if (!list.length) {
      target.innerHTML = `<div class="empty-state">No data to compute contribution yet.</div>`;
      return;
    }
    target.innerHTML = list.map((row) => {
      const cls = row.net >= 0 ? "pos" : "neg";
      return `
        <div class="contrib-row">
          <div class="row-name">${escapeHtml(row.farm)}</div>
          <div class="row-metric muted col-mid">+${fmtMoney(row.delivered_value)}</div>
          <div class="row-metric muted col-mid">−${fmtMoney(row.expenses)}</div>
          <div class="row-metric muted col-mid">−${fmtMoney(row.spoiled_value)}</div>
          <div class="row-metric ${cls}">${fmtMoney(row.net)}</div>
        </div>
      `;
    }).join("");
  }

  // ── signals panel ───────────────────────────────────────────────
  function renderSignals() {
    const list = (state.data && state.data.insights) || [];
    const target = $id("signals-list");
    if (!target) return;
    if (!list.length) {
      target.innerHTML = `<div class="empty-state">No notable signals — things look steady.</div>`;
      return;
    }
    target.innerHTML = list.map((row) => `
      <div class="signal-row">
        <div class="signal-dot ${escapeHtml(row.kind || "info")}"></div>
        <div>
          <div class="signal-title">${escapeHtml(row.title || "")}</div>
          <div class="signal-body">${escapeHtml(row.body || "")}</div>
        </div>
      </div>
    `).join("");
  }

  // ── error banner ────────────────────────────────────────────────
  function showError(message) {
    const banner = $id("error-banner");
    if (!banner) return;
    banner.innerHTML = `<div class="card error-card" role="alert">${escapeHtml(message)}</div>`;
  }
  function clearError() {
    const banner = $id("error-banner");
    if (banner) banner.innerHTML = "";
  }

  // ── escape ──────────────────────────────────────────────────────
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ── fetch + paint ───────────────────────────────────────────────
  async function load() {
    paintHeader();
    const loading = $id("loading");
    if (loading) loading.style.display = "flex";

    const params = new URLSearchParams();
    params.set("range", state.range);
    if (state.range === "custom") {
      if (state.start) params.set("start", state.start);
      if (state.end)   params.set("end",   state.end);
    }

    try {
      const r = await fetch(`/farm-dashboard/summary?${params.toString()}`, {
        headers: { Accept: "application/json" },
      });
      if (!r.ok) {
        if (r.status === 401) { window.location.href = "/"; return; }
        throw new Error(`HTTP ${r.status}`);
      }
      const data = await r.json();
      state.data = data;
      clearError();
      renderNumberCards(data);
      renderBriefing(data);
      renderSeasonChart(data);
      renderTopFarms();
      renderTopCrops();
      renderSpoilage();
      renderExpenses();
      renderContribution();
      renderSignals();
      paintHeader();
    } catch (err) {
      console.error("farm-dashboard load failed", err);
      showError("Couldn't load farm dashboard. Please refresh.");
      const stale = $id("last-updated");
      if (stale) stale.classList.add("last-updated-error");
    } finally {
      if (loading) loading.style.display = "none";
    }
  }

  // ── range picker wiring ─────────────────────────────────────────
  function setRange(range) {
    state.range = range;
    document.querySelectorAll(".range-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.range === range);
    });
  }

  function openCustomRange() {
    $id("custom-range-modal")?.classList.remove("hidden");
    const today = new Date().toISOString().slice(0, 10);
    const s = $id("custom-range-start"); if (s && !s.value) s.value = today;
    const e = $id("custom-range-end");   if (e && !e.value) e.value = today;
  }
  function closeCustomRange() {
    $id("custom-range-modal")?.classList.add("hidden");
    const err = $id("custom-range-error"); if (err) err.hidden = true;
  }
  function applyCustomRange() {
    const s = $id("custom-range-start")?.value;
    const e = $id("custom-range-end")?.value;
    const err = $id("custom-range-error");
    if (!s || !e) {
      if (err) { err.textContent = "Please pick a start and end date."; err.hidden = false; }
      return;
    }
    if (s > e) {
      if (err) { err.textContent = "Start date must be before end date."; err.hidden = false; }
      return;
    }
    state.start = s;
    state.end   = e;
    setRange("custom");
    closeCustomRange();
    load();
  }

  // ── bootstrap ───────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", () => {
    setRange(state.range);

    document.querySelectorAll(".range-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const r = btn.dataset.range;
        if (r === "custom") { openCustomRange(); return; }
        setRange(r);
        load();
      });
    });

    document.querySelectorAll("[data-farm-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.activeFarmTab = btn.dataset.farmTab;
        document.querySelectorAll("[data-farm-tab]").forEach((b) =>
          b.classList.toggle("active", b.dataset.farmTab === state.activeFarmTab));
        renderTopFarms();
      });
    });

    document.querySelectorAll("[data-spoilage-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.activeSpoilageTab = btn.dataset.spoilageTab;
        document.querySelectorAll("[data-spoilage-tab]").forEach((b) =>
          b.classList.toggle("active", b.dataset.spoilageTab === state.activeSpoilageTab));
        renderSpoilage();
      });
    });

    document.querySelectorAll("[data-exp-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.activeExpTab = btn.dataset.expTab;
        document.querySelectorAll("[data-exp-tab]").forEach((b) =>
          b.classList.toggle("active", b.dataset.expTab === state.activeExpTab));
        renderExpenses();
      });
    });

    $id("range-cancel")?.addEventListener("click", closeCustomRange);
    $id("range-modal-close")?.addEventListener("click", closeCustomRange);
    $id("range-apply")?.addEventListener("click", applyCustomRange);
    $id("custom-range-modal")?.addEventListener("click", (e) => {
      if (e.target.id === "custom-range-modal") closeCustomRange();
    });

    load();
    // Auto-refresh every 60 seconds
    setInterval(load, 60000);
  });
})();