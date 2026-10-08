/*
 * Ask — confirm actions, and record sales invoices from a PDF.
 *
 * AskActions.cards(bubble, proposals)  — a Confirm / Cancel card per proposed action. Nothing happens
 *                                         until Confirm, which posts the signed token to the server.
 * AskActions.pdf(file, {log, toast, onLimit}) — renders the PDF's pages to images IN THE BROWSER (pdf.js),
 *                                         sends them to be read, then shows one review card per invoice:
 *                                         customer, products, quantities and prices can be fixed, and an
 *                                         invoice can only be recorded when its total matches the PDF.
 *
 * Everything that came from the PDF or the model is set with textContent — never as HTML.
 */
(function () {
  "use strict";
  const PDFJS = "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.min.js";
  const PDFJS_WORKER = "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js";
  const MAX_PAGES = 10, MAX_WIDTH = 1400, JPEG_QUALITY = 0.72;

  // ── tiny DOM helper ──
  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const k in attrs || {}) {
      if (k === "class") node.className = attrs[k];
      else if (k === "text") node.textContent = attrs[k];
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== undefined && attrs[k] !== null && attrs[k] !== false) node.setAttribute(k, attrs[k] === true ? "" : attrs[k]);
    }
    kids.flat().forEach(k => { if (k !== null && k !== undefined && k !== false) node.append(k instanceof Node ? k : String(k)); });
    return node;
  }
  const money = v => (Math.round((Number(v) || 0) * 100) / 100).toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const round2 = v => Math.round((Number(v) + Number.EPSILON) * 100) / 100;
  // ── Pack sizes: "Tomato (500g)" on the PDF, "Tomato (1g)" in Azed ──
  const fmtQty = v => String(Math.round(v * 1000) / 1000);
  const packText = p => p.amount >= 1000 ? `${fmtQty(p.amount / 1000)} ${p.base === "g" ? "kg" : "l"}` : `${fmtQty(p.amount)} ${p.base}`;
  /** Quantity and price in the product's own unit, from what the PDF says (packs × pack size). */
  function inProductUnits(l) {
    const p = l.product, from = l.pdfPack, to = p && p.pack;
    let qty = l.pdfQty, price = l.pdfPrice, note = "";
    if (from && to && from.base === to.base && Math.abs(from.amount - to.amount) > 1e-9 && qty !== "" && qty !== null) {
      const factor = from.amount / to.amount;
      qty = Math.round(Number(qty) * factor * 1000) / 1000;
      if (price !== "" && price !== null) price = Number(price) / factor;
      note = `${fmtQty(l.pdfQty)} × ${packText(from)} = ${fmtQty(Number(l.pdfQty) * from.amount)} ${from.base}` +
             (price !== "" && price !== null ? ` · ${money(l.pdfPrice)} each → ${Number(price).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")} per ${packText(to)}` : "");
    }
    if ((price === "" || price === null) && p) price = p.price;
    // Within rounding of the catalogue price → the catalogue price (the POS compares them exactly).
    if (p && price !== "" && price !== null && Math.abs(Number(price) - p.price) < 0.0005) price = p.price;
    return { qty: qty ?? "", price: price ?? "", note };
  }

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(typeof data.detail === "string" ? data.detail : "Something went wrong."); e.status = r.status; throw e; }
    return data;
  }

  // ── Action cards ──
  function cards(bubble, proposals) {
    (proposals || []).forEach(p => {
      const rows = (p.lines || []).map(([k, v]) => h("tr", null, h("th", { text: k }), h("td", { text: v })));
      const status = h("div", { class: "act-status" });
      const confirm = h("button", { class: "act-btn act-primary", text: "Confirm" });
      const cancel = h("button", { class: "act-btn", text: "Cancel" });
      const card = h("div", { class: "act-card" }, h("div", { class: "act-title", text: p.title }),
        h("table", { class: "act-table" }, h("tbody", null, rows)), h("div", { class: "act-buttons" }, confirm, cancel), status);
      confirm.onclick = async () => {
        confirm.disabled = cancel.disabled = true; status.textContent = "Saving…"; status.className = "act-status";
        try {
          const out = await post("/assistant/api/actions/confirm", { token: p.token });
          status.textContent = "✓ " + out.message; status.className = "act-status ok";
          confirm.remove(); cancel.remove();
        } catch (e) {
          status.textContent = e.message; status.className = "act-status bad";
          if (e.status !== 409 && e.status !== 410) confirm.disabled = cancel.disabled = false;
        }
      };
      cancel.onclick = () => { confirm.remove(); cancel.remove(); status.textContent = "Cancelled — nothing was changed."; };
      bubble.appendChild(card);
    });
  }

  // ── PDF → page images (in the browser) ──
  function loadPdfJs() {
    if (window.pdfjsLib) return Promise.resolve(window.pdfjsLib);
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = PDFJS; s.onload = () => {
        window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER;
        resolve(window.pdfjsLib);
      };
      s.onerror = () => reject(new Error("Couldn't load the PDF reader. Check the connection."));
      document.head.appendChild(s);
    });
  }
  async function renderPages(file, onProgress) {
    const pdfjsLib = await loadPdfJs();
    const pdf = await pdfjsLib.getDocument({ data: await file.arrayBuffer(), isEvalSupported: false }).promise;
    if (pdf.numPages > MAX_PAGES) throw new Error(`That PDF has ${pdf.numPages} pages — send at most ${MAX_PAGES} at a time.`);
    const pages = [];
    for (let i = 1; i <= pdf.numPages; i++) {
      onProgress && onProgress(i, pdf.numPages);
      const page = await pdf.getPage(i);
      const base = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: Math.min(2, MAX_WIDTH / base.width) });
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(viewport.width); canvas.height = Math.floor(viewport.height);
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
      await page.render({ canvasContext: ctx, viewport }).promise;
      pages.push(canvas.toDataURL("image/jpeg", JPEG_QUALITY));
      canvas.width = canvas.height = 0;        // free the canvas memory right away
      page.cleanup();
    }
    pdf.destroy();
    return pages;
  }

  // ── Picker: a select of likely matches, plus search ──
  function picker(kind, current, candidates, allowWalkIn, onChange, extra) {
    const wrap = h("div", { class: "inv-picker" });
    const select = h("select", { class: "inv-select" });
    const known = new Map();
    const add = (item, selected) => {
      if (known.has(String(item.id))) { if (selected) select.value = String(item.id); return; }
      known.set(String(item.id), item);
      const label = kind === "customers" ? item.name + (item.phone ? ` · ${item.phone}` : "")
                                         : `${item.name} · ${item.sku} · ${money(item.price)}`;
      const opt = h("option", { value: String(item.id), text: label });
      select.insertBefore(opt, select.querySelector('option[value="__search"]'));
      if (selected) select.value = String(item.id);
    };
    if (allowWalkIn) select.append(h("option", { value: "", text: "Walk-in (no named customer)" }));
    else select.append(h("option", { value: "", text: "— pick a product —" }));
    if (extra) select.append(h("option", { value: "__new", text: extra.label, disabled: extra.disabled }));
    select.append(h("option", { value: "__search", text: "🔍 Search…" }));
    if (current) add(current, true);
    (candidates || []).forEach(c => add(c, false));
    if (!current) select.value = extra && extra.selected ? "__new" : "";
    const search = h("input", { class: "inv-search", type: "search", placeholder: kind === "customers" ? "Name or phone…" : "Product name or SKU…", hidden: true });
    const results = h("div", { class: "inv-results", hidden: true });
    let last = select.value, timer = null;
    select.onchange = () => {
      if (select.value === "__search") {
        select.value = last; search.hidden = false; results.hidden = false; search.focus(); return;
      }
      last = select.value; onChange(select.value === "__new" ? "__new" : (known.get(select.value) || null));
    };
    search.oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        results.replaceChildren();
        if (search.value.trim().length < 2) return;
        try {
          const r = await fetch(`/assistant/api/invoices/search?kind=${kind}&q=${encodeURIComponent(search.value.trim())}`);
          const data = await r.json();
          (data.results || []).forEach(item => results.append(h("button", {
            class: "inv-result", type: "button",
            text: kind === "customers" ? item.name + (item.phone ? ` · ${item.phone}` : "") : `${item.name} · ${item.sku} · ${money(item.price)}`,
            onclick: () => { add(item, true); last = select.value; search.hidden = results.hidden = true; search.value = ""; results.replaceChildren(); onChange(item); },
          })));
          if (!(data.results || []).length) results.append(h("div", { class: "inv-muted", text: "No matches." }));
        } catch (e) { /* ignore */ }
      }, 250);
    };
    wrap.append(select, search, results);
    return wrap;
  }

  // ── One invoice to review ──
  function invoiceCard(inv, filename, toast, canCreateCustomers) {
    const state = {
      customer: inv.customer_match || null,
      newCustomer: inv.new_customer ? Object.assign({}, inv.new_customer) : null,
      useNew: !!(inv.new_customer && !inv.customer_match && canCreateCustomers),
      date: inv.date || "",
      paid: inv.paid !== false,
      discount: Number(inv.discount) || 0,
      pdfTotal: inv.total,
      lines: inv.lines.map(l => {
        const line = { description: l.description, product: l.product, candidates: l.candidates, match: l.match,
                       delivery: !!l.delivery,
                       pdfQty: l.qty ?? "", pdfPrice: l.unit_price ?? "", pdfPack: l.pack, pdfLine: l.line_total };
        return Object.assign(line, inProductUnits(line));
      }),
      recorded: false, force: false,
    };
    const card = h("div", { class: "inv-card" });
    const totalBox = h("div", { class: "inv-total" });
    const problems = h("ul", { class: "inv-problems" });
    const result = h("div", { class: "act-status" });
    const recordBtn = h("button", { class: "act-btn act-primary", text: "Record invoice" });

    function check() {
      const issues = [];
      let subtotal = 0;
      const qtyByProduct = new Map();
      state.lines.forEach((l, i) => {
        const n = i + 1, q = Number(l.qty), p = Number(l.price);
        if (!l.product) issues.push(`Line ${n}: pick a product.`);
        if (!(q > 0)) issues.push(`Line ${n}: quantity must be above 0.`);
        if (l.price === "" || !(p >= 0)) issues.push(`Line ${n}: price is missing.`);
        subtotal += (q || 0) * (p || 0);
        if (l.product) {
          if ((state.customer || state.useNew) && Math.abs(p - l.product.price) > 0.0001)
            issues.push(`Line ${n}: ${l.product.name} is ${money(l.product.price)} in the catalogue — the POS doesn't allow other prices for a named customer. Use the catalogue price or record it as Walk-in.`);
          if (l.product.tracked) qtyByProduct.set(l.product.id, { p: l.product, q: (qtyByProduct.get(l.product.id)?.q || 0) + (q || 0) });
        }
      });
      qtyByProduct.forEach(({ p, q }) => { if (q > p.stock + 1e-9) issues.push(`Only ${p.stock} ${p.unit} of ${p.name} in stock (needs ${q}).`); });
      const pct = state.discount > 0 && subtotal > 0 ? Math.min(100, state.discount / subtotal * 100) : 0;
      const total = round2(subtotal - subtotal * pct / 100);
      const pdfTotal = state.pdfTotal === null || state.pdfTotal === "" || state.pdfTotal === undefined ? null : round2(state.pdfTotal);
      totalBox.replaceChildren();
      if (pdfTotal === null) {
        issues.push("The PDF total couldn't be read — type it in.");
        totalBox.append(h("span", { text: `Our total ${money(total)}` }));
      } else if (Math.abs(total - pdfTotal) <= 0.01) {
        totalBox.append(h("span", { class: "inv-ok", text: `✅ ${money(total)} matches the PDF` }));
      } else {
        issues.push(`Total ${money(total)} doesn't match the PDF (${money(pdfTotal)}) — off by ${money(total - pdfTotal)}.`);
        totalBox.append(h("span", { class: "inv-bad", text: `❌ ${money(total)} vs PDF ${money(pdfTotal)}` }));
      }
      if (!state.date) issues.push("Set the invoice date.");
      if (state.useNew && !(state.newCustomer.name || "").trim()) issues.push("Give the new customer a name.");
      if (inv.duplicate && !state.force) issues.push(inv.duplicate + ".");
      problems.replaceChildren(...issues.map(t => h("li", { text: t })));
      recordBtn.disabled = state.recorded || issues.length > 0;
      card.classList.toggle("ready", !issues.length && !state.recorded);
      return !issues.length;
    }

    // header
    const dateInput = h("input", { type: "date", value: state.date, max: new Date().toISOString().slice(0, 10), oninput: e => { state.date = e.target.value; check(); } });
    const paidSelect = h("select", { onchange: e => { state.paid = e.target.value === "paid"; check(); } },
      h("option", { value: "paid", text: "Paid" }), h("option", { value: "unpaid", text: "Unpaid (settle later)" }));
    paidSelect.value = state.paid ? "paid" : "unpaid";
    const head = h("div", { class: "inv-head" },
      h("div", { class: "inv-title", text: `PDF invoice ${inv.number || "(no number)"}` }),
      h("label", null, "Date ", dateInput), h("label", null, paidSelect));
    // A customer the invoice names but Azed doesn't have: created with these details when recorded.
    const newBox = h("div", { class: "inv-new", hidden: !state.useNew });
    if (state.newCustomer) {
      const field = (key, label, type) => h("label", null, label, h("input", {
        type: type || "text", value: state.newCustomer[key] || "", maxlength: key === "address" ? 300 : 150,
        oninput: e => { state.newCustomer[key] = e.target.value; check(); } }));
      newBox.append(h("div", { class: "inv-muted", text: "Not in Azed yet — it will be added with these details when you record:" }),
        h("div", { class: "inv-new-fields" }, field("name", "Name"), field("phone", "Phone", "tel"),
          field("email", "Email", "email"), field("address", "Address")));
    }
    const customerExtra = state.newCustomer ? {
      label: canCreateCustomers ? `➕ New customer: ${state.newCustomer.name}` : `➕ New customer: ${state.newCustomer.name} (you can't add customers)`,
      disabled: !canCreateCustomers, selected: state.useNew } : null;
    const customerRow = h("div", { class: "inv-row" }, h("span", { class: "inv-label", text: "Customer" }),
      inv.customer ? h("span", { class: "inv-muted", text: `on PDF: ${inv.customer}` }) : null,
      picker("customers", state.customer, inv.customer_candidates, true, c => {
        state.useNew = c === "__new"; state.customer = state.useNew ? null : c;
        newBox.hidden = !state.useNew; check();
      }, customerExtra));

    // lines
    const tbody = h("tbody");
    state.lines.forEach(l => {
      const lineTotal = h("td", { class: "num" });
      const paint = () => { lineTotal.textContent = money((Number(l.qty) || 0) * (Number(l.price) || 0)); };
      const qty = h("input", { type: "number", step: "any", min: "0", value: l.qty, oninput: e => { l.qty = e.target.value; paint(); check(); } });
      const price = h("input", { type: "number", step: "any", min: "0", value: l.price, oninput: e => { l.price = e.target.value; paint(); check(); } });
      const hint = h("div", { class: "inv-hint", text: l.match === "closest" && l.product ? "Closest match by name — check it"
                                                      : l.delivery && !l.product ? "Shipping — pick the delivery area" : "" });
      const conv = h("div", { class: "inv-conv", text: l.note || (l.match === "delivery" && l.product ? "Delivery item chosen from the address" : "") });
      const pick = picker("products", l.product, l.candidates, false, p => {
        l.product = p;
        hint.textContent = "";
        // A different product may count in a different size: redo the conversion from the PDF's figures.
        Object.assign(l, inProductUnits(l));
        qty.value = l.qty; price.value = l.price; conv.textContent = l.note;
        paint(); check();
      });
      paint();
      tbody.append(h("tr", null, h("td", { class: "inv-desc", text: l.description || "—" }), h("td", null, pick, hint, conv),
        h("td", null, qty), h("td", null, price), lineTotal));
    });
    const table = h("table", { class: "inv-table" },
      h("thead", null, h("tr", null, ["On the PDF", "Product", "Qty", "Price", "Line"].map(t => h("th", { text: t })))), tbody);

    const discount = h("input", { type: "number", step: "any", min: "0", value: state.discount, oninput: e => { state.discount = Number(e.target.value) || 0; check(); } });
    const pdfTotal = h("input", { type: "number", step: "any", value: state.pdfTotal ?? "", oninput: e => { state.pdfTotal = e.target.value === "" ? null : Number(e.target.value); check(); } });
    const foot = h("div", { class: "inv-foot" },
      h("label", null, "Discount ", discount), h("label", null, "PDF total ", pdfTotal), totalBox);

    const buttons = h("div", { class: "act-buttons" }, recordBtn);
    if (inv.duplicate) buttons.append(h("button", { class: "act-btn", text: "Record anyway", onclick: e => { state.force = true; e.target.remove(); check(); } }));
    recordBtn.onclick = async () => {
      if (!check()) return;
      recordBtn.disabled = true; result.textContent = "Recording…"; result.className = "act-status";
      try {
        const out = await post("/assistant/api/invoices/record", {
          number: inv.number, date: state.date, customer_id: state.customer ? state.customer.id : null,
          new_customer: state.useNew ? state.newCustomer : null,
          paid: state.paid, discount: state.discount, pdf_total: state.pdfTotal, filename, force: state.force,
          items: state.lines.map(l => ({ product_id: l.product ? l.product.id : null, qty: Number(l.qty), unit_price: Number(l.price) })),
        });
        state.recorded = true;
        result.textContent = `✓ Recorded as ${out.invoice_number} (${money(out.total)}, ${out.date})`;
        result.className = "act-status ok";
        card.classList.add("done"); card.querySelectorAll("input, select, button").forEach(x => { x.disabled = true; });
      } catch (e) {
        result.textContent = e.message; result.className = "act-status bad"; check();
      }
    };

    card.append(head, customerRow, newBox, table, foot, problems, buttons, result);
    card._record = () => recordBtn.disabled ? null : recordBtn.onclick();
    check();
    return card;
  }

  async function pdf(file, opts) {
    const { log, toast } = opts;
    const bubble = h("div", { class: "bubble inv-bubble" });
    const msg = h("div", { class: "msg bot" }, bubble);
    const status = h("div", { class: "inv-muted", text: "Opening the PDF…" });
    bubble.append(h("div", { class: "inv-file", text: `📎 ${file.name}` }), status);
    log.appendChild(msg); msg.scrollIntoView({ behavior: "smooth", block: "end" });
    try {
      if (file.type && file.type !== "application/pdf") throw new Error("Choose a PDF file.");
      const pages = await renderPages(file, (i, n) => { status.textContent = `Preparing page ${i} of ${n}…`; });
      status.textContent = `Reading ${pages.length} page${pages.length > 1 ? "s" : ""}… this can take a minute.`;
      const data = await post("/assistant/api/invoices/read", { filename: file.name, pages });
      opts.onLimit && opts.onLimit(data.questions_left, data.daily_limit);
      if (!data.invoices.length) { status.textContent = "No invoices were found in that PDF."; return; }
      const took = data.timing ? ` (read in ${data.timing.seconds}s)` : "";
      status.textContent = `Found ${data.invoices.length} invoice${data.invoices.length > 1 ? "s" : ""}${took}. Check each one — only invoices whose total matches the PDF can be recorded.`;
      const cardsEls = data.invoices.map(inv => invoiceCard(inv, data.filename, toast, !!data.can_create_customers));
      bubble.append(...cardsEls);
      if (cardsEls.length > 1) {
        bubble.append(h("div", { class: "act-buttons" }, h("button", {
          class: "act-btn", text: "Record all that are ready",
          onclick: async () => { for (const c of cardsEls) { if (c.classList.contains("ready")) await c._record(); } },
        })));
      }
    } catch (e) {
      status.textContent = e.message || "Couldn't read that PDF.";
      status.className = "act-status bad";
    }
  }

  window.AskActions = { cards, pdf };
})();
