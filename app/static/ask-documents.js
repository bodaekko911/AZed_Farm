/*
 * Ask — read photos and scans of paper documents (expense receipts …) into records.
 *
 * AskDocuments.start(files, {log, toast, onLimit, kinds})
 *   1. Prepares each file IN THE BROWSER: photos are turned upright (EXIF), scaled to at most 2000 px on the long
 *      side and compressed to JPEG; HEIC/HEIF is converted first where the browser can't open it; a PDF is rendered
 *      page by page. More photos can be added for one document (a long receipt in two parts).
 *   2. The user confirms the document type, then the images are read on the server — one question.
 *   3. A review card shows what was read. Values the model couldn't read are empty and marked; values it found hard
 *      to read are highlighted. Nothing is recorded until Record, and only when the lines add up to the printed total.
 *
 * Everything that came from the image or the model is set as an input value or with textContent — never as HTML —
 * and marked data-no-i18n so the translation layer leaves it alone. Fixed labels are separate text nodes so i18n.js
 * can translate them.
 */
(function () {
  "use strict";
  const MAX_SIDE = 2000, QUALITY = 0.82, MIN_QUALITY = 0.6, MAX_CHARS = 2300000, MAX_IMAGES = 10;
  const HEIC2ANY = "https://cdn.jsdelivr.net/npm/heic2any@0.0.4/dist/heic2any.min.js";

  // ── tiny DOM helper (same shape as ask-actions.js) ──
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
  const data = (tag, text, cls) => h(tag, { class: cls, text: text == null ? "" : String(text), "data-no-i18n": true });
  const money = v => (Math.round((Number(v) || 0) * 100) / 100).toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const round2 = v => Math.round((Number(v) + Number.EPSILON) * 100) / 100;
  const num = v => (v === "" || v === null || v === undefined || isNaN(Number(v))) ? null : Number(v);
  const today = () => new Date().toISOString().slice(0, 10);

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) {
      // A structured detail (the Receive page's cost check) keeps its fields so the card can show them.
      const d = out.detail;
      const e = new Error(typeof d === "string" ? d : (d && d.message) || "Something went wrong.");
      e.status = r.status; e.detail = d; throw e;
    }
    return out;
  }

  // ── Preparing images ──
  const isHeic = f => /image\/hei[cf]/i.test(f.type || "") || /\.(heic|heif)$/i.test(f.name || "");
  const isPdf = f => (f.type || "") === "application/pdf" || /\.pdf$/i.test(f.name || "");

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src; s.onload = resolve; s.onerror = () => reject(new Error("Couldn't load the photo converter. Check the connection."));
      document.head.appendChild(s);
    });
  }
  async function decode(blob) {
    // createImageBitmap applies the EXIF orientation itself, so a phone photo comes out upright.
    if (window.createImageBitmap) {
      try { return await createImageBitmap(blob, { imageOrientation: "from-image" }); } catch (e) { /* fall back */ }
    }
    const url = URL.createObjectURL(blob);
    try {
      const img = new Image();
      img.src = url;
      await img.decode();         // modern browsers honour EXIF orientation on <img> too
      return img;
    } finally { URL.revokeObjectURL(url); }
  }
  function toJpeg(source, w, h0) {
    const scale = Math.min(1, MAX_SIDE / Math.max(w, h0));
    let cw = Math.max(1, Math.round(w * scale)), ch = Math.max(1, Math.round(h0 * scale));
    let quality = QUALITY, out = "";
    for (let attempt = 0; attempt < 6; attempt++) {
      const canvas = document.createElement("canvas");
      canvas.width = cw; canvas.height = ch;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, cw, ch);
      ctx.drawImage(source, 0, 0, cw, ch);
      out = canvas.toDataURL("image/jpeg", quality);
      canvas.width = canvas.height = 0;
      if (out.length <= MAX_CHARS) return out;
      // Too heavy for the upload cap: lower the quality first, then the size.
      if (quality > MIN_QUALITY) quality = Math.max(MIN_QUALITY, quality - 0.08);
      else { cw = Math.round(cw * 0.85); ch = Math.round(ch * 0.85); }
    }
    return out;
  }
  async function prepare(file, onStatus) {
    if (isPdf(file)) {
      if (!window.AskActions || !window.AskActions.renderPages) throw new Error("The PDF reader isn't loaded.");
      return await window.AskActions.renderPages(file, (i, n) => onStatus(`page ${i}/${n}`), MAX_SIDE);
    }
    let blob = file, bitmap = null;
    try { bitmap = await decode(blob); } catch (e) { bitmap = null; }
    if (!bitmap && isHeic(file)) {
      // Safari opens HEIC itself; elsewhere convert it to JPEG first.
      if (!window.heic2any) await loadScript(HEIC2ANY);
      const converted = await window.heic2any({ blob: file, toType: "image/jpeg", quality: 0.9 });
      blob = Array.isArray(converted) ? converted[0] : converted;
      bitmap = await decode(blob);
    }
    if (!bitmap) throw new Error("That file isn't a photo this browser can open. Use JPG, PNG, HEIC or PDF.");
    const w = bitmap.width || bitmap.naturalWidth, h0 = bitmap.height || bitmap.naturalHeight;
    const jpeg = toJpeg(bitmap, w, h0);
    if (bitmap.close) bitmap.close();
    return [jpeg];
  }

  // ── Step 1: the document's images and its type ──
  function start(files, opts) {
    const kinds = opts.kinds || [];
    const bubble = h("div", { class: "bubble inv-bubble" });
    const msg = h("div", { class: "msg bot" }, bubble);
    const status = h("div", { class: "inv-muted" });
    const thumbs = h("div", { class: "doc-thumbs" });
    const images = [];
    let name = (files[0] && files[0].name) || "photo";
    opts.log.appendChild(msg);

    const addMore = h("input", { type: "file", accept: "image/*,.heic,.heif,application/pdf,.pdf", multiple: true, hidden: true });
    const camera = h("input", { type: "file", accept: "image/*", capture: "environment", hidden: true });
    const readBtn = h("button", { class: "act-btn act-primary", text: "Read document" });
    const choice = h("select", { class: "doc-kind" }, kinds.map(k => h("option", { value: k.kind, text: k.label })));
    if (kinds.length > 1) choice.prepend(h("option", { value: "auto", text: "Not sure — let the assistant tell" }));

    function paintThumbs() {
      thumbs.replaceChildren(...images.map((src, i) => h("div", { class: "doc-thumb" },
        h("img", { src, alt: "" }),
        h("button", { class: "doc-thumb-x", type: "button", title: "Remove", text: "×", onclick: () => { images.splice(i, 1); paintThumbs(); } }))));
      readBtn.disabled = !images.length;
    }
    async function add(list) {
      for (const f of Array.from(list || [])) {
        if (images.length >= MAX_IMAGES) { status.replaceChildren(h("span", { text: "At most 10 images for one document." })); break; }
        status.replaceChildren(h("span", { text: "Preparing" }), " ", data("span", f.name), "…");
        try {
          const out = await prepare(f, step => status.replaceChildren(h("span", { text: "Preparing" }), " ", data("span", step), "…"));
          images.push(...out.slice(0, MAX_IMAGES - images.length));
          status.replaceChildren();
        } catch (e) {
          status.replaceChildren(h("span", { class: "inv-bad", text: e.message || "Couldn't open that file." }));
        }
      }
      paintThumbs();
    }
    addMore.onchange = e => { add(e.target.files); e.target.value = ""; };
    camera.onchange = e => { add(e.target.files); e.target.value = ""; };

    readBtn.onclick = async () => {
      if (!images.length) return;
      readBtn.disabled = true;
      chooser.querySelectorAll("button, select").forEach(x => { x.disabled = true; });
      status.replaceChildren(h("span", { text: "Reading the document… this can take a minute." }));
      try {
        const out = await post("/assistant/api/documents/read", { kind: choice.value, images, filename: name });
        opts.onLimit && opts.onLimit(out.questions_left, out.daily_limit);
        const d = out.document;
        if (d.not_this_kind) {
          status.replaceChildren(h("span", { class: "inv-bad", text: "This doesn't look like that kind of document. Check the type and the photos." }));
          chooser.querySelectorAll("button, select").forEach(x => { x.disabled = false; });
          readBtn.disabled = !images.length;
          return;
        }
        status.replaceChildren();
        chooser.remove();
        if (d.kind === "expense_receipt") bubble.append(expenseCard(d, images, name, opts));
        else if (d.kind === "delivery_note") bubble.append(deliveryCard(d, images, name, opts));
      } catch (e) {
        status.replaceChildren(h("span", { class: "inv-bad", text: e.message || "Couldn't read that document." }));
        chooser.querySelectorAll("button, select").forEach(x => { x.disabled = false; });
        readBtn.disabled = !images.length;
      }
    };

    const chooser = h("div", { class: "inv-card" },
      h("div", { class: "inv-title", text: "New document" }),
      thumbs,
      h("div", { class: "act-buttons" },
        h("button", { class: "act-btn", type: "button", text: "📷 Add a photo", onclick: () => camera.click() }),
        h("button", { class: "act-btn", type: "button", text: "Add a file", onclick: () => addMore.click() })),
      h("div", { class: "inv-row", style: "margin-top:10px" }, h("span", { class: "inv-label", text: "Type" }), choice),
      h("div", { class: "inv-muted", text: "Add every part of the document (a long receipt can be several photos), then read it. Reading counts as one question." }),
      h("div", { class: "act-buttons" }, readBtn), addMore, camera);
    bubble.append(h("div", { class: "inv-file" }, "📄 ", h("span", { text: "Document" })), status, chooser);
    msg.scrollIntoView({ behavior: "smooth", block: "end" });
    add(files);
  }

  // ── Step 2: review an expense receipt ──
  function expenseCard(d, images, filename, opts) {
    const unclear = new Set(d.unclear || []), missing = new Set(d.missing || []);
    const state = {
      vendor: d.vendor || "", date: d.date || "", receipt_number: d.receipt_number || "",
      payment_method: d.payment_method || "cash",
      category_id: d.category_match ? String(d.category_match.id) : "",
      farm: d.farm_match === "animals" ? "animals" : (d.farm_match ? String(d.farm_match.id) : ""),
      total: d.total ?? "", discount: d.discount ?? 0, other_charges: d.other_charges ?? 0,
      lines: (d.items || []).map(it => ({ description: it.description || "", qty: it.qty ?? "", unit_price: it.unit_price ?? "",
                                           line_total: it.line_total ?? "", category_id: "" })),
      split: false, alloc: {}, force: false, recorded: false,
    };
    const card = h("div", { class: "inv-card" });
    const problems = h("ul", { class: "inv-problems" });
    const totalBox = h("div", { class: "inv-total" });
    const result = h("div", { class: "act-status" });
    const recordBtn = h("button", { class: "act-btn act-primary", text: "Record expense" });

    // Flag a field the model couldn't read (missing) or found hard to read (unclear).
    function flag(input, path) {
      if (missing.has(path)) { input.classList.add("doc-missing"); input.title = "Not readable on the image — enter it"; }
      else if (unclear.has(path)) { input.classList.add("doc-unclear"); input.title = "Hard to read on the image — check it"; }
      input.addEventListener("input", () => input.classList.remove("doc-missing", "doc-unclear"));
      return input;
    }
    const field = (label, input) => h("label", { class: "doc-field" }, h("span", { text: label }), input);
    const textIn = (key, max, path) => flag(h("input", { type: "text", value: state[key], maxlength: max, "data-no-i18n": true,
      oninput: e => { state[key] = e.target.value; check(); } }), path || key);
    const numIn = (obj, key, path) => flag(h("input", { type: "number", step: "any", min: "0", value: obj[key] ?? "",
      oninput: e => { obj[key] = e.target.value; paintLines(); check(); } }), path);

    const catOptions = sel => [h("option", { value: "", text: "— pick a category —" }),
      ...(d.categories || []).map(c => h("option", { value: String(c.id), text: c.name, selected: String(c.id) === sel, "data-no-i18n": true }))];
    const farmSelect = (value, onChange) => {
      const s = h("select", { onchange: e => onChange(e.target.value) },
        h("option", { value: "", text: "No farm (shared)" }), h("option", { value: "animals", text: "Animals" }),
        ...(d.farms || []).map(f => h("option", { value: String(f.id), text: f.name, "data-no-i18n": true })));
      s.value = value; return s;
    };
    const category = h("select", { onchange: e => { state.category_id = e.target.value; check(); } }, catOptions(state.category_id));
    if (d.category && !d.category_match) category.title = d.category;
    const method = h("select", { onchange: e => { state.payment_method = e.target.value; } },
      h("option", { value: "cash", text: "Cash" }), h("option", { value: "card", text: "Card" }),
      h("option", { value: "bank_transfer", text: "Bank transfer" }));
    method.value = state.payment_method;

    const head = h("div", { class: "doc-grid" },
      field("Vendor", textIn("vendor", 150)),
      field("Date", flag(h("input", { type: "date", value: state.date, max: today(), oninput: e => { state.date = e.target.value; check(); } }), "date")),
      field("Receipt no.", textIn("receipt_number", 40)),
      field("Paid by", method),
      field("Category", category),
      field("Farm", farmSelect(state.farm, v => { state.farm = v; })));
    const suggestion = h("div", { class: "inv-muted" });
    if (d.category_match) suggestion.append(h("span", { text: "Category suggested by the assistant — confirm or change it." }));

    // Lines
    const tbody = h("tbody");
    const lineCat = [];
    function paintLines() {
      tbody.querySelectorAll("td.doc-line-amt").forEach((td, i) => {
        const l = state.lines[i], a = lineAmount(l);
        td.textContent = a === null ? "—" : money(a);
      });
    }
    const lineAmount = l => num(l.line_total) !== null ? num(l.line_total)
      : (num(l.qty) !== null && num(l.unit_price) !== null ? round2(num(l.qty) * num(l.unit_price)) : null);
    state.lines.forEach((l, i) => {
      const cat = h("select", { class: "doc-line-cat", hidden: true, onchange: e => { l.category_id = e.target.value; computeAlloc(); check(); } }, catOptions(""));
      lineCat.push(cat);
      tbody.append(h("tr", null,
        h("td", null, h("input", { type: "text", value: l.description, maxlength: 200, "data-no-i18n": true, oninput: e => { l.description = e.target.value; } })),
        h("td", null, numIn(l, "qty", `items.${i}.qty`)),
        h("td", null, numIn(l, "unit_price", `items.${i}.unit_price`)),
        h("td", null, numIn(l, "line_total", `items.${i}.line_total`)),
        h("td", { class: "num doc-line-amt" }),
        h("td", null, cat)));
    });
    const lines = state.lines.length ? h("table", { class: "inv-table" },
      h("thead", null, h("tr", null, ["Item", "Qty", "Price", "Line total", "Amount", ""].map(t => h("th", { text: t })))), tbody)
      : h("div", { class: "inv-muted", text: "No item lines were printed — the total is recorded as it is on the receipt." });

    const foot = h("div", { class: "inv-foot" },
      field("Discount", numIn(state, "discount", "discount")),
      field("Tax / extra charges", numIn(state, "other_charges", "other_charges")),
      field("Printed total", numIn(state, "total", "total")), totalBox);

    // Split by category
    const allocBox = h("div", { class: "doc-alloc", hidden: true });
    const splitToggle = h("label", { class: "doc-split" },
      h("input", { type: "checkbox", onchange: e => { state.split = e.target.checked; lineCat.forEach(s => { s.hidden = !state.split; });
        category.disabled = state.split; allocBox.hidden = !state.split; computeAlloc(); check(); } }),
      h("span", { text: "Split by category" }));
    // Manual split rows for a receipt with no lines
    const manual = [];
    function computeAlloc() {
      if (!state.split) return;
      allocBox.replaceChildren(h("div", { class: "inv-muted", text: "One expense per category, all linked to this receipt. The amounts must add up to the printed total." }));
      if (state.lines.length) {
        // Line amounts per category; discount and extra charges shared out in proportion, the rounding on the largest.
        const sums = {};
        state.lines.forEach(l => { if (l.category_id) sums[l.category_id] = (sums[l.category_id] || 0) + (lineAmount(l) || 0); });
        const lineSum = Object.values(sums).reduce((a, b) => a + b, 0);
        const adjust = (num(state.other_charges) || 0) - (num(state.discount) || 0);
        const ids = Object.keys(sums);
        const out = {}; let running = 0;
        ids.sort((a, b) => sums[b] - sums[a]).forEach((id, i) => {
          out[id] = i === 0 ? null : round2(sums[id] + (lineSum ? adjust * sums[id] / lineSum : 0));
          if (i) running += out[id];
        });
        if (ids.length) out[ids.sort((a, b) => sums[b] - sums[a])[0]] = round2((num(state.total) || 0) - running);
        // Keep amounts the user typed, for categories still in use.
        ids.forEach(id => { if (!(id in state.alloc) || state.alloc[id].auto) state.alloc[id] = { amount: out[id], auto: true }; });
        Object.keys(state.alloc).forEach(id => { if (!(id in sums)) delete state.alloc[id]; });
        ids.forEach(id => {
          const c = (d.categories || []).find(x => String(x.id) === id);
          const input = h("input", { type: "number", step: "any", min: "0", value: state.alloc[id].amount,
            oninput: e => { state.alloc[id] = { amount: e.target.value, auto: false }; check(); } });
          allocBox.append(h("div", { class: "inv-row" }, data("span", c ? c.name : id, "inv-label"), input));
        });
      } else {
        if (!manual.length) manual.push({ category_id: "", amount: "" }, { category_id: "", amount: "" });
        manual.forEach(m => {
          const s = h("select", { onchange: e => { m.category_id = e.target.value; check(); } }, catOptions(m.category_id));
          const a = h("input", { type: "number", step: "any", min: "0", value: m.amount, oninput: e => { m.amount = e.target.value; check(); } });
          allocBox.append(h("div", { class: "inv-row" }, s, a));
        });
        allocBox.append(h("button", { class: "act-btn", type: "button", text: "+ Add a category",
          onclick: () => { manual.push({ category_id: "", amount: "" }); computeAlloc(); check(); } }));
      }
    }
    function allocations() {
      if (state.lines.length) {
        return Object.keys(state.alloc).map(id => ({ category_id: Number(id), amount: num(state.alloc[id].amount),
          lines: state.lines.map((l, i) => l.category_id === id ? i : -1).filter(i => i >= 0) }));
      }
      return manual.filter(m => m.category_id || m.amount).map(m => ({ category_id: Number(m.category_id) || null, amount: num(m.amount) }));
    }

    function issue(parts) { return h("li", null, ...parts.map(p => typeof p === "string" ? h("span", { text: p }) : p)); }
    function check() {
      const issues = [];
      const total = num(state.total);
      if (total === null || total <= 0) issues.push(issue(["Enter the printed total."]));
      if (!state.date) issues.push(issue(["Set the receipt's date."]));
      else if (state.date > today()) issues.push(issue(["The date is in the future."]));
      totalBox.replaceChildren();
      if (state.lines.length) {
        let ok = true, sum = 0;
        state.lines.forEach((l, i) => {
          const a = lineAmount(l);
          if (a === null) { ok = false; issues.push(issue(["A line has no amount — fill in its total or its quantity and price:", " ", data("strong", l.description || `#${i + 1}`)])); }
          else sum += a;
        });
        const computed = round2(sum - (num(state.discount) || 0) + (num(state.other_charges) || 0));
        if (ok && total !== null) {
          if (Math.abs(computed - total) <= 0.1) totalBox.append(h("span", { class: "inv-ok" }, "✅ ", h("span", { text: "Lines match the printed total" })));
          else {
            issues.push(issue(["The lines don't add up to the printed total:", " ", data("strong", money(computed)), " / ", data("strong", money(total))]));
            totalBox.append(h("span", { class: "inv-bad" }, "❌ ", data("span", `${money(computed)} / ${money(total)}`)));
          }
        }
      }
      if (state.split) {
        const parts = allocations();
        if (parts.length < 2) issues.push(issue(["A split needs at least two categories."]));
        if (state.lines.length && state.lines.some(l => !l.category_id)) issues.push(issue(["Give every line a category."]));
        if (parts.some(p => !p.category_id)) issues.push(issue(["Pick a category for every amount."]));
        if (parts.some(p => !(p.amount > 0))) issues.push(issue(["Every category needs an amount above 0."]));
        const ids = parts.map(p => p.category_id).filter(Boolean);
        if (new Set(ids).size !== ids.length) issues.push(issue(["Each category can appear only once."]));
        const s = round2(parts.reduce((a, p) => a + (p.amount || 0), 0));
        if (total !== null && Math.abs(s - total) > 0.01) issues.push(issue(["The split doesn't add up to the printed total:", " ", data("strong", money(s)), " / ", data("strong", money(total))]));
      } else if (!state.category_id) issues.push(issue(["Pick an expense category."]));
      if (d.duplicate && !state.force) issues.push(issue([data("span", d.duplicate)]));
      problems.replaceChildren(...issues);
      recordBtn.disabled = state.recorded || issues.length > 0;
      return !issues.length;
    }

    const buttons = h("div", { class: "act-buttons" }, recordBtn);
    if (d.duplicate) buttons.append(h("button", { class: "act-btn", text: "Record anyway",
      onclick: e => { state.force = true; e.target.remove(); check(); } }));
    recordBtn.onclick = async () => {
      if (!check()) return;
      recordBtn.disabled = true; result.replaceChildren(h("span", { text: "Recording…" })); result.className = "act-status";
      try {
        const out = await post("/assistant/api/documents/record", {
          kind: "expense_receipt", images, proof: d.proof, filename, force: state.force,
          vendor: state.vendor, date: state.date, receipt_number: state.receipt_number, payment_method: state.payment_method,
          total: num(state.total), discount: num(state.discount) || 0, other_charges: num(state.other_charges) || 0,
          items: state.lines.map(l => ({ description: l.description, qty: num(l.qty), unit_price: num(l.unit_price), line_total: num(l.line_total) })),
          category_id: state.split ? null : Number(state.category_id), farm: state.farm || null,
          split: state.split, allocations: state.split ? allocations() : null,
        });
        state.recorded = true;
        result.className = "act-status ok";
        result.replaceChildren("✓ ", h("span", { text: "Recorded" }), " ",
          ...out.expenses.map((x, i) => h("span", null, i ? ", " : "", h("a", { href: `/documents/view?ref_type=expense&ref_id=${x.id}`, target: "_blank", rel: "noopener", "data-no-i18n": true, text: `${x.ref_number} (${money(x.amount)})` }))),
          " — ", h("span", { text: "receipt image kept with the expense" }));
        card.classList.add("done"); card.querySelectorAll("input, select, button").forEach(x => { x.disabled = true; });
      } catch (e) {
        result.replaceChildren(h("span", { text: e.message })); result.className = "act-status bad"; check();
      }
    };

    card.append(
      h("div", { class: "inv-head" }, h("div", { class: "inv-title", text: "Expense receipt" }),
        h("span", { class: "inv-muted", text: "Highlighted fields were hard to read — check them against the photo." })),
      // Tap a photo to see it large next to the fields (browsers won't open an embedded image in a new tab).
      h("div", { class: "doc-thumbs small" }, images.map(src => h("img", { src, alt: "", title: "Tap to enlarge",
        onclick: e => e.target.classList.toggle("big") }))),
      head, suggestion, lines, foot, splitToggle, allocBox, problems, buttons, result);
    paintLines(); check();
    return card;
  }

  // ── Picker: likely matches, plus search (suppliers / products) ──
  function picker(kind, current, candidates, emptyLabel, onChange, extra) {
    const label = item => kind === "suppliers" ? item.name + (item.phone ? ` · ${item.phone}` : "")
                                               : `${item.name} · ${item.sku} · ${item.unit || ""}`;
    const wrap = h("div", { class: "inv-picker" });
    const select = h("select", { class: "inv-select", "data-no-i18n": true });
    const known = new Map();
    const add = (item, selected) => {
      if (!known.has(String(item.id))) {
        known.set(String(item.id), item);
        select.insertBefore(h("option", { value: String(item.id), text: label(item) }), select.querySelector('option[value="__search"]'));
      }
      if (selected) select.value = String(item.id);
    };
    select.append(h("option", { value: "", text: emptyLabel }));
    if (extra) select.append(h("option", { value: "__new", text: extra.label, disabled: extra.disabled }));
    select.append(h("option", { value: "__search", text: "🔍 Search…" }));
    if (current) add(current, true);
    (candidates || []).forEach(c => add(c, false));
    if (!current) select.value = extra && extra.selected ? "__new" : "";
    const search = h("input", { class: "inv-search", type: "search", hidden: true,
                                placeholder: kind === "suppliers" ? "Name or phone…" : "Product name or SKU…" });
    const results = h("div", { class: "inv-results", hidden: true });
    let last = select.value, timer = null;
    select.onchange = () => {
      if (select.value === "__search") { select.value = last; search.hidden = results.hidden = false; search.focus(); return; }
      last = select.value; onChange(select.value === "__new" ? "__new" : (known.get(select.value) || null));
    };
    search.oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        results.replaceChildren();
        if (search.value.trim().length < 2) return;
        try {
          const r = await fetch(`/assistant/api/documents/search?kind=${kind}&q=${encodeURIComponent(search.value.trim())}`);
          const out = await r.json();
          (out.results || []).forEach(item => results.append(h("button", { class: "inv-result", type: "button", text: label(item), "data-no-i18n": true,
            onclick: () => { add(item, true); last = select.value; search.hidden = results.hidden = true; search.value = ""; results.replaceChildren(); onChange(item); } })));
          if (!(out.results || []).length) results.append(h("div", { class: "inv-muted", text: "No matches." }));
        } catch (e) { /* ignore */ }
      }, 250);
    };
    wrap.append(select, search, results);
    return wrap;
  }

  // ── Pack sizes: "Honey 500g" × 10 on the note, "Honey (1g)" in Azed ──
  const fmt3 = v => String(Math.round(v * 1000) / 1000);
  const packText = p => p.amount >= 1000 ? `${fmt3(p.amount / 1000)} ${p.base === "g" ? "kg" : "l"}` : `${fmt3(p.amount)} ${p.base}`;
  /** Quantity and unit cost in the product's own unit, from what the note says. */
  function inProductUnits(l) {
    const p = l.product, from = l.pack, to = p && p.pack;
    let qty = l.pQty, cost = l.pCost, note = "";
    if (from && to && from.base === to.base && Math.abs(from.amount - to.amount) > 1e-9 && qty !== "" && qty !== null) {
      const f = from.amount / to.amount;
      qty = Math.round(Number(qty) * f * 1000) / 1000;
      if (cost !== "" && cost !== null) cost = Number(cost) / f;
      note = `${fmt3(l.pQty)} × ${packText(from)} = ${fmt3(Number(l.pQty) * from.amount)} ${from.base}`;
    }
    return { qty: qty ?? "", cost: cost ?? "", note };
  }

  // ── Step 2: review a supplier delivery note ──
  function deliveryCard(d, images, filename, opts) {
    const unclear = new Set(d.unclear || []), missing = new Set(d.missing || []);
    const paid0 = d.amount_paid !== null && d.amount_paid !== undefined ? d.amount_paid
                : (d.paid_in_full === true ? (d.total ?? "") : 0);
    const state = {
      supplier: d.supplier_match || null,
      newSupplier: d.new_supplier ? Object.assign({}, d.new_supplier) : null,
      useNew: !!(d.new_supplier && !d.supplier_match),
      date: d.date || "", number: d.document_number || "",
      total: d.total ?? "", discount: d.discount ?? 0, other_charges: d.other_charges ?? 0, paid: paid0,
      category_id: "", farm: "", location_id: "",
      lines: (d.lines || []).map(l => {
        // A cost derived from the printed line total and quantity is arithmetic on printed values, not a guess.
        const pCost = l.unit_cost ?? (l.line_total !== null && l.line_total !== undefined && l.qty ? l.line_total / l.qty : null);
        const line = { description: l.description, product: l.product, candidates: l.candidates, match: l.match,
                       pack: l.pack, unit: l.unit, pQty: l.qty ?? "", pCost: pCost ?? "" };
        return Object.assign(line, inProductUnits(line));
      }),
      force: false, confirmCost: false, recorded: false,
    };
    const card = h("div", { class: "inv-card" });
    const problems = h("ul", { class: "inv-problems" });
    const totalBox = h("div", { class: "inv-total" });
    const result = h("div", { class: "act-status" });
    const costBox = h("div", { class: "doc-alloc", hidden: true });
    const recordBtn = h("button", { class: "act-btn act-primary", text: "Receive stock" });
    function flag(input, path) {
      if (missing.has(path)) { input.classList.add("doc-missing"); input.title = "Not readable on the image — enter it"; }
      else if (unclear.has(path)) { input.classList.add("doc-unclear"); input.title = "Hard to read on the image — check it"; }
      input.addEventListener("input", () => input.classList.remove("doc-missing", "doc-unclear"));
      return input;
    }
    const field = (label, input) => h("label", { class: "doc-field" }, h("span", { text: label }), input);
    const priced = () => state.lines.some(l => l.cost !== "" && l.cost !== null) || (state.total !== "" && state.total !== null);

    // Supplier (existing, or new from the note)
    const newBox = h("div", { class: "inv-new", hidden: !state.useNew });
    if (state.newSupplier) {
      const f = (key, label) => h("label", null, label, h("input", { type: "text", value: state.newSupplier[key] || "", maxlength: 150,
        "data-no-i18n": true, oninput: e => { state.newSupplier[key] = e.target.value; check(); } }));
      newBox.append(h("div", { class: "inv-muted", text: "Not in Azed yet — it will be added when you receive:" }),
        h("div", { class: "inv-new-fields" }, f("name", "Name"), f("phone", "Phone")));
    }
    const supplierRow = h("div", { class: "inv-row" }, h("span", { class: "inv-label", text: "Supplier" }),
      d.supplier ? data("span", d.supplier, "inv-muted") : null,
      picker("suppliers", state.supplier, d.supplier_candidates, "— no supplier (paid in cash) —", s => {
        state.useNew = s === "__new"; state.supplier = state.useNew ? null : s; newBox.hidden = !state.useNew; check();
      }, state.newSupplier ? { label: `➕ ${state.newSupplier.name}`, selected: state.useNew } : null));

    const cats = h("select", { onchange: e => { state.category_id = e.target.value; check(); } },
      h("option", { value: "", text: "— pick a category —" }),
      ...(d.categories || []).map(c => h("option", { value: String(c.id), text: c.name, "data-no-i18n": true })));
    const farm = h("select", { onchange: e => { state.farm = e.target.value; } },
      h("option", { value: "", text: "No farm (shared)" }), h("option", { value: "animals", text: "Animals" }),
      ...(d.farms || []).map(f => h("option", { value: String(f.id), text: f.name, "data-no-i18n": true })));
    const loc = h("select", { onchange: e => { state.location_id = e.target.value; } },
      h("option", { value: "", text: "Main warehouse (default)" }),
      ...(d.locations || []).map(l => h("option", { value: String(l.id), text: l.name, "data-no-i18n": true })));
    const head = h("div", { class: "doc-grid" },
      field("Date", flag(h("input", { type: "date", value: state.date, max: today(), oninput: e => { state.date = e.target.value; check(); } }), "date")),
      field("Document no.", flag(h("input", { type: "text", value: state.number, maxlength: 60, "data-no-i18n": true,
        oninput: e => { state.number = e.target.value; check(); } }), "document_number")),
      field("Store in", loc), field("Expense category", cats), field("Farm", farm));

    // Lines
    const tbody = h("tbody");
    state.lines.forEach((l, i) => {
      const amt = h("td", { class: "num" });
      const paint = () => { const q = num(l.qty), c = num(l.cost); amt.textContent = q !== null && c !== null ? money(q * c) : "—"; };
      const qty = flag(h("input", { type: "number", step: "any", min: "0", value: l.qty, oninput: e => { l.qty = e.target.value; paint(); check(); } }), `items.${i}.qty`);
      const cost = flag(h("input", { type: "number", step: "any", min: "0", value: l.cost, oninput: e => { l.cost = e.target.value; paint(); check(); } }), `items.${i}.unit_cost`);
      const hint = h("div", { class: "inv-hint", text: l.match === "closest" && l.product ? "Closest match by name — check it" : "" });
      const conv = h("div", { class: "inv-conv", text: l.note });
      const unit = data("span", l.product ? l.product.unit : "", "inv-muted");
      const pick = picker("products", l.product, l.candidates, "— pick a product —", p => {
        l.product = p; hint.textContent = "";
        Object.assign(l, inProductUnits(l));      // a different product may count in a different size
        qty.value = l.qty; cost.value = l.cost; conv.textContent = l.note; unit.textContent = p ? p.unit : "";
        paint(); check();
      });
      paint();
      const printed = [l.pQty !== "" ? l.pQty : "?", l.unit || "", l.pCost !== "" ? `@ ${money(l.pCost)}` : ""].join(" ");
      tbody.append(h("tr", null,
        h("td", { class: "inv-desc" }, data("div", l.description || "—"), data("div", printed, "inv-muted")),
        h("td", null, pick, hint, conv), h("td", null, qty, " ", unit), h("td", null, cost), amt));
    });
    const table = h("table", { class: "inv-table" },
      h("thead", null, h("tr", null, ["On the note", "Product", "Qty", "Unit cost", "Line"].map(t => h("th", { text: t })))), tbody);

    const numIn = (key, path) => flag(h("input", { type: "number", step: "any", min: "0", value: state[key] ?? "",
      oninput: e => { state[key] = e.target.value; check(); } }), path || key);
    const foot = h("div", { class: "inv-foot" },
      field("Discount", numIn("discount")), field("Transport / extra charges", numIn("other_charges")),
      field("Printed total", numIn("total")), field("Amount paid now", numIn("paid", "amount_paid")), totalBox);

    function issue(parts) { return h("li", null, ...parts.map(p => typeof p === "string" ? h("span", { text: p }) : p)); }
    function check() {
      const issues = [];
      if (!state.date) issues.push(issue(["Set the delivery note's date."]));
      else if (state.date > today()) issues.push(issue(["The date is in the future."]));
      if (state.useNew && !(state.newSupplier.name || "").trim()) issues.push(issue(["Give the new supplier a name."]));
      state.lines.forEach(l => {
        if (!l.product) issues.push(issue(["Pick a product for:", " ", data("strong", l.description || "—")]));
        if (!(num(l.qty) > 0)) issues.push(issue(["A quantity must be above 0:", " ", data("strong", l.description || "—")]));
      });
      totalBox.replaceChildren();
      if (priced()) {
        if (state.lines.some(l => num(l.cost) === null)) issues.push(issue(["Give every line a unit cost, or clear all costs and the total to receive quantities only."]));
        const total = num(state.total);
        if (total === null) issues.push(issue(["Enter the printed total."]));
        else {
          const sum = round2(state.lines.reduce((a, l) => a + (num(l.qty) || 0) * (num(l.cost) || 0), 0)
                      - (num(state.discount) || 0) + (num(state.other_charges) || 0));
          if (Math.abs(sum - total) <= 0.1) totalBox.append(h("span", { class: "inv-ok" }, "✅ ", h("span", { text: "Lines match the printed total" })));
          else {
            issues.push(issue(["The lines don't add up to the printed total:", " ", data("strong", money(sum)), " / ", data("strong", money(total))]));
            totalBox.append(h("span", { class: "inv-bad" }, "❌ ", data("span", `${money(sum)} / ${money(total)}`)));
          }
          const paid = num(state.paid) || 0;
          if (paid < 0 || paid > total) issues.push(issue(["The amount paid must be between 0 and the total."]));
          if (!state.supplier && !state.useNew && paid < total) issues.push(issue(["Pick the supplier — there is an unpaid amount."]));
        }
        if (!state.category_id) issues.push(issue(["Pick the expense category."]));
      } else {
        totalBox.append(h("span", { class: "inv-muted", text: "No prices on the note — quantities only" }));
      }
      if (d.duplicate && !state.force) issues.push(issue([data("span", d.duplicate)]));
      problems.replaceChildren(...issues);
      recordBtn.disabled = state.recorded || issues.length > 0;
      return !issues.length;
    }

    const buttons = h("div", { class: "act-buttons" }, recordBtn);
    if (d.duplicate) buttons.append(h("button", { class: "act-btn", text: "Record anyway", onclick: e => { state.force = true; e.target.remove(); check(); } }));
    recordBtn.onclick = async () => {
      if (!check()) return;
      recordBtn.disabled = true; result.replaceChildren(h("span", { text: "Recording…" })); result.className = "act-status";
      const isPriced = priced();
      try {
        const out = await post("/assistant/api/documents/record", {
          kind: "delivery_note", images, proof: d.proof, filename, force: state.force, confirm_cost: state.confirmCost,
          date: state.date, document_number: state.number,
          supplier_id: state.supplier ? state.supplier.id : null, new_supplier: state.useNew ? state.newSupplier : null,
          items: state.lines.map(l => ({ product_id: l.product ? l.product.id : null, qty: num(l.qty), unit_cost: isPriced ? num(l.cost) : null })),
          total: isPriced ? num(state.total) : null, discount: num(state.discount) || 0, other_charges: num(state.other_charges) || 0,
          amount_paid: isPriced ? (num(state.paid) || 0) : null,
          expense_category_id: isPriced ? Number(state.category_id) : null, farm: state.farm || null, location_id: state.location_id || null,
        });
        state.recorded = true; costBox.hidden = true;
        result.className = "act-status ok";
        result.replaceChildren("✓ ", h("span", { text: "Received" }), " ",
          ...out.receipts.map((x, i) => h("span", null, i ? ", " : "", h("a", { href: `/documents/view?ref_type=product_receipt&ref_id=${x.id}`,
            target: "_blank", rel: "noopener", "data-no-i18n": true, text: x.ref_number }))),
          isPriced ? h("span", { "data-no-i18n": true, text: ` — ${money(out.total_cost)} (${money(out.total_unpaid)} owed)` }) : "",
          " — ", h("span", { text: "delivery note image kept with the stock" }));
        card.classList.add("done"); card.querySelectorAll("input, select, button").forEach(x => { x.disabled = true; });
      } catch (e) {
        if (e.status === 409 && e.detail && e.detail.code === "cost_check") {
          // The Receive page's check: a cost far from the product's usual one is usually a unit mix-up.
          costBox.hidden = false;
          costBox.replaceChildren(h("div", { class: "inv-bad", text: e.detail.message }),
            h("ul", { class: "inv-problems" }, ...(e.detail.problems || []).map(p => h("li", null, data("span", p)))),
            h("button", { class: "act-btn", type: "button", text: "The costs are right — receive anyway",
              onclick: () => { state.confirmCost = true; costBox.hidden = true; recordBtn.onclick(); } }));
          result.replaceChildren();
        } else { result.replaceChildren(h("span", { text: e.message })); result.className = "act-status bad"; }
        check();
      }
    };

    card.append(
      h("div", { class: "inv-head" }, h("div", { class: "inv-title", text: "Supplier delivery note" }),
        h("span", { class: "inv-muted", text: "Highlighted fields were hard to read — check them against the photo." })),
      h("div", { class: "doc-thumbs small" }, images.map(src => h("img", { src, alt: "", title: "Tap to enlarge", onclick: e => e.target.classList.toggle("big") }))),
      supplierRow, newBox, head, table, foot, problems, costBox, buttons, result);
    check();
    return card;
  }

  window.AskDocuments = { start, prepare, number: num };
})();
