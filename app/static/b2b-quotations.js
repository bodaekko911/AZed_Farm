/*
 * B2B → Quotations tab (price offers).
 *
 * The list with filters; a form for a client or a prospect with lines, delivery, terms and live totals; the margin
 * guard on every line (cost and margin are shown here only — never on the printed or e-mailed quotation); and the
 * quotation's own actions: edit while draft, duplicate, print, e-mail, mark sent / accepted / rejected, renew.
 *
 * Fixed labels are separate text nodes so i18n.js can translate them; names, notes and numbers from the database are
 * set with textContent or as input values and marked data-no-i18n.
 */
(function () {
  "use strict";
  const FREQ = { once: "One time", weekly: "Weekly", twice_weekly: "Twice a week" };
  const PER_WEEK = { once: 0, weekly: 1, twice_weekly: 2 };
  const STATUS = { draft: "Draft", sent: "Sent", accepted: "Accepted", rejected: "Rejected", expired: "Expired" };
  const TERMS = { immediate: "Payment on delivery", net15: "Within 15 days", net30: "Within 30 days",
                  net60: "Within 60 days", consignment: "Consignment" };
  let opts = null, root = null, filters = { status: "", q: "", from: "", to: "" };

  // ── small helpers ──
  function h(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const k in attrs || {}) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "class") n.className = attrs[k];
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== undefined && attrs[k] !== null && attrs[k] !== false) n.setAttribute(k, attrs[k] === true ? "" : attrs[k]);
    }
    kids.flat().forEach(k => { if (k !== null && k !== undefined && k !== false) n.append(k instanceof Node ? k : String(k)); });
    return n;
  }
  const data = (tag, text, cls) => h(tag, { class: cls, text: text == null ? "" : String(text), "data-no-i18n": true });
  const lbl = text => h("span", { text });
  const money = v => (Math.round((Number(v) || 0) * 100) / 100).toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const num = v => (v === "" || v === null || v === undefined || isNaN(Number(v))) ? null : Number(v);
  const today = () => new Date().toISOString().slice(0, 10);
  const plusDays = (d, n) => { const x = new Date(d + "T00:00:00"); x.setDate(x.getDate() + n); return x.toISOString().slice(0, 10); };

  async function api(url, body, method) {
    const r = await fetch(url, body === undefined && !method ? { credentials: "same-origin" } : {
      method: method || "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = out.detail;
      const e = new Error(typeof d === "string" ? d : (d && d.message) || "Something went wrong.");
      e.detail = d; e.status = r.status; throw e;
    }
    return out;
  }
  function toast(text) { if (typeof window.showToast === "function") window.showToast(text); else alert(text); }

  function style() {
    if (document.getElementById("quote-style")) return;
    document.head.append(h("style", { id: "quote-style" }, `
      .q-row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin:12px 0}
      .q-row .fld{min-width:150px;flex:1}
      .q-lines td,.q-lines th{padding:6px 6px;vertical-align:top}
      .q-lines input,.q-lines select{width:100%}
      .q-lines input.q-qty,.q-lines input.q-pct{max-width:80px}
      .q-lines input.q-price{max-width:100px}
      .q-guard{font-size:11.5px;margin-top:3px}
      .q-warn{color:var(--warn)} .q-bad{color:var(--danger)} .q-ok{color:var(--green)} .q-muted{color:var(--muted)}
      .q-totals{margin-left:auto;min-width:300px;font-size:13px}
      .q-totals td{padding:4px 8px} .q-totals td:last-child{text-align:right;font-weight:700}
      .q-est{font-size:12.5px;margin-top:8px} .q-est td,.q-est th{padding:3px 8px;text-align:right} .q-est td:first-child,.q-est th:first-child{text-align:left}
      .q-badge-draft{background:rgba(255,255,255,.08);color:var(--sub)} .q-badge-sent{background:rgba(77,159,255,.12);color:var(--blue)}
      .q-badge-accepted{background:rgba(0,255,157,.12);color:var(--green)} .q-badge-rejected{background:rgba(255,77,109,.12);color:var(--danger)}
      .q-badge-expired{background:rgba(255,181,71,.12);color:var(--warn)}
      .q-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
      .q-modal .modal{width:980px}
    `));
  }

  // ── the list ──
  async function open() {
    root = document.getElementById("section-quotations");
    if (!root) return;
    style();
    if (!opts) {
      try { opts = await api("/b2b/api/quotations/options"); }
      catch (e) { root.replaceChildren(h("div", { class: "q-bad", text: e.message })); return; }
    }
    const status = h("select", { onchange: e => { filters.status = e.target.value; load(); } },
      h("option", { value: "", text: "All statuses" }), ...Object.entries(STATUS).map(([v, t]) => h("option", { value: v, text: t })));
    status.value = filters.status;
    const search = h("input", { placeholder: "Client, prospect or number…", value: filters.q,
      oninput: e => { clearTimeout(search._t); search._t = setTimeout(() => { filters.q = e.target.value.trim(); load(); }, 300); } });
    const from = h("input", { type: "date", value: filters.from, onchange: e => { filters.from = e.target.value; load(); } });
    const to = h("input", { type: "date", value: filters.to, onchange: e => { filters.to = e.target.value; load(); } });
    root.replaceChildren(
      h("div", { class: "toolbar", style: "margin-bottom:12px" },
        h("div", { class: "search-box" }, search), status, lbl("From"), from, lbl("To"), to,
        h("div", { style: "flex:1" }),
        opts.can.create ? h("button", { class: "btn btn-blue", text: "Settings", onclick: openSettings }) : null,
        opts.can.create ? h("button", { class: "btn btn-green", text: "+ New quotation", onclick: () => openForm(null) }) : null),
      h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, ...["Number", "Client or prospect", "Date", "Valid until", "Status", "Total", "Weekly value", ""].map(t => h("th", { text: t })))),
        h("tbody", { id: "q-body" }, h("tr", null, h("td", { colspan: 8, class: "q-muted", text: "Loading…" }))))));
    load();
  }

  async function load() {
    const body = document.getElementById("q-body");
    const p = new URLSearchParams();
    if (filters.status) p.set("status", filters.status);
    if (filters.q) p.set("q", filters.q);
    if (filters.from) p.set("date_from", filters.from);
    if (filters.to) p.set("date_to", filters.to);
    try {
      const out = await api("/b2b/api/quotations" + (p.toString() ? "?" + p : ""));
      body.replaceChildren(...(out.quotations.length ? out.quotations.map(q => h("tr", null,
        h("td", null, data("b", q.number)),
        h("td", null, data("span", q.recipient), q.is_prospect ? h("span", { class: "badge q-badge-draft", style: "margin-left:6px", text: "Prospect" }) : null),
        h("td", null, data("span", q.quote_date)), h("td", null, data("span", q.valid_until)),
        h("td", null, h("span", { class: `badge q-badge-${q.status}`, text: STATUS[q.status] || q.status })),
        h("td", null, data("span", money(q.total))), h("td", null, data("span", q.weekly_value ? money(q.weekly_value) : "")),
        h("td", null, h("button", { class: "action-btn", text: "Open", onclick: () => openView(q.id) }))))
        : [h("tr", null, h("td", { colspan: 8, class: "q-muted", text: "No quotations yet." }))]));
    } catch (e) { body.replaceChildren(h("tr", null, h("td", { colspan: 8, class: "q-bad", text: e.message }))); }
  }

  // ── a modal ──
  function modal(title, ...content) {
    const bg = h("div", { class: "modal-bg open q-modal", onclick: e => { if (e.target === bg) bg.remove(); } },
      h("div", { class: "modal" }, h("div", { class: "modal-title", text: title }), ...content));
    document.body.append(bg);
    return bg;
  }

  // ── the form ──
  function openForm(q) {
    const editing = !!q;
    const state = {
      kind: q ? (q.is_prospect ? "prospect" : "client") : "client",
      client_id: q && q.client_id ? String(q.client_id) : "",
      lines: q ? q.lines.map(l => ({ product_id: String(l.product_id), qty: l.qty, unit_price: l.unit_price,
        line_discount_pct: l.line_discount_pct, frequency: l.frequency, note: l.note || "", check: null })) : [],
    };
    const client = () => opts.clients.find(c => String(c.id) === state.client_id);
    const field = (label, input) => h("div", { class: "fld" }, h("label", { text: label }), input);
    const kind = h("select", { onchange: e => { state.kind = e.target.value; paintWho(); refreshAll(); } },
      h("option", { value: "client", text: "An existing B2B client" }), h("option", { value: "prospect", text: "A prospect (not in AZed yet)" }));
    kind.value = state.kind;
    const clientSel = h("select", { onchange: e => { state.client_id = e.target.value; onClient(); } },
      h("option", { value: "", text: "— pick the client —" }),
      ...opts.clients.map(c => h("option", { value: String(c.id), text: c.name + (c.discount_pct ? ` (−${c.discount_pct}%)` : ""), "data-no-i18n": true })));
    clientSel.value = state.client_id;
    const p = (q && q.prospect) || {};
    const pName = h("input", { value: p.name || "", maxlength: 200, "data-no-i18n": true });
    const pContact = h("input", { value: p.contact || "", maxlength: 150, "data-no-i18n": true });
    const pPhone = h("input", { value: p.phone || "", maxlength: 50, "data-no-i18n": true });
    const pEmail = h("input", { type: "email", value: p.email || "", maxlength: 150, "data-no-i18n": true });
    const pArea = h("input", { value: p.area || "", maxlength: 120, "data-no-i18n": true });
    const who = h("div");
    function paintWho() {
      who.replaceChildren(state.kind === "client"
        ? h("div", { class: "q-row" }, field("Client", clientSel))
        : h("div", { class: "q-row" }, field("Name", pName), field("Contact person", pContact), field("Phone", pPhone),
            field("E-mail", pEmail), field("Area", pArea)));
    }
    const qDate = h("input", { type: "date", value: q ? q.quote_date : today() });
    const valid = h("input", { type: "date", value: q ? q.valid_until : plusDays(today(), opts.settings.validity_days || 14) });
    const lang = h("select", null, h("option", { value: "en", text: "English" }), h("option", { value: "ar", text: "العربية" }));
    lang.value = q ? q.language : "en";
    const area = h("input", { value: q ? q.delivery_area || "" : "", placeholder: "Dahab, Sharm El Sheikh…", maxlength: 120, "data-no-i18n": true });
    const day = h("input", { value: q ? q.delivery_day || "" : "", placeholder: "Friday", maxlength: 40, "data-no-i18n": true });
    const fee = h("input", { type: "number", min: 0, step: "any", value: q ? q.totals.delivery_fee : 0, oninput: paintTotals });
    const terms = h("select", null, h("option", { value: "", text: "— the client's terms —" }),
      ...Object.entries(TERMS).map(([v, t]) => h("option", { value: v, text: t })));
    terms.value = q ? q.payment_terms || "" : "";
    const notes = h("textarea", { rows: 2, maxlength: 2000, "data-no-i18n": true }); notes.value = q ? q.notes || "" : "";
    const termsText = h("textarea", { rows: 3, maxlength: 4000, "data-no-i18n": true });
    termsText.value = q ? q.terms || "" : (lang.value === "ar" ? opts.settings.terms_ar : opts.settings.terms_en) || "";
    lang.onchange = () => { if (!editing) termsText.value = (lang.value === "ar" ? opts.settings.terms_ar : opts.settings.terms_en) || ""; };

    const tbody = h("tbody");
    const totalsBox = h("div");
    const approveWrap = h("label", { class: "q-row", style: "display:none;gap:6px;align-items:center" });
    const approve = h("input", { type: "checkbox" });
    approveWrap.append(approve, lbl("Approve below cost (recorded with your name)"));
    const result = h("div", { class: "q-bad" });
    const belowMsg = h("div", { class: "q-bad" });

    function onClient() {
      const c = client();
      if (c && !terms.value && c.payment_terms) terms.value = c.payment_terms;
      state.lines.forEach(l => { l.unit_price = null; });      // re-price from the client's discount
      refreshAll();
    }
    function addLine(line) {
      state.lines.push(line || { product_id: "", qty: 1, unit_price: null, line_discount_pct: 0, frequency: "once", note: "", check: null });
      paintLines();
    }
    async function priceLine(l) {
      if (!l.product_id) { l.check = null; return; }
      const params = new URLSearchParams({ product_id: l.product_id, line_discount_pct: l.line_discount_pct || 0 });
      if (state.kind === "client" && state.client_id) params.set("client_id", state.client_id);
      if (l.unit_price !== null && l.unit_price !== "" && l.unit_price !== undefined) params.set("unit_price", l.unit_price);
      try {
        l.check = await api("/b2b/api/quotations/price?" + params);
        if (l.unit_price === null || l.unit_price === "" || l.unit_price === undefined) l.unit_price = l.check.default_price;
      } catch (e) { l.check = { error: e.message }; }
    }
    async function refreshAll() { for (const l of state.lines) await priceLine(l); paintLines(); }

    function guardText(c) {
      if (!c) return null;
      if (c.error) return h("div", { class: "q-guard q-bad" }, data("span", c.error));
      const parts = [];
      if (c.effective_discount_pct) parts.push(h("span", { class: "q-muted" }, lbl("Discount vs catalogue:"), " ", data("span", `${c.effective_discount_pct}%`), " · "));
      if (c.no_cost) parts.push(h("span", { class: "q-warn", text: "No cost recorded for this product" }));
      else {
        parts.push(h("span", { class: "q-muted" }, lbl("Cost"), " ", data("span", money(c.cost)), " · ", lbl("Margin"), " ", data("span", c.margin_pct + "%")));
        if (c.below_cost) parts.push(" ", h("span", { class: "q-bad", text: "Below cost" }));
        else if (c.below_target) parts.push(" ", h("span", { class: "q-warn" }, lbl("Below the target margin of"), " ", data("span", opts.settings.target_margin_pct + "%")));
      }
      return h("div", { class: "q-guard" }, ...parts);
    }
    function paintLines() {
      tbody.replaceChildren(...state.lines.map((l, i) => {
        const product = opts.products.find(p => String(p.id) === l.product_id);
        const pick = h("select", { onchange: async e => { l.product_id = e.target.value; l.unit_price = null; await priceLine(l); paintLines(); } },
          h("option", { value: "", text: "— pick a product —" }),
          ...opts.products.map(p => h("option", { value: String(p.id), text: `${p.name} · ${p.unit}`, "data-no-i18n": true })));
        pick.value = l.product_id;
        const qty = h("input", { class: "q-qty", type: "number", min: 0, step: "any", value: l.qty, oninput: e => { l.qty = e.target.value; paintTotals(); } });
        const priceIn = h("input", { class: "q-price", type: "number", min: 0, step: "any", value: l.unit_price ?? "",
          onchange: async e => { l.unit_price = e.target.value; await priceLine(l); paintLines(); } });
        const pct = h("input", { class: "q-pct", type: "number", min: 0, max: 100, step: "any", value: l.line_discount_pct || 0,
          onchange: async e => { l.line_discount_pct = e.target.value; await priceLine(l); paintLines(); } });
        const freq = h("select", { onchange: e => { l.frequency = e.target.value; paintTotals(); } },
          ...Object.entries(FREQ).map(([v, t]) => h("option", { value: v, text: t })));
        freq.value = l.frequency;
        const note = h("input", { value: l.note || "", maxlength: 200, placeholder: "e.g. mixed vegetables", "data-no-i18n": true, oninput: e => { l.note = e.target.value; } });
        return h("tr", null,
          h("td", { style: "min-width:220px" }, pick, guardText(l.check)),
          h("td", null, qty, data("div", product ? product.unit : "", "q-muted")),
          h("td", null, priceIn), h("td", null, pct), h("td", null, freq), h("td", null, note),
          h("td", null, data("span", money(lineTotal(l)))),
          h("td", null, h("button", { class: "action-btn danger", type: "button", text: "✕", onclick: () => { state.lines.splice(i, 1); paintLines(); } })));
      }));
      paintTotals();
    }
    const lineTotal = l => (num(l.qty) || 0) * (num(l.unit_price) || 0) * (1 - (num(l.line_discount_pct) || 0) / 100);
    function paintTotals() {
      let sub = 0, after = 0, weekly = 0, perWeek = 0;
      state.lines.forEach(l => {
        const gross = (num(l.qty) || 0) * (num(l.unit_price) || 0), net = lineTotal(l);
        sub += gross; after += net; weekly += net * PER_WEEK[l.frequency]; perWeek = Math.max(perWeek, PER_WEEK[l.frequency]);
      });
      const f = num(fee.value) || 0, wDel = f * perWeek, monthly = x => x * 52 / 12;
      const rows = [["Subtotal", money(sub)], ["Discounts", money(sub - after)],
                    ["Delivery fee", f ? money(f) : "Delivery included"], ["Total", money(after + f)]];
      totalsBox.replaceChildren(h("table", { class: "q-totals" }, ...rows.map(([a, b]) => h("tr", null, h("td", { text: a }),
        h("td", null, b === "Delivery included" ? lbl(b) : data("span", b))))),
        perWeek ? h("table", { class: "q-est" },
          h("tr", null, h("th", { text: "Estimated regular supply" }), h("th", { text: "Per week" }), h("th", { text: "Per month" })),
          h("tr", null, h("td", { text: "Produce" }), h("td", null, data("span", money(weekly))), h("td", null, data("span", money(monthly(weekly))))),
          h("tr", null, h("td", { text: "Delivery" }), ...(wDel ? [h("td", null, data("span", money(wDel))), h("td", null, data("span", money(monthly(wDel))))]
                                                              : [h("td", { text: "Delivery included" }), h("td", { text: "Delivery included" })])),
          h("tr", null, h("th", { text: "Total" }), h("th", null, data("span", money(weekly + wDel))), h("th", null, data("span", money(monthly(weekly + wDel))))))
          : null);
      const below = state.lines.some(l => l.check && l.check.below_cost);
      approveWrap.style.display = below && opts.can.below_cost ? "flex" : "none";
      if (!below) approve.checked = false;
      belowMsg.replaceChildren(...(below && !opts.can.below_cost
        ? [lbl("A line is priced below cost. Raise the price; only an admin can approve a price below cost.")] : []));
    }

    async function save() {
      result.replaceChildren();
      const body = {
        client_id: state.kind === "client" ? Number(state.client_id) || null : null,
        prospect_name: state.kind === "prospect" ? pName.value : null, prospect_contact: pContact.value,
        prospect_phone: pPhone.value, prospect_email: pEmail.value, prospect_area: pArea.value,
        quote_date: qDate.value, valid_until: valid.value, language: lang.value, delivery_area: area.value,
        delivery_day: day.value, delivery_fee: num(fee.value) || 0, payment_terms: terms.value || null,
        notes: notes.value, terms: termsText.value, approve_below_cost: approve.checked,
        lines: state.lines.map(l => ({ product_id: Number(l.product_id) || null, qty: num(l.qty), unit_price: num(l.unit_price),
                                       line_discount_pct: num(l.line_discount_pct) || 0, frequency: l.frequency, note: l.note })),
      };
      try {
        const out = await api(editing ? `/b2b/api/quotations/${q.id}` : "/b2b/api/quotations", body, editing ? "PUT" : "POST");
        bg.remove(); toast(`${out.number} saved`); load(); openView(out.id);
      } catch (e) { result.replaceChildren(data("span", e.message)); }
    }

    const table = h("table", { class: "q-lines", style: "width:100%" },
      h("thead", null, h("tr", null, ...["Product", "Quantity", "Unit price", "Discount %", "Frequency", "Note", "Amount", ""].map(t => h("th", { text: t })))), tbody);
    const bg = modal(editing ? `Edit ${q.number}` : "New quotation",
      h("div", { class: "modal-sub", text: "Prices start from the catalogue, less the client's discount. Cost and margin are shown here only, never on the quotation." }),
      h("div", { class: "q-row" }, field("For", kind), field("Quotation date", qDate), field("Valid until", valid), field("Language of the quotation", lang)),
      who, table,
      h("button", { class: "action-btn green", type: "button", text: "+ Add a product", onclick: () => addLine() }),
      h("div", { class: "q-row" }, field("Delivery area", area), field("Delivery day", day), field("Delivery fee (0 = included)", fee), field("Payment terms", terms)),
      h("div", { class: "q-row" }, field("Notes", notes)), h("div", { class: "q-row" }, field("Terms and conditions", termsText)),
      totalsBox, approveWrap, belowMsg, result,
      h("div", { class: "modal-actions" }, h("button", { class: "btn-cancel", text: "Cancel", onclick: () => bg.remove() }),
        h("button", { class: "btn btn-green", text: editing ? "Save changes" : "Save as draft", onclick: save })));
    paintWho();
    if (!state.lines.length) addLine(); else refreshAll();
  }

  // ── one quotation ──
  async function openView(id) {
    let q;
    try { q = await api(`/b2b/api/quotations/${id}`); } catch (e) { toast(e.message); return; }
    const t = q.totals;
    const rows = q.lines.map(l => h("tr", null,
      h("td", null, data("div", l.product), l.note ? data("div", l.note, "q-muted") : null,
        l.no_cost ? h("div", { class: "q-guard q-warn", text: "No cost recorded" }) : h("div", { class: "q-guard q-muted" },
          lbl("Cost"), " ", data("span", money(l.cost)), " · ", lbl("Margin"), " ", data("span", `${l.margin_pct}%`),
          l.below_cost ? h("span", { class: "q-bad", text: " Below cost" }) : l.below_target ? h("span", { class: "q-warn", text: " Below target" }) : null)),
      h("td", null, data("span", `${l.qty} ${l.unit}`)), h("td", null, data("span", money(l.unit_price))),
      h("td", null, data("span", l.line_discount_pct ? `${l.line_discount_pct}%` : "")),
      h("td", null, data("span", l.effective_discount_pct ? `${l.effective_discount_pct}%` : "")),
      h("td", { text: FREQ[l.frequency] }), h("td", null, data("span", money(l.line_total)))));
    const can = opts.can, act = [];
    const btn = (text, onclick, cls) => h("button", { class: "btn " + (cls || "btn-blue"), text, onclick });
    act.push(btn("Print", () => window.open(`/b2b/quotation/${q.id}/print`, "_blank")));
    if (can.create && q.status === "draft") act.push(btn("Edit", () => { bg.remove(); openForm(q); }));
    if (can.create) act.push(btn("Duplicate", async () => {
      try { const out = await api(`/b2b/api/quotations/${q.id}/duplicate`, {}); bg.remove(); load(); openView(out.id); }
      catch (e) { toast(e.message); } }));
    if (can.send && (q.status === "draft" || q.status === "sent")) {
      act.push(btn("E-mail it", async () => {
        if (!q.email) { toast("The recipient has no e-mail address."); return; }
        if (!confirm(`Send ${q.number} to ${q.email}?`)) return;
        try { const out = await api(`/b2b/api/quotations/${q.id}/email`, {}); toast(out.message); bg.remove(); load(); openView(q.id); }
        catch (e) { toast(e.message); } }, "btn-green"));
      if (q.status === "draft") act.push(btn("Mark as sent", () => setStatus("sent")));
      act.push(btn("Accepted", () => setStatus("accepted"), "btn-green"));
      act.push(btn("Rejected", () => { const r = prompt("Why was it rejected?"); if (r) setStatus("rejected", r); }, "btn-danger"));
    }
    if (can.convert && q.status === "accepted" && !q.is_prospect && (!q.invoices.length || q.has_recurring)) {
      const label = !q.invoices.length ? (q.has_recurring ? "Create the first delivery's invoice" : "Create the invoice")
                                       : "Create the next delivery's invoice";
      act.push(btn(label, () => convert(false), "btn-green"));
    }
    if (q.is_prospect && can.create) act.push(btn("Make a B2B client", () => makeClient(null), "btn-teal"));
    async function convert(skipDelivery) {
      if (!skipDelivery && !confirm("Create a B2B invoice now? Stock is checked and taken out as with any invoice.")) return;
      try {
        const out = await api(`/b2b/api/quotations/${q.id}/invoice`, { skip_delivery: skipDelivery });
        toast(`${out.invoice_number} created`);
        const notes = [];
        if (out.rounding_difference) notes.push(`Rounding difference: ${out.rounding_difference} EGP`);
        if (out.note) notes.push(out.note);
        if (notes.length) alert(notes.join("\n"));
        bg.remove(); load(); openView(q.id);
      } catch (e) {
        if (e.detail && e.detail.needs_delivery_item) {
          if (confirm(e.message + "\n\nCreate the invoice without the delivery fee?")) convert(true);
        } else toast(e.message);
      }
    }
    async function makeClient(linkId) {
      if (!linkId && !confirm("Add this prospect to B2B as a client? The quotation then belongs to the client.")) return;
      try {
        const out = await api(`/b2b/api/quotations/${q.id}/client`, { link_client_id: linkId });
        toast(out.created ? `${out.client} added as a B2B client` : `Linked to ${out.client}`);
        opts = null; await open(); openView(q.id);
        bg.remove();
      } catch (e) {
        const found = e.detail && e.detail.existing;
        if (found && found.length && confirm(e.message + "\n\n" + found[0].name)) makeClient(found[0].id);
        else if (!found) toast(e.message);
      }
    }
    if (can.create && q.status === "expired") act.push(btn("Renew", async () => {
      const until = prompt("New valid until date (YYYY-MM-DD)", plusDays(today(), opts.settings.validity_days || 14));
      if (!until) return;
      try { await api(`/b2b/api/quotations/${q.id}/renew`, { valid_until: until }); toast("Renewed"); bg.remove(); load(); openView(q.id); }
      catch (e) {
        if (e.detail && e.detail.below_cost && can.below_cost && confirm(e.message + "\n\nApprove below cost and renew?")) {
          try { await api(`/b2b/api/quotations/${q.id}/renew`, { valid_until: until, approve_below_cost: true }); bg.remove(); load(); openView(q.id); }
          catch (e2) { toast(e2.message); }
        } else toast(e.message);
      } }, "btn-warn"));
    async function setStatus(status, reason) {
      try { await api(`/b2b/api/quotations/${q.id}/status`, { status, reason }); bg.remove(); load(); openView(q.id); }
      catch (e) { toast(e.message); }
    }
    const est = t.deliveries_per_week ? h("table", { class: "q-est" },
      h("tr", null, h("th", { text: "Estimated regular supply" }), h("th", { text: "Per week" }), h("th", { text: "Per month" })),
      h("tr", null, h("td", { text: "Produce" }), h("td", null, data("span", money(t.weekly_produce))), h("td", null, data("span", money(t.monthly_produce)))),
      h("tr", null, h("td", { text: "Delivery" }), h("td", null, data("span", money(t.weekly_delivery))), h("td", null, data("span", money(t.monthly_delivery)))),
      h("tr", null, h("th", { text: "Total" }), h("th", null, data("span", money(t.weekly_value))), h("th", null, data("span", money(t.monthly_value))))) : null;
    const bg = modal(`${q.number}`,
      h("div", { class: "modal-sub" }, h("span", { class: `badge q-badge-${q.status}`, text: STATUS[q.status] }), " ", data("b", q.recipient),
        q.is_prospect ? h("span", { class: "q-muted", text: " (prospect)" }) : null),
      h("div", { class: "q-row q-muted" }, lbl("Date"), data("span", q.quote_date), lbl("Valid until"), data("span", q.valid_until),
        q.delivery_area ? [lbl("Delivery"), data("span", `${q.delivery_area}${q.delivery_day ? ", " + q.delivery_day : ""}`)] : null,
        q.email ? [lbl("E-mail"), data("span", q.email)] : null),
      q.reject_reason ? h("div", { class: "q-bad" }, lbl("Rejected:"), " ", data("span", q.reject_reason)) : null,
      q.below_cost_approved ? h("div", { class: "q-warn", text: "A price below cost was approved on this quotation." }) : null,
      h("table", { class: "q-lines", style: "width:100%" }, h("thead", null, h("tr", null,
        ...["Product", "Quantity", "Unit price", "Line discount", "Discount vs catalogue", "Frequency", "Amount"].map(x => h("th", { text: x })))), h("tbody", null, rows)),
      h("table", { class: "q-totals" },
        h("tr", null, h("td", { text: "Subtotal" }), h("td", null, data("span", money(t.subtotal)))),
        h("tr", null, h("td", { text: "Discounts" }), h("td", null, data("span", money(t.discount_total)))),
        h("tr", null, h("td", { text: "Delivery fee" }), h("td", null, t.delivery_fee ? data("span", money(t.delivery_fee)) : lbl("Delivery included"))),
        h("tr", null, h("td", { text: "Total" }), h("td", null, data("span", money(t.total))))),
      est,
      q.invoices.length ? h("div", null, h("div", { class: "modal-title", style: "font-size:14px;margin-top:14px", text: "Invoices from this quotation" }),
        ...q.invoices.map(i => h("div", null, h("a", { href: `/b2b/invoice/${i.id}/print`, target: "_blank", rel: "noopener", "data-no-i18n": true, text: i.invoice_number }),
          " ", data("span", money(i.total)), " ", data("span", (i.created_at || "").slice(0, 10), "q-muted")))) : null,
      h("div", { class: "q-actions" }, ...act));
  }

  // ── settings ──
  function openSettings() {
    const s = opts.settings;
    const margin = h("input", { type: "number", min: 0, max: 95, value: s.target_margin_pct });
    const days = h("input", { type: "number", min: 1, max: 180, value: s.validity_days });
    const en = h("textarea", { rows: 4, "data-no-i18n": true }); en.value = s.terms_en || "";
    const ar = h("textarea", { rows: 4, dir: "rtl", "data-no-i18n": true }); ar.value = s.terms_ar || "";
    const out = h("div", { class: "q-bad" });
    const bg = modal("Quotation settings",
      h("div", { class: "q-row" }, h("div", { class: "fld" }, h("label", { text: "Target margin %" }), margin),
        h("div", { class: "fld" }, h("label", { text: "Valid for (days)" }), days)),
      h("div", { class: "fld" }, h("label", { text: "Default terms (English)" }), en),
      h("div", { class: "fld" }, h("label", { text: "Default terms (Arabic)" }), ar), out,
      h("div", { class: "modal-actions" }, h("button", { class: "btn-cancel", text: "Cancel", onclick: () => bg.remove() }),
        h("button", { class: "btn btn-green", text: "Save", onclick: async () => {
          try {
            opts.settings = await api("/b2b/api/quotations/settings", { target_margin_pct: Number(margin.value) || 30,
              validity_days: Number(days.value) || 14, terms_en: en.value, terms_ar: ar.value });
            bg.remove(); toast("Saved");
          } catch (e) { out.replaceChildren(data("span", e.message)); }
        } })));
  }

  window.B2BQuotations = { open };
})();
