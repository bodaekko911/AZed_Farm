/* Vocabulary page: list, search, filter; add / edit / delete for managers of the vocabulary.
 * Text goes in with textContent only — aliases are data, never markup. */
(function () {
  "use strict";
  const cfg = JSON.parse(document.getElementById("vocab-cfg").textContent);
  const $ = id => document.getElementById(id);
  const NEW_GROUP = "__new__";
  const SOURCES = { manual: "Added by hand", learned: "Learned from a correction", import: "Imported", seed: "Built in" };
  let rows = [], editing = null, picked = null, timer = null;

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

  cfg.types.forEach(t => {
    $("type").append(el("option", { value: t.key, text: t.label }));
    $("f-type").append(el("option", { value: t.key, text: t.label }));
  });
  if (cfg.canManage) { $("add").hidden = false; $("act-h").textContent = ""; }

  async function load() {
    const p = new URLSearchParams();
    if ($("type").value) p.set("type", $("type").value);
    if ($("q").value.trim()) p.set("q", $("q").value.trim());
    try { rows = (await api("/vocabulary/api/entries?" + p)).entries; } catch (e) { rows = []; }
    paint();
  }

  function paint() {
    const body = $("rows");
    body.textContent = "";
    if (!rows.length) {
      body.append(el("tr", null, el("td", { colspan: "6", class: "empty", text: "No aliases yet." })));
      return;
    }
    rows.forEach(r => {
      const when = r.created_at ? new Date(r.created_at).toLocaleDateString() : "";
      const who = [r.created_by, when].filter(Boolean).join(" · ");
      const acts = el("td");
      if (cfg.canManage) {
        acts.append(el("button", { class: "btn ghost", text: "Edit", onclick: () => open(r) }), " ",
                    el("button", { class: "btn danger", text: "Delete", onclick: () => del(r) }));
      }
      body.append(el("tr", null,
        el("td", null, el("strong", { text: r.alias })),
        el("td", { text: r.type_label }),
        el("td", { text: r.target }),
        el("td", { class: "muted", text: r.note || "" }),
        el("td", null, el("span", { class: "chip " + r.source, text: SOURCES[r.source] || r.source }),
           who ? el("div", { class: "muted", text: who }) : null),
        acts));
    });
  }

  // ── the add / edit dialog ──
  async function groups(selected) {
    const g = $("f-group");
    g.textContent = "";
    let names = [];
    try { names = (await api("/vocabulary/api/area-groups")).groups; } catch (e) { /* none */ }
    g.append(el("option", { value: "", text: "— pick a group —" }));
    names.forEach(n => g.append(el("option", { value: n, text: n })));
    g.append(el("option", { value: NEW_GROUP, text: "+ New group…" }));
    g.value = selected || "";
    $("f-new-box").hidden = g.value !== NEW_GROUP;
  }

  async function searchRecords() {
    const list = $("f-list");
    list.textContent = "";
    const type = $("f-type").value;
    if (type === "area") return;
    let found = [];
    try {
      found = (await api(`/vocabulary/api/records?type=${encodeURIComponent(type)}&q=${encodeURIComponent($("f-search").value.trim())}`)).records;
    } catch (e) { /* none */ }
    found.forEach(rec => list.append(el("button", {
      type: "button", class: picked && picked.id === rec.id ? "on" : "",
      text: rec.name + (rec.active ? "" : " (inactive)"),
      onclick: () => { picked = rec; $("f-picked").textContent = "Means: " + rec.name; searchRecords(); },
    })));
  }

  function syncType() {
    const area = $("f-type").value === "area";
    $("f-record-box").hidden = area;
    $("f-area-box").hidden = !area;
  }

  async function open(row) {
    editing = row || null;
    picked = row && row.type !== "area" ? { id: row.record_id, name: row.target } : null;
    $("dlg-title").textContent = row ? "Edit alias" : "Add alias";
    $("f-type").value = row ? row.type : ($("type").value || "product");
    $("f-type").disabled = !!row;
    $("f-alias").value = row ? row.alias : "";
    $("f-note").value = row ? row.note : "";
    $("f-search").value = "";
    $("f-new").value = "";
    $("f-picked").textContent = picked ? "Means: " + picked.name : "";
    $("f-msg").textContent = ""; $("f-msg").className = "msg";
    syncType();
    await groups(row && row.type === "area" ? row.target : "");
    searchRecords();
    $("dlg").classList.add("open");
    $("f-alias").focus();
  }

  function close() { $("dlg").classList.remove("open"); }

  async function save() {
    const type = $("f-type").value, area = type === "area";
    const body = { alias: $("f-alias").value.trim(), note: $("f-note").value.trim() || null };
    if (area) {
      if ($("f-group").value === NEW_GROUP) body.new_group = $("f-new").value.trim();
      else body.group = $("f-group").value;
    } else {
      body.record_id = picked ? picked.id : null;
    }
    $("f-save").disabled = true;
    try {
      const out = editing
        ? await api(`/vocabulary/api/entries/${editing.id}`, { method: "PUT", body: JSON.stringify(body) })
        : await api("/vocabulary/api/entries", { method: "POST", body: JSON.stringify(Object.assign({ type }, body)) });
      await load();
      if (out.warnings && out.warnings.length) {
        $("f-msg").className = "msg warn";
        $("f-msg").textContent = "Saved. " + out.warnings.join(" ");
        editing = rows.find(r => r.id === (out.id || (editing && editing.id))) || editing;
      } else {
        close();
      }
    } catch (e) {
      $("f-msg").className = "msg err";
      $("f-msg").textContent = e.message;
    } finally {
      $("f-save").disabled = false;
    }
  }

  async function del(row) {
    if (!confirm(`Delete the alias "${row.alias}"? Typing it will no longer mean ${row.target}.`)) return;
    try { await api(`/vocabulary/api/entries/${row.id}`, { method: "DELETE" }); } catch (e) { alert(e.message); }
    load();
  }

  $("q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
  $("type").addEventListener("change", load);
  $("add").addEventListener("click", () => open(null));
  $("f-type").addEventListener("change", () => { picked = null; $("f-picked").textContent = ""; syncType(); searchRecords(); });
  $("f-search").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(searchRecords, 250); });
  $("f-group").addEventListener("change", () => { $("f-new-box").hidden = $("f-group").value !== NEW_GROUP; });
  $("f-cancel").addEventListener("click", close);
  $("f-save").addEventListener("click", save);
  $("dlg").addEventListener("click", e => { if (e.target === $("dlg")) close(); });
  load();
})();
