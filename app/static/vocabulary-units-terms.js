/* Vocabulary page — the Unit words and Business terms tabs (the Aliases tab is vocabulary.js).
 * Text goes in with textContent only: words, definitions and product names are data, never markup. */
(function () {
  "use strict";
  const cfg = JSON.parse(document.getElementById("vocab-cfg").textContent);
  const $ = id => document.getElementById(id);
  let timer = null;

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "text") n.textContent = v; else if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else n.setAttribute(k, v);
    });
    kids.forEach(k => k && n.append(k));
    return n;
  }
  async function api(url, opts) {
    const r = await fetch(url, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Something went wrong.");
    return d;
  }
  const num = v => String(Math.round(Number(v) * 1000) / 1000);
  const actions = (onEdit, onDelete) => cfg.canManage
    ? el("td", null, el("button", { class: "btn ghost", text: "Edit", onclick: onEdit }), " ",
         el("button", { class: "btn danger", text: "Delete", onclick: onDelete }))
    : el("td");

  // ── tabs ──
  document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.toggle("on", x === t));
    ["aliases", "units", "terms"].forEach(name => { $("tab-" + name).hidden = name !== t.dataset.tab; });
    if (t.dataset.tab === "units") loadUnits();
    if (t.dataset.tab === "terms") loadTerms();
  }));
  if (cfg.canManage) { $("u-add").hidden = false; $("t-add").hidden = false; }

  // ── unit words ──
  // Units one can type the amount in: the product's own, and its metric partner (g ↔ kg, ml ↔ L).
  const WEIGHT = ["g", "gm", "gram", "grams", "gr", "kg", "kgs", "kilo", "kilos", "جرام", "جم", "كيلو", "كجم"];
  const VOLUME = ["ml", "l", "lt", "ltr", "liter", "litre", "مل", "لتر"];
  function unitOptions(unit) {
    const u = (unit || "").trim().toLowerCase();
    const list = [unit || ""];
    if (WEIGHT.includes(u)) list.push("g", "kg");
    if (VOLUME.includes(u)) list.push("ml", "l");
    return [...new Set(list.filter(x => x !== undefined))];
  }

  let units = [], editingUnit = null, product = null;
  async function loadUnits() {
    const q = $("u-q").value.trim();
    try { units = (await api("/vocabulary/api/unit-words" + (q ? "?q=" + encodeURIComponent(q) : ""))).unit_words; }
    catch (e) { units = []; }
    const body = $("u-rows");
    body.textContent = "";
    if (!units.length) { body.append(el("tr", null, el("td", { colspan: "5", class: "empty", text: "No unit words yet." }))); return; }
    units.forEach(w => body.append(el("tr", null,
      el("td", { text: w.product }), el("td", null, el("strong", { text: w.word })),
      el("td", { text: `1 ${w.word} = ${num(w.qty)} ${w.unit}` }), el("td", { class: "muted", text: w.note }),
      actions(() => openUnit(w), () => delUnit(w)))));
  }
  function paintUnit() {
    $("u-picked").textContent = product ? `${product.name} — counted in ${product.unit || "units"}` : "";
    const sel = $("u-unit");
    sel.textContent = "";
    unitOptions(product ? product.unit : "").forEach(u => sel.append(el("option", { value: u, text: u || "—" })));
  }
  async function searchProducts() {
    const list = $("u-list");
    list.textContent = "";
    let found = [];
    try { found = (await api(`/vocabulary/api/records?type=product&q=${encodeURIComponent($("u-search").value.trim())}`)).records; }
    catch (e) { /* none */ }
    found.forEach(r => list.append(el("button", { type: "button", class: product && product.id === r.id ? "on" : "",
      text: `${r.name}${r.unit ? " · " + r.unit : ""}`, onclick: () => { product = r; paintUnit(); searchProducts(); } })));
  }
  function openUnit(w) {
    editingUnit = w || null;
    product = w ? { id: w.product_id, name: w.product, unit: w.unit } : null;
    $("u-title").textContent = w ? "Edit unit word" : "Add unit word";
    $("u-word").value = w ? w.word : "";
    $("u-amount").value = w ? num(w.qty) : "";
    $("u-note").value = w ? w.note : "";
    $("u-search").value = ""; $("u-msg").textContent = ""; $("u-msg").className = "msg";
    paintUnit(); searchProducts();
    $("u-dlg").classList.add("open");
  }
  async function saveUnit() {
    const body = { product_id: product ? product.id : 0, word: $("u-word").value.trim(), amount: Number($("u-amount").value),
                   amount_unit: $("u-unit").value || null, note: $("u-note").value.trim() || null };
    try {
      if (editingUnit) await api(`/vocabulary/api/unit-words/${editingUnit.id}`, { method: "PUT", body: JSON.stringify(body) });
      else await api("/vocabulary/api/unit-words", { method: "POST", body: JSON.stringify(body) });
      $("u-dlg").classList.remove("open");
      loadUnits();
    } catch (e) { $("u-msg").className = "msg err"; $("u-msg").textContent = e.message; }
  }
  async function delUnit(w) {
    if (!confirm(`Delete the unit word "${w.word}" of ${w.product}?`)) return;
    try { await api(`/vocabulary/api/unit-words/${w.id}`, { method: "DELETE" }); } catch (e) { alert(e.message); }
    loadUnits();
  }
  $("u-q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(loadUnits, 250); });
  $("u-add").addEventListener("click", () => openUnit(null));
  $("u-search").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(searchProducts, 250); });
  $("u-cancel").addEventListener("click", () => $("u-dlg").classList.remove("open"));
  $("u-save").addEventListener("click", saveUnit);

  // ── business terms ──
  let editingTerm = null;
  async function loadTerms() {
    let out = { terms: [], used: 0, cap: 1500 };
    try { out = await api("/vocabulary/api/terms"); } catch (e) { /* keep empty */ }
    const pct = Math.min(100, Math.round(out.used / out.cap * 100));
    $("t-bar").style.width = pct + "%";
    $("t-bar").classList.toggle("full", pct >= 90);
    $("t-used").textContent = `${out.used} / ${out.cap} characters used`;
    const body = $("t-rows");
    body.textContent = "";
    if (!out.terms.length) { body.append(el("tr", null, el("td", { colspan: "4", class: "empty", text: "No terms yet." }))); return; }
    const SRC = { manual: "Added by hand", seed: "Built in" };
    out.terms.forEach(t => body.append(el("tr", null,
      el("td", null, el("strong", { text: t.term })), el("td", { text: t.definition }),
      el("td", null, el("span", { class: "chip " + t.source, text: SRC[t.source] || t.source }),
         t.created_by ? el("div", { class: "muted", text: t.created_by }) : null),
      actions(() => openTerm(t), () => delTerm(t)))));
  }
  function openTerm(t) {
    editingTerm = t || null;
    $("t-title").textContent = t ? "Edit term" : "Add term";
    $("t-term").value = t ? t.term : "";
    $("t-def").value = t ? t.definition : "";
    $("t-msg").textContent = ""; $("t-msg").className = "msg";
    $("t-dlg").classList.add("open");
  }
  async function saveTerm() {
    const body = { term: $("t-term").value.trim(), definition: $("t-def").value.trim() };
    try {
      if (editingTerm) await api(`/vocabulary/api/terms/${editingTerm.id}`, { method: "PUT", body: JSON.stringify(body) });
      else await api("/vocabulary/api/terms", { method: "POST", body: JSON.stringify(body) });
      $("t-dlg").classList.remove("open");
      loadTerms();
    } catch (e) { $("t-msg").className = "msg err"; $("t-msg").textContent = e.message; }
  }
  async function delTerm(t) {
    if (!confirm(`Delete the term "${t.term}"?`)) return;
    try { await api(`/vocabulary/api/terms/${t.id}`, { method: "DELETE" }); } catch (e) { alert(e.message); }
    loadTerms();
  }
  $("t-add").addEventListener("click", () => openTerm(null));
  $("t-cancel").addEventListener("click", () => $("t-dlg").classList.remove("open"));
  $("t-save").addEventListener("click", saveTerm);
})();
