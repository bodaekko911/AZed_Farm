/*
 * Charts inside Ask answers.
 *
 * The model may add one fenced block to an answer:
 *
 *   ```chart
 *   {"type": "line" | "bar" | "hbar", "title": "...", "unit": "EGP",
 *    "labels": ["Jan", "Feb", ...], "series": [{"name": "Net sales", "values": [1200, 900, ...]}]}
 *   ```
 *
 * AskChart.extract() pulls those blocks out of the answer text (a block that isn't valid is dropped, never
 * shown as raw JSON) and leaves a placeholder; AskChart.mount() draws each one as an inline SVG.
 * Every string from the model is set with textContent — nothing it writes becomes markup.
 *
 * Colours come from the page's CSS: --chart-1..3, --chart-grid, --chart-axis, and --card for the surface.
 */
(function () {
  "use strict";
  const SVG = "http://www.w3.org/2000/svg";
  const FENCE = /```chart[^\n]*\n([\s\S]*?)```/g;
  const TYPES = ["line", "bar", "hbar"];
  const MAX_SERIES = 3, MAX_LABELS = 40;

  function validate(raw) {
    if (!raw || typeof raw !== "object" || !TYPES.includes(raw.type)) return null;
    const labels = Array.isArray(raw.labels) ? raw.labels.map(l => String(l ?? "")) : [];
    if (!labels.length || labels.length > MAX_LABELS) return null;
    const series = (Array.isArray(raw.series) ? raw.series : []).slice(0, MAX_SERIES).map((s, i) => ({
      name: String((s && s.name) || `Series ${i + 1}`),
      values: (Array.isArray(s && s.values) ? s.values : []).map(v => {
        const n = typeof v === "string" ? Number(v.replace(/,/g, "")) : v;
        return typeof n === "number" && isFinite(n) ? n : null;
      }),
    })).filter(s => s.values.length === labels.length && s.values.some(v => v !== null));
    if (!series.length) return null;
    return { type: raw.type, title: String(raw.title || ""), unit: String(raw.unit || "").slice(0, 8), labels, series };
  }

  function extract(text) {
    const specs = [];
    const out = String(text || "").replace(FENCE, (_m, body) => {
      let spec = null;
      try { spec = validate(JSON.parse(body)); } catch (e) { spec = null; }
      if (!spec) return "";
      specs.push(spec);
      return `\n\n@@CHART${specs.length - 1}@@\n\n`;
    });
    return { text: out, specs };
  }

  /** The answer without its chart blocks — for copying and for the history sent back to the model. */
  function strip(text) {
    return String(text || "").replace(FENCE, "").replace(/\n{3,}/g, "\n\n").trim();
  }

  /** Turn the placeholders md() left as paragraphs into chart slots. */
  function slots(html) {
    return html.replace(/<p>@@CHART(\d+)@@<\/p>|@@CHART(\d+)@@/g,
      (_m, a, b) => `<div class="ask-chart" data-chart="${a ?? b}"></div>`);
  }

  // ── numbers ──
  function compact(v) {
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(a >= 1e5 ? 0 : 1).replace(/\.0$/, "") + "K";
    return v.toLocaleString("en", { maximumFractionDigits: a < 10 ? 2 : a < 100 ? 1 : 0 });
  }
  function full(v, unit) {
    if (v === null) return "—";
    const n = v.toLocaleString("en", { maximumFractionDigits: 2 });
    return unit === "%" ? n + "%" : unit ? `${n} ${unit}` : n;
  }
  function niceStep(span, count) {
    const raw = span / Math.max(count, 1);
    const p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const m = raw / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
  }
  function scale(values) {
    const finite = values.filter(v => v !== null);
    let lo = Math.min(0, ...finite), hi = Math.max(0, ...finite);
    if (hi === lo) hi = lo + 1;
    const step = niceStep(hi - lo, 4);
    lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
    const ticks = [];
    for (let t = lo; t <= hi + step / 2; t += step) ticks.push(+t.toFixed(10));
    return { lo, hi, ticks };
  }

  // ── svg helpers ──
  function el(name, attrs, parent) {
    const node = document.createElementNS(SVG, name);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(node);
    return node;
  }
  function text(parent, x, y, str, attrs) {
    const t = el("text", Object.assign({ x, y, fill: "var(--muted)", "font-size": 11 }, attrs || {}), parent);
    t.textContent = str;
    return t;
  }
  /** A bar from the baseline, rounded 4px only at its data end. Vertical (columns) or horizontal. */
  function barPath(x, y, w, h, end) {
    const r = Math.min(4, Math.abs(end === "top" || end === "bottom" ? h : w), (end === "top" || end === "bottom" ? w : h) / 2);
    if (end === "top") return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
    if (end === "bottom") return `M${x},${y}V${y + h - r}Q${x},${y + h} ${x + r},${y + h}H${x + w - r}Q${x + w},${y + h} ${x + w},${y + h - r}V${y}Z`;
    if (end === "right") return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`;
    return `M${x + w},${y}H${x + r}Q${x},${y} ${x},${y + r}V${y + h - r}Q${x},${y + h} ${x + r},${y + h}H${x + w}Z`;
  }
  const color = i => `var(--chart-${i + 1})`;
  const textWidth = (s, size) => String(s).length * size * 0.56;

  // ── tooltip ──
  function tooltip(box) {
    const tip = document.createElement("div");
    tip.className = "ask-chart-tip";
    tip.hidden = true;
    box.appendChild(tip);
    return {
      show(x, y, label, rows, unit) {
        tip.replaceChildren();
        const head = document.createElement("div");
        head.className = "ask-chart-tip-head"; head.textContent = label; tip.appendChild(head);
        rows.forEach(r => {
          const row = document.createElement("div"); row.className = "ask-chart-tip-row";
          const key = document.createElement("span"); key.className = "ask-chart-key"; key.style.background = r.color;
          const name = document.createElement("span"); name.textContent = r.name;
          const val = document.createElement("b"); val.textContent = full(r.value, unit);
          row.append(key, name, val); tip.appendChild(row);
        });
        tip.hidden = false;
        const bw = box.clientWidth, tw = tip.offsetWidth;
        tip.style.left = Math.max(0, Math.min(x + 12, bw - tw)) + "px";
        tip.style.top = Math.max(0, y - tip.offsetHeight - 8) + "px";
      },
      hide() { tip.hidden = true; },
    };
  }

  // ── charts ──
  function columns(svg, spec, W, H, tip, box) {
    const all = spec.series.flatMap(s => s.values);
    const { lo, hi, ticks } = scale(all);
    const left = Math.max(...ticks.map(t => textWidth(compact(t), 11))) + 10;
    const lastLabel = spec.type === "line" && spec.series.length <= 3;
    const right = lastLabel ? Math.max(...spec.series.map(s => textWidth(compact(s.values.filter(v => v !== null).slice(-1)[0] ?? 0), 11))) + 14 : 8;
    const top = 10, bottom = 24, pw = W - left - right, ph = H - top - bottom;
    const n = spec.labels.length, band = pw / n;
    const y = v => top + ph - ((v - lo) / (hi - lo)) * ph;

    ticks.forEach(t => {
      el("line", { x1: left, x2: left + pw, y1: y(t), y2: y(t), stroke: t === 0 ? "var(--chart-axis)" : "var(--chart-grid)", "stroke-width": 1 }, svg);
      text(svg, left - 6, y(t) + 4, compact(t), { "text-anchor": "end", "font-variant-numeric": "tabular-nums" });
    });
    const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(pw / Math.max(44, Math.max(...spec.labels.map(l => textWidth(l, 11))) + 10)))));
    spec.labels.forEach((l, i) => {
      if (i % every === 0 || i === n - 1 && n - 1 - Math.floor((n - 1) / every) * every > every / 2)
        text(svg, left + band * (i + 0.5), H - 6, l, { "text-anchor": "middle" });
    });

    const k = spec.series.length;
    if (spec.type === "bar") {
      const barW = Math.max(3, Math.min(24, (band * 0.72 - 2 * (k - 1)) / k));
      const groupW = barW * k + 2 * (k - 1);
      // Values on the caps only when every one fits over its own bar; otherwise the tooltip and table carry them.
      const room = k === 1 ? Math.min(band - 6, barW + 24) : barW + 2;
      const labelled = n * k <= 12 && all.every(v => v === null || textWidth(compact(v), 11) <= room);
      spec.series.forEach((s, si) => s.values.forEach((v, i) => {
        if (v === null) return;
        const x = left + band * i + (band - groupW) / 2 + si * (barW + 2);
        const y0 = y(0), y1 = y(v);
        el("path", { d: barPath(x, Math.min(y0, y1), barW, Math.max(1, Math.abs(y1 - y0)), v >= 0 ? "top" : "bottom"), fill: color(si) }, svg);
        if (labelled) text(svg, x + barW / 2, v >= 0 ? y1 - 5 : y1 + 13, compact(v), { "text-anchor": "middle", fill: "var(--sub)" });
      }));
    } else {
      spec.series.forEach((s, si) => {
        let d = "", pen = false;
        s.values.forEach((v, i) => {
          if (v === null) { pen = false; return; }
          d += `${pen ? "L" : "M"}${left + band * (i + 0.5)},${y(v)}`; pen = true;
        });
        el("path", { d, fill: "none", stroke: color(si), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
        if (n <= 16) s.values.forEach((v, i) => {
          if (v !== null) el("circle", { cx: left + band * (i + 0.5), cy: y(v), r: 3, fill: color(si), stroke: "var(--card)", "stroke-width": 2 }, svg);
        });
      });
      // End labels — only where they don't collide; the legend and tooltip carry the rest.
      const ends = spec.series.map((s, si) => {
        let i = s.values.length - 1; while (i >= 0 && s.values[i] === null) i--;
        return i < 0 ? null : { si, i, v: s.values[i], y: y(s.values[i]) };
      }).filter(Boolean);
      ends.forEach(e => {
        el("circle", { cx: left + band * (e.i + 0.5), cy: e.y, r: 4, fill: color(e.si), stroke: "var(--card)", "stroke-width": 2 }, svg);
        if (ends.every(o => o === e || Math.abs(o.y - e.y) > 13))
          text(svg, left + band * (e.i + 0.5) + 8, e.y + 4, compact(e.v), { fill: "var(--sub)" });
      });
    }

    // Hover: one band per label shows every series' value there.
    const cross = el("line", { x1: 0, x2: 0, y1: top, y2: top + ph, stroke: "var(--chart-axis)", "stroke-width": 1, visibility: "hidden" }, svg);
    spec.labels.forEach((l, i) => {
      const cx = left + band * (i + 0.5);
      const hit = el("rect", { x: left + band * i, y: top, width: band, height: ph, fill: "transparent" }, svg);
      hit.addEventListener("mouseenter", () => {
        if (spec.type === "line") { cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible"); }
        const peak = Math.min(...spec.series.map(s => s.values[i] === null ? top + ph : y(s.values[i])));
        tip.show(cx, peak, l, spec.series.map((s, si) => ({ name: s.name, value: s.values[i], color: color(si) })), spec.unit);
      });
      hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); tip.hide(); });
    });
  }

  function rows(svg, spec, W, tip) {
    const k = spec.series.length, n = spec.labels.length;
    const thick = k === 1 ? 16 : Math.max(6, Math.floor(18 / k));
    const rowH = thick * k + 2 * (k - 1) + 12;
    const all = spec.series.flatMap(s => s.values);
    const { lo, hi } = scale(all);
    const maxLabel = Math.min(W * 0.38, Math.max(...spec.labels.map(l => textWidth(l, 12))) + 10);
    const valueW = Math.max(...all.filter(v => v !== null).map(v => textWidth(compact(v), 11))) + 10;
    const left = maxLabel, pw = W - left - valueW - 4;
    const x = v => left + ((v - lo) / (hi - lo)) * pw;
    const H = n * rowH + 6;
    svg.setAttribute("height", H); svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    el("line", { x1: x(0), x2: x(0), y1: 0, y2: H - 6, stroke: "var(--chart-axis)", "stroke-width": 1 }, svg);
    const fit = (s, width) => {
      let out = s; while (out.length > 4 && textWidth(out, 12) > width - 10) out = out.slice(0, -2);
      return out === s ? s : out.slice(0, -1) + "…";
    };
    spec.labels.forEach((l, i) => {
      const y0 = i * rowH + 6;
      text(svg, left - 8, y0 + (rowH - 12) / 2 + 4, fit(l, maxLabel), { "text-anchor": "end", fill: "var(--sub)", "font-size": 12 });
      spec.series.forEach((s, si) => {
        const v = s.values[i];
        if (v === null) return;
        const by = y0 + si * (thick + 2);
        const x0 = x(0), x1 = x(v);
        el("path", { d: barPath(Math.min(x0, x1), by, Math.max(1, Math.abs(x1 - x0)), thick, v >= 0 ? "right" : "left"), fill: color(si) }, svg);
        text(svg, v >= 0 ? x1 + 5 : x1 - 5, by + thick / 2 + 4, compact(v), { "text-anchor": v >= 0 ? "start" : "end", fill: "var(--sub)" });
      });
      const hit = el("rect", { x: 0, y: y0 - 4, width: W, height: rowH, fill: "transparent" }, svg);
      hit.addEventListener("mouseenter", () => tip.show(left, y0, l, spec.series.map((s, si) => ({ name: s.name, value: s.values[i], color: color(si) })), spec.unit));
      hit.addEventListener("mouseleave", () => tip.hide());
    });
  }

  function draw(box) {
    const spec = box._spec;
    box.replaceChildren();
    box.dir = "ltr";
    if (spec.title || spec.series.length > 1) {
      const head = document.createElement("div"); head.className = "ask-chart-head";
      if (spec.title) { const t = document.createElement("div"); t.className = "ask-chart-title"; t.textContent = spec.title + (spec.unit && spec.unit !== "%" ? ` (${spec.unit})` : spec.unit === "%" ? " (%)" : ""); head.appendChild(t); }
      if (spec.series.length > 1) {
        const legend = document.createElement("div"); legend.className = "ask-chart-legend";
        spec.series.forEach((s, si) => {
          const item = document.createElement("span");
          const key = document.createElement("span"); key.className = "ask-chart-key"; key.style.background = color(si);
          const name = document.createElement("span"); name.textContent = s.name;
          item.append(key, name); legend.appendChild(item);
        });
        head.appendChild(legend);
      }
      box.appendChild(head);
    }
    const W = Math.max(260, Math.floor(box.clientWidth || 520));
    const H = 220;
    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": spec.title || "Chart", "font-family": "inherit" });
    box.appendChild(svg);
    const tip = tooltip(box);
    if (spec.type === "hbar") rows(svg, spec, W, tip); else columns(svg, spec, W, H, tip, box);
  }

  function mount(root, specs) {
    root.querySelectorAll(".ask-chart[data-chart]").forEach(box => {
      const spec = specs[Number(box.dataset.chart)];
      if (!spec) { box.remove(); return; }
      box._spec = spec;
      draw(box);
    });
  }

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => document.querySelectorAll(".ask-chart").forEach(b => { if (b._spec) draw(b); }), 150);
  });

  window.AskChart = { extract, strip, slots, mount, validate };
})();
