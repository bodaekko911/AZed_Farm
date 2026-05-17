/* ───────────────────────────────────────────────────────────────────
   Farm Dashboard — front-end controller.
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
    activeUtilTab: "total",
    chart:  null,
    utilityChart: null,
    currentUser: null,
    loaded: false,   // true after first successful load — suppresses spinner on refresh
  };

  // ── helpers ─────────────────────────────────────────────────────
  function $id(id) { return document.getElementById(id); }
  function setText(id, val) { const n = $id(id); if (n) n.textContent = val; }
  function fmtMoney(n) { return FMT_MONEY.format(Math.round(Number(n) || 0)); }
  function fmtQty(n)   { return FMT_QTY.format(Number(n) || 0); }
  function fmtInt(n)   { return FMT_INT.format(Math.round(Number(n) || 0)); }
  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmtDelta(pct) {
    if (pct === null || pct === undefined) return "";
    const sign = pct > 0 ? "▲" : pct < 0 ? "▼" : "•";
    return `${sign} ${Math.abs(pct).toFixed(1)}%`;
  }
  function deltaClass(pct, invert) {
    if (pct === null || pct === undefined || pct === 0) return "";
    const positive = invert ? pct < 0 : pct > 0;
    return positive ? "positive" : "negative";
  }

  function longDateLabel() {
    return new Date().toLocaleDateString("en-GB", {
      weekday: "long", year: "numeric", month: "long", day: "numeric",
    });
  }

  function setGreeting() {
    const hour = new Date().getHours();
    let g = "Good evening";
    if (hour < 12) g = "Good morning";
    else if (hour < 18) g = "Good afternoon";
    const name = state.currentUser?.name ? `, ${state.currentUser.name.split(" ")[0]}` : "";
    setText("greeting", `${g}${name}`);
  }

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
    const dl  = d.deliveries || {};
    const sp  = d.spoilage   || {};
    const ex  = d.expenses   || {};
    const cad = d.cadence    || {};
    const ut  = d.utilities  || {};
    const wt  = ut.water       || {};
    const el  = ut.electricity || {};

    const specs = {
      deliveries: {
        label:     "Deliveries",
        value:     fmtInt(dl.count || 0),
        meta:      `${cad.deliveries_per_day || 0}/day · ${cad.active_days || 0} active days`,
        breakdown: fmtDelta(dl.count_delta),
        deltaCls:  deltaClass(dl.count_delta, false),
      },
      intake_qty: {
        label:     "Intake quantity",
        value:     fmtQty(dl.qty || 0),
        meta:      `${fmtQty(cad.qty_per_day || 0)} avg per day`,
        breakdown: fmtDelta(dl.qty_delta),
        deltaCls:  deltaClass(dl.qty_delta, false),
      },
      intake_value: {
        label:     "Intake value",
        value:     fmtMoney(dl.value || 0),
        meta:      "Estimated at product cost",
        breakdown: fmtDelta(dl.value_delta),
        deltaCls:  deltaClass(dl.value_delta, false),
      },
      spoilage: {
        label:     "Spoilage",
        value:     fmtQty(sp.qty || 0),
        meta:      `${sp.rate_pct || 0}% rate · ${fmtMoney(sp.value || 0)} lost`,
        breakdown: fmtDelta(sp.qty_delta),
        deltaCls:  deltaClass(sp.qty_delta, true),
      },
      farm_expenses: {
        label:     "Farm expenses",
        value:     fmtMoney(ex.farm_total || 0),
        meta:      `${ex.farm_share_pct || 0}% of company spend`,
        breakdown: fmtDelta(ex.farm_delta),
        deltaCls:  deltaClass(ex.farm_delta, true),
      },
      water: {
        label:     "Water consumption",
        value:     `${fmtQty(wt.consumption || 0)} ${wt.unit || "m³"}`,
        meta:      `${fmtMoney(wt.cost || 0)} · ${fmtInt(wt.count || 0)} bill${(wt.count || 0) === 1 ? "" : "s"}`,
        breakdown: fmtDelta(wt.delta),
        deltaCls:  deltaClass(wt.delta, true),
      },
      electricity: {
        label:     "Electricity consumption",
        value:     `${fmtQty(el.consumption || 0)} ${el.unit || "kWh"}`,
        meta:      `${fmtMoney(el.cost || 0)} · ${fmtInt(el.count || 0)} bill${(el.count || 0) === 1 ? "" : "s"}`,
        breakdown: fmtDelta(el.delta),
        deltaCls:  deltaClass(el.delta, true),
      },
    };

    Object.keys(specs).forEach((key) => {
      const card = document.querySelector(`[data-card="${key}"]`);
      if (!card) return;
      const s = specs[key];
      card.innerHTML = `
        <div class="number-card-button">
          <span class="number-label">${escHtml(s.label)}</span>
          <strong class="number-value">${escHtml(s.value)}</strong>
          <span class="number-meta">${escHtml(s.meta)}</span>
          <span class="number-breakdown ${s.deltaCls}">${escHtml(s.breakdown)}</span>
        </div>
      `;
    });
  }

  // ── briefing card ────────────────────────────────────────────────
  function renderBriefing(d) {
    const dl    = d.deliveries   || {};
    const sp    = d.spoilage     || {};
    const range = d.range        || {};
    const sx    = d.season_peaks || {};

    let lead = `${range.label || ""} — ${fmtQty(dl.qty || 0)} units intaken across ${fmtInt(dl.count || 0)} deliveries.`;
    if (dl.qty_delta !== null && dl.qty_delta !== undefined) {
      lead += ` Intake is ${dl.qty_delta >= 0 ? "up" : "down"} ${Math.abs(dl.qty_delta).toFixed(1)}% vs the previous window.`;
    }

    const bodyParts = [];
    if (sp.rate_pct != null)          bodyParts.push(`Spoilage running at ${sp.rate_pct}%.`);
    if (sx.peak_month)                bodyParts.push(`Strongest month in the last 12: ${sx.peak_month}.`);
    if ((d.dormant_count || 0) > 0)   bodyParts.push(`${d.dormant_count} active farm(s) had no deliveries.`);

    setText("briefing-lead", lead);
    setText("briefing-body", bodyParts.join(" "));
  }

  // ── season chart ─────────────────────────────────────────────────
  function renderSeasonChart(d) {
    const ctx = $id("season-chart");
    if (!ctx || !window.Chart) return;
    const season = d.season || [];

    if (state.chart) {
      try { state.chart.destroy(); } catch (_) {}
    }
    state.chart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: season.map((m) => m.label),
        datasets: [
          {
            label: "Intake qty",
            data: season.map((m) => m.qty),
            backgroundColor: "rgba(74, 222, 128, .55)",
            borderColor: "#4ade80",
            borderWidth: 1,
            yAxisID: "y",
            order: 2,
          },
          {
            label: "Spoilage qty",
            data: season.map((m) => m.spoilage),
            backgroundColor: "rgba(248, 113, 113, .55)",
            borderColor: "#f87171",
            borderWidth: 1,
            yAxisID: "y",
            order: 1,
          },
          {
            label: "Farm expenses (EGP)",
            data: season.map((m) => m.expenses),
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

    const table = $id("chart-table");
    if (table) {
      table.innerHTML =
        "<thead><tr><th>Month</th><th>Intake</th><th>Spoilage</th><th>Expenses</th></tr></thead>" +
        "<tbody>" + season.map((m) =>
          `<tr><td>${escHtml(m.label)}</td><td>${fmtQty(m.qty)}</td><td>${fmtQty(m.spoilage)}</td><td>${fmtMoney(m.expenses)}</td></tr>`
        ).join("") + "</tbody>";
    }
  }

  // ── shared list-row builder ──────────────────────────────────────
  function listRow({ title, sub, value, valueSub, bar, barClass }) {
    const subHtml    = sub      ? `<span class="row-sub">${escHtml(sub)}</span>`           : "";
    const valSubHtml = valueSub ? `<span class="row-sub mono">${escHtml(valueSub)}</span>` : "";
    const barHtml    = (bar !== null && bar !== undefined)
      ? `<span class="row-bar ${barClass || ""}"><span style="width:${Math.max(2, Math.min(100, bar))}%"></span></span>`
      : "";
    return `
      <div class="list-row">
        <div class="row-main">
          <div class="row-left">
            <div class="row-title">${escHtml(title)}</div>
            ${subHtml}
          </div>
          <div class="row-right">
            <strong class="row-value mono">${escHtml(value)}</strong>
            ${valSubHtml}
          </div>
        </div>
        ${barHtml}
      </div>
    `;
  }

  function emptyState(msg) {
    return `<div class="empty-state">${escHtml(msg)}</div>`;
  }

  // ── top farms panel ──────────────────────────────────────────────
  function renderTopFarms() {
    const d = state.data; if (!d) return;
    const byQty = state.activeFarmTab === "qty";
    const list  = byQty ? (d.top_farms_by_qty || []) : (d.top_farms_by_value || []);
    const target = $id("top-farms-list"); if (!target) return;
    if (!list.length) { target.innerHTML = emptyState("No farm deliveries in this window."); return; }

    const maxV = Math.max(1, ...list.map((r) => byQty ? Number(r.qty || 0) : Number(r.value || 0)));
    target.innerHTML = list.map((row) => {
      const v         = byQty ? Number(row.qty || 0) : Number(row.value || 0);
      const primary   = byQty ? `${fmtQty(row.qty)} units` : fmtMoney(row.value);
      const secondary = byQty ? fmtMoney(row.value) : `${fmtQty(row.qty)} units`;
      return listRow({
        title: row.farm,
        sub: `${fmtInt(row.deliveries || 0)} deliveries`,
        value: primary,
        valueSub: secondary,
        bar: (v / maxV) * 100,
      });
    }).join("");
  }

  // ── top products by intake panel ─────────────────────────────────
  // Shows products ranked by delivered qty, with their spoilage rate
  // alongside so you can spot high-waste items at a glance.
  function renderTopProducts() {
    const d = state.data; if (!d) return;
    const target = $id("top-crops-list"); if (!target) return;

    const intakeList  = d.top_crops || [];           // products by delivery qty
    const spoilByPid  = {};                           // build spoilage rate lookup
    (d.spoilage?.by_crop || []).forEach((sc) => {
      spoilByPid[sc.product_id] = sc.rate_pct;
    });

    if (!intakeList.length) {
      target.innerHTML = emptyState("No product intake in this window.");
      return;
    }

    // Sort by qty (most delivered first)
    const sorted = [...intakeList].sort((a, b) => Number(b.qty || 0) - Number(a.qty || 0));
    const maxQty = Math.max(1, ...sorted.map((r) => Number(r.qty || 0)));

    target.innerHTML = sorted.map((row) => {
      const spoilRate = spoilByPid[row.product_id];
      const spoilText = spoilRate != null ? `${spoilRate}% spoilage` : "no spoilage data";
      const spoilCls  = spoilRate >= 10 ? " (high ⚠)" : "";
      return listRow({
        title:    row.name,
        sub:      `${spoilText}${spoilCls}`,
        value:    `${fmtQty(row.qty)} units`,
        valueSub: fmtMoney(row.value),
        bar:      (Number(row.qty || 0) / maxQty) * 100,
        barClass: spoilRate >= 10 ? "negative" : "",
      });
    }).join("");
  }

  // ── spoilage panel ───────────────────────────────────────────────
  function renderSpoilage() {
    const d = state.data; if (!d) return;
    const sp     = d.spoilage || {};
    const target = $id("spoilage-list"); if (!target) return;

    if (state.activeSpoilageTab === "reasons") {
      const list = sp.top_reasons || [];
      if (!list.length) { target.innerHTML = emptyState("No spoilage records in this window."); return; }
      const maxV = Math.max(1, ...list.map((r) => Number(r.value || 0)));
      target.innerHTML = list.map((row) => listRow({
        title:    row.reason,
        sub:      `${fmtInt(row.count || 0)} record(s)`,
        value:    fmtMoney(row.value),
        valueSub: `${fmtQty(row.qty)} units`,
        bar:      (Number(row.value || 0) / maxV) * 100,
        barClass: "negative",
      })).join("");
    } else {
      const list = sp.by_crop || [];
      if (!list.length) { target.innerHTML = emptyState("No spoilage records in this window."); return; }
      const maxRate = Math.max(1, ...list.map((r) => Number(r.rate_pct || 0)));
      target.innerHTML = list.map((row) => listRow({
        title:    row.name,
        sub:      `${fmtQty(row.spoiled)} spoiled / ${fmtQty(row.delivered + row.spoiled)} handled`,
        value:    `${row.rate_pct}%`,
        valueSub: null,
        bar:      (Number(row.rate_pct || 0) / maxRate) * 100,
        barClass: "negative",
      })).join("");
    }
  }

  // ── expenses panel ───────────────────────────────────────────────
  function renderExpenses() {
    const d = state.data; if (!d) return;
    const ex     = d.expenses || {};
    const target = $id("expenses-list"); if (!target) return;
    const list   = state.activeExpTab === "farm" ? (ex.by_farm || []) : (ex.by_category || []);
    if (!list.length) { target.innerHTML = emptyState("No farm-tagged expenses in this window."); return; }
    const maxV = Math.max(1, ...list.map((r) => Number(r.amount || 0)));
    target.innerHTML = list.map((row) => listRow({
      title:    row.category || row.farm,
      sub:      `${fmtInt(row.count || 0)} entries`,
      value:    fmtMoney(row.amount),
      valueSub: null,
      bar:      (Number(row.amount || 0) / maxV) * 100,
    })).join("");
  }

  // ── utilities trend chart (water + electricity, last 12 months) ──
  function renderUtilitiesChart(d) {
    const months = (d.utilities && d.utilities.by_month) || [];
    const canvas = $id("utilities-chart");
    if (!canvas || !window.Chart) return;

    if (state.utilityChart) {
      state.utilityChart.destroy();
      state.utilityChart = null;
    }

    const waterUnit = (d.utilities && d.utilities.water && d.utilities.water.unit) || "m³";
    const elecUnit  = (d.utilities && d.utilities.electricity && d.utilities.electricity.unit) || "kWh";

    state.utilityChart = new Chart(canvas, {
      type: "line",
      data: {
        labels: months.map((m) => m.label),
        datasets: [
          {
            label: `Water (${waterUnit})`,
            data: months.map((m) => m.water),
            borderColor: "#38bdf8",
            backgroundColor: "rgba(56, 189, 248, .18)",
            tension: .35,
            yAxisID: "y",
            fill: true,
          },
          {
            label: `Electricity (${elecUnit})`,
            data: months.map((m) => m.electricity),
            borderColor: "#fbbf24",
            backgroundColor: "rgba(251, 191, 36, .18)",
            tension: .35,
            yAxisID: "y1",
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
          y:  { beginAtZero: true, position: "left",  title: { display: true, text: waterUnit } },
          y1: { beginAtZero: true, position: "right", title: { display: true, text: elecUnit }, grid: { drawOnChartArea: false } },
        },
      },
    });

    const table = $id("utilities-chart-table");
    if (table) {
      table.innerHTML =
        `<thead><tr><th>Month</th><th>Water (${escHtml(waterUnit)})</th><th>Electricity (${escHtml(elecUnit)})</th></tr></thead>` +
        "<tbody>" + months.map((m) =>
          `<tr><td>${escHtml(m.label)}</td><td>${fmtQty(m.water)}</td><td>${fmtQty(m.electricity)}</td></tr>`
        ).join("") + "</tbody>";
    }
  }

  // ── utilities-by-farm panel ──────────────────────────────────────
  function renderUtilities() {
    const d = state.data; if (!d) return;
    const ut     = d.utilities || {};
    const list   = ut.by_farm || [];
    const target = $id("utilities-list"); if (!target) return;
    if (!list.length) { target.innerHTML = emptyState("No water or electricity bills logged in this window."); return; }

    const waterUnit = (ut.water && ut.water.unit) || "m³";
    const elecUnit  = (ut.electricity && ut.electricity.unit) || "kWh";
    const tab = state.activeUtilTab;

    // Sort & choose what to show as the primary value depending on tab.
    let sorted = list.slice();
    if (tab === "water") {
      sorted.sort((a, b) => Number(b.water_cost || 0) - Number(a.water_cost || 0));
    } else if (tab === "electricity") {
      sorted.sort((a, b) => Number(b.electricity_cost || 0) - Number(a.electricity_cost || 0));
    } else {
      sorted.sort((a, b) => Number(b.total_cost || 0) - Number(a.total_cost || 0));
    }

    const maxV = Math.max(1, ...sorted.map((r) => {
      if (tab === "water")       return Number(r.water_cost || 0);
      if (tab === "electricity") return Number(r.electricity_cost || 0);
      return Number(r.total_cost || 0);
    }));

    target.innerHTML = sorted.map((row) => {
      let primary, secondary, barVal;
      if (tab === "water") {
        primary   = fmtMoney(row.water_cost);
        secondary = `${fmtQty(row.water_qty)} ${waterUnit}`;
        barVal    = Number(row.water_cost || 0);
      } else if (tab === "electricity") {
        primary   = fmtMoney(row.electricity_cost);
        secondary = `${fmtQty(row.electricity_qty)} ${elecUnit}`;
        barVal    = Number(row.electricity_cost || 0);
      } else {
        primary   = fmtMoney(row.total_cost);
        secondary = `${fmtQty(row.water_qty)} ${waterUnit} · ${fmtQty(row.electricity_qty)} ${elecUnit}`;
        barVal    = Number(row.total_cost || 0);
      }
      return listRow({
        title:    row.farm,
        sub:      secondary,
        value:    primary,
        valueSub: null,
        bar:      (barVal / maxV) * 100,
      });
    }).join("");
  }

  // ── contribution panel ───────────────────────────────────────────
  function renderContribution() {
    const list   = (state.data && state.data.contribution) || [];
    const target = $id("contribution-list"); if (!target) return;
    if (!list.length) { target.innerHTML = emptyState("No data to compute contribution yet."); return; }
    const maxAbs = Math.max(1, ...list.map((r) => Math.abs(Number(r.net || 0))));
    target.innerHTML = list.map((row) => {
      const net = Number(row.net || 0);
      const cls = net >= 0 ? "positive" : "negative";
      const bar = (Math.abs(net) / maxAbs) * 100;
      const sub = `+${fmtMoney(row.delivered_value)} − ${fmtMoney(row.expenses)} exp − ${fmtMoney(row.spoiled_value)} spoiled`;
      return `
        <div class="list-row">
          <div class="row-main">
            <div class="row-left">
              <div class="row-title">${escHtml(row.farm)}</div>
              <span class="row-sub">${escHtml(sub)}</span>
            </div>
            <div class="row-right">
              <strong class="row-value mono ${cls}">${escHtml(fmtMoney(net))}</strong>
            </div>
          </div>
          <span class="row-bar ${cls}"><span style="width:${Math.max(2, Math.min(100, bar))}%"></span></span>
        </div>
      `;
    }).join("");
  }

  // ── signals panel ────────────────────────────────────────────────
  function renderSignals() {
    const list   = (state.data && state.data.insights) || [];
    const target = $id("signals-list"); if (!target) return;
    if (!list.length) { target.innerHTML = emptyState("No notable signals — things look steady."); return; }
    target.innerHTML = list.map((row) => {
      const kind = row.kind || "info";
      return `
        <div class="list-row signal-row">
          <div class="row-main">
            <div class="row-left">
              <span class="signal-dot signal-dot-${escHtml(kind)}"></span>
              <div class="row-title-stack">
                <div class="row-title">${escHtml(row.title || "")}</div>
                <span class="row-sub">${escHtml(row.body || "")}</span>
              </div>
            </div>
          </div>
        </div>
      `;
    }).join("");
  }

  // ── error banner ─────────────────────────────────────────────────
  function showError(message) {
    const banner = $id("error-banner");
    if (banner) banner.innerHTML = `<div class="error-banner load-error" role="alert"><div class="error-banner-text">${escHtml(message)}</div></div>`;
  }
  function clearError() {
    const banner = $id("error-banner");
    if (banner) banner.innerHTML = "";
  }

  // ── fetch + paint ─────────────────────────────────────────────────
  // silent=true → skip the full-page spinner (used for background refresh)
  async function load(silent) {
    const loading = $id("loading");
    // Only show the blocking spinner on the very first load
    if (!silent && !state.loaded && loading) loading.style.display = "flex";

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
      state.data   = data;
      state.loaded = true;
      clearError();
      renderNumberCards(data);
      renderBriefing(data);
      renderSeasonChart(data);
      renderUtilitiesChart(data);
      renderTopFarms();
      renderTopProducts();
      renderSpoilage();
      renderExpenses();
      renderUtilities();
      renderContribution();
      renderSignals();
      paintHeader();
    } catch (err) {
      console.error("farm-dashboard load failed", err);
      if (!silent) showError("Couldn't load farm dashboard. Please refresh.");
      const stale = $id("last-updated");
      if (stale) stale.classList.add("last-updated-error");
    } finally {
      if (loading) loading.style.display = "none";
    }
  }

  // ── range picker ─────────────────────────────────────────────────
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
    const s   = $id("custom-range-start")?.value;
    const e   = $id("custom-range-end")?.value;
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
    load(false);
  }

  // ── account / user wiring ────────────────────────────────────────
  async function initUser() {
    try {
      const r = await fetch("/auth/me");
      if (r.ok) state.currentUser = await r.json();
    } catch (_) {}
    const name   = state.currentUser?.name  || "Admin";
    const email  = state.currentUser?.email || "—";
    const avatar = (name.trim()[0] || "A").toUpperCase();
    setText("user-name", name);
    setText("user-email", email);
    setText("user-avatar", avatar);
    setGreeting();
  }

  function bindAccountMenu() {
    const trigger  = $id("account-trigger");
    const dropdown = $id("account-dropdown");
    const signout  = $id("signout-btn");
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

  function bindThemeToggle() {
    const btn = $id("mode-btn");
    if (!btn) return;
    btn.addEventListener("click", () => {
      if (typeof window.toggleTheme === "function") window.toggleTheme();
    });
  }

  // ── bootstrap ─────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", () => {
    setRange(state.range);

    document.querySelectorAll(".range-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const r = btn.dataset.range;
        if (r === "custom") { openCustomRange(); return; }
        setRange(r);
        load(false);
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

    document.querySelectorAll("[data-util-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.activeUtilTab = btn.dataset.utilTab;
        document.querySelectorAll("[data-util-tab]").forEach((b) =>
          b.classList.toggle("active", b.dataset.utilTab === state.activeUtilTab));
        renderUtilities();
      });
    });

    $id("range-cancel")?.addEventListener("click", closeCustomRange);
    $id("range-modal-close")?.addEventListener("click", closeCustomRange);
    $id("range-apply")?.addEventListener("click", applyCustomRange);
    $id("custom-range-modal")?.addEventListener("click", (e) => {
      if (e.target.id === "custom-range-modal") closeCustomRange();
    });

    if (!window.__appNav) {
      bindAccountMenu();
      bindThemeToggle();
    }

    initUser();
    load(false);                              // first load — shows spinner
    setInterval(() => load(true), 60000);    // background refresh — no flicker
  });
})();