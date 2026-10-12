/*
 * Field inbox — review what workers sent from the Telegram bot.
 *
 * The review card is Ask's own (AskDocuments.cards), filled from the stored draft; its Record button confirms the draft
 * here instead of uploading images. Everything from the worker or the model is set with textContent or as an input
 * value, never as HTML, and marked data-no-i18n. Fixed labels are separate text nodes so i18n.js can translate them.
 */
(function () {
  "use strict";
  const KIND = { expense_receipt: "Expense receipt", delivery_note: "Supplier delivery note",
                 harvest_sheet: "Harvest sheet / delivery", spoilage: "Spoilage", b2b_sale: "B2B sale" };
  const WARN = { duplicate: "Possible duplicate", totals: "Lines don't add up", new_supplier: "New supplier",
                 unmatched_product: "Product not matched", unmatched_line: "Farm or product not matched",
                 no_category: "No expense category", priced: "Has prices", closest_match: "Product match not certain",
                 unit: "Unit differs", no_client: "No client" };

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
  const when = iso => { try { return new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return iso; } };

  async function api(url, body, method) {
    const r = await fetch(url, body === undefined && !method ? { credentials: "same-origin" } : {
      method: method || "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = out.detail;
      const e = new Error(typeof d === "string" ? d : (d && d.message) || "Something went wrong.");
      e.status = r.status; e.detail = d; throw e;
    }
    return out;
  }

  const list = document.getElementById("list"), panel = document.getElementById("panel");
  let status = "in_review", current = null;
  if (window.AskDocuments) window.AskDocuments.setSearchUrl("/field-inbox/api/search");

  function warnChips(ws) {
    return (ws || []).map(w => {
      const [key, field] = String(w).split(":");
      const label = key === "missing" ? "Missing" : key === "unclear" ? "Unclear" : (WARN[key] || key);
      return h("span", { class: "warn-chip" }, h("span", { text: label }), field ? data("span", " " + field) : "");
    });
  }

  async function load() {
    list.replaceChildren(h("div", { class: "empty", text: "Loading…" }));
    try {
      const out = await api(`/field-inbox/api/drafts?status=${status}`);
      if (out.is_admin) admin();
      list.replaceChildren();
      if (!out.drafts.length) { list.append(h("div", { class: "empty", text: "Nothing here." })); return; }
      out.drafts.forEach(d => {
        const item = h("button", { class: "item" + (current === d.id ? " on" : ""), type: "button", onclick: () => open(d.id, item) },
          h("div", { class: "item-top" }, h("span", { text: KIND[d.kind] || d.kind }), data("span", "#" + d.id)),
          h("div", { class: "item-sub" }, data("span", d.sender.name), d.sender.username ? data("span", " @" + d.sender.username) : "",
            " · ", data("span", when(d.sent_at)), d.images.length ? data("span", ` · 📷 ${d.images.length}`) : ""),
          d.result_refs ? h("div", { class: "item-sub" }, data("span", d.result_refs)) : "",
          d.reject_reason ? h("div", { class: "item-sub" }, data("span", d.reject_reason)) : "",
          h("div", null, warnChips(d.warnings)));
        list.append(item);
      });
    } catch (e) { list.replaceChildren(h("div", { class: "empty bad", text: e.message })); }
  }

  async function open(id, item) {
    current = id;
    list.querySelectorAll(".item").forEach(x => x.classList.toggle("on", x === item));
    panel.replaceChildren(h("div", { class: "empty", text: "Loading…" }));
    let d;
    try { d = await api(`/field-inbox/api/drafts/${id}`); }
    catch (e) { panel.replaceChildren(h("div", { class: "empty bad", text: e.message })); return; }
    const images = d.images.map(i => `/documents/file/${i}`);
    const parts = [
      h("div", { class: "meta" },
        h("strong", { text: KIND[d.kind] || d.kind }),
        h("span", null, h("span", { text: "From" }), " ", data("span", d.sender.name), d.sender.username ? data("span", ` (@${d.sender.username})`) : ""),
        data("span", when(d.sent_at))),
      h("div", null, warnChips(d.warnings)),
    ];
    if (d.source_text) parts.push(h("div", { class: "quote" }, h("div", { class: "quote-label", text: "The worker wrote" }), data("div", d.source_text)));
    if (d.worker_note) parts.push(h("div", { class: "quote" }, h("div", { class: "quote-label", text: "Worker's note" }), data("div", d.worker_note)));
    if (images.length) parts.push(h("div", { class: "photos" }, images.map(src => h("img", { src, alt: "", loading: "lazy", onclick: e => e.target.classList.toggle("big") }))));
    if (d.status !== "in_review") {
      parts.push(h("div", { class: "note", text: d.status === "confirmed" ? "Already confirmed." : "This draft was " + d.status + "." }));
      panel.replaceChildren(...parts); return;
    }
    if (d.own) parts.push(h("div", { class: "note", text: "You sent this yourself — someone else has to review it." }));
    else if (d.own_admin) parts.push(h("div", { class: "note", text: "You sent this yourself — as an admin you may still record it." }));
    else if (!d.can_review) parts.push(h("div", { class: "note", text: "You can see this draft but not confirm or reject it (you need the review permission and the permission for this record)." }));
    const make = window.AskDocuments && window.AskDocuments.cards[d.kind];
    if (make) {
      const card = make(d.draft, [], null, {
        submit: async body => {
          const out = await api(`/field-inbox/api/drafts/${id}/confirm`, body);
          setTimeout(() => { refreshBadge(); load(); }, 1200);
          return out;
        },
      });
      // The photos are shown above, full size; the card's own thumbnails would be empty.
      card.querySelectorAll(".doc-thumbs").forEach(x => x.remove());
      if (d.own || !d.can_review) card.querySelectorAll("button").forEach(b => { b.disabled = true; });
      parts.push(card);
    }
    if (!d.own && d.can_review) {
      const reason = h("input", { type: "text", maxlength: 300, placeholder: "Reason for the worker (required)", "data-no-i18n": true });
      const out = h("span", { class: "act-status" });
      const btn = h("button", { class: "act-btn", text: "Reject", onclick: async () => {
        if (!reason.value.trim()) { out.className = "act-status bad"; out.textContent = ""; out.append(h("span", { text: "Write a reason first." })); return; }
        btn.disabled = true;
        try {
          await api(`/field-inbox/api/drafts/${id}/reject`, { reason: reason.value.trim() });
          panel.replaceChildren(h("div", { class: "empty ok", text: "Rejected — the worker was told." }));
          refreshBadge(); load();
        } catch (e) { btn.disabled = false; out.className = "act-status bad"; out.replaceChildren(data("span", e.message)); }
      } });
      parts.push(h("div", { class: "reject" }, reason, btn, out));
    }
    panel.replaceChildren(...parts);
  }

  function refreshBadge() {
    api("/field-inbox/api/pending-count").then(d => {
      document.querySelectorAll('.app-nav-badge[data-badge-url="/field-inbox/api/pending-count"]').forEach(b => {
        b.textContent = d.count > 99 ? "99+" : String(d.count); b.hidden = !d.count;
      });
    }).catch(() => {});
  }

  document.getElementById("tabs").addEventListener("click", e => {
    const tab = e.target.closest(".tab"); if (!tab) return;
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("on", t === tab));
    status = tab.dataset.status; current = null;
    panel.replaceChildren(h("div", { class: "empty", text: "Pick a submission on the left." }));
    load();
  });

  // ── Admin: bot status, webhook, link codes, chats ──
  let adminReady = false, people = null;
  async function admin() {
    if (adminReady) return; adminReady = true;
    document.getElementById("admin").hidden = false;
    botStatus();
    document.getElementById("register").onclick = async () => {
      const out = document.getElementById("register-out");
      out.className = "act-status"; out.replaceChildren(h("span", { text: "Registering…" }));
      try { const r = await api("/telegram/api/admin/webhook", {}); out.className = "act-status ok"; out.replaceChildren(h("span", { text: "Webhook registered:" }), " ", data("span", r.url)); botStatus(); }
      catch (e) { out.className = "act-status bad"; out.replaceChildren(data("span", e.message)); }
    };
    people = await api("/telegram/api/admin/people");
    const who = document.getElementById("link-who"), user = document.getElementById("link-user");
    who.append(h("option", { value: "", text: "— employee or user —" }),
      h("optgroup", { label: "Employees" }, people.employees.map(p => h("option", { value: "e" + p.id, text: p.name, "data-no-i18n": true }))),
      h("optgroup", { label: "Users" }, people.users.map(p => h("option", { value: "u" + p.id, text: p.name, "data-no-i18n": true }))));
    user.append(h("option", { value: "", text: "— their AZed login (if any) —" }),
      ...people.users.map(p => h("option", { value: String(p.id), text: p.name, "data-no-i18n": true })));
    who.onchange = () => { user.disabled = who.value.startsWith("u"); if (user.disabled) user.value = ""; };
    const kinds = document.getElementById("link-kinds");
    people.kinds.forEach(k => kinds.append(h("label", null, h("input", { type: "checkbox", value: k, checked: true }), h("span", { text: KIND[k] || k }))));
    document.getElementById("link-make").onclick = async () => {
      const out = document.getElementById("link-out");
      const v = who.value, chosen = [...kinds.querySelectorAll("input:checked")].map(x => x.value);
      try {
        const r = await api("/telegram/api/admin/link-codes", {
          employee_id: v.startsWith("e") ? Number(v.slice(1)) : null,
          user_id: v.startsWith("u") ? Number(v.slice(1)) : (Number(user.value) || null),
          kinds: chosen, daily_limit: Number(document.getElementById("link-limit").value) || 30,
          language: document.getElementById("link-lang").value });
        out.className = "act-status ok";
        out.replaceChildren(h("span", { text: "Give this code to the worker (valid 24 hours, works once). In the bot they send:" }), " ",
          data("span", "/start " + r.code, "code"));
      } catch (e) { out.className = "act-status bad"; out.replaceChildren(data("span", e.message)); }
    };
    chats();
  }

  async function botStatus() {
    const box = document.getElementById("bot-status");
    try {
      const s = await api("/telegram/api/admin/status");
      const rows = [];
      rows.push(h("div", null, h("span", { text: "Bot token:" }), " ", h("span", { class: s.token_set ? "ok" : "bad", text: s.token_set ? "set" : "not set" }),
        " · ", h("span", { text: "Webhook secret:" }), " ", h("span", { class: s.secret_set ? "ok" : "bad", text: s.secret_set ? "set" : "not set" })));
      if (s.bot) rows.push(h("div", null, h("span", { text: "Bot:" }), " ", data("span", "@" + s.bot.username)));
      rows.push(h("div", null, h("span", { text: "Voice notes:" }), " ",
        s.voice_model ? data("span", s.voice_model, "ok") : h("span", { class: "bad", text: "off (set ASSISTANT_TRANSCRIBE_MODEL)" })));
      if (s.webhook) {
        const registered = s.webhook.url === s.webhook_url;
        rows.push(h("div", null, h("span", { text: "Webhook:" }), " ",
          h("span", { class: registered ? "ok" : "bad", text: registered ? "registered" : "not registered" }),
          " · ", h("span", { text: "waiting at Telegram:" }), " ", data("span", s.webhook.pending)));
        if (s.webhook.last_error) rows.push(h("div", { class: "bad" }, h("span", { text: "Last error:" }), " ", data("span", s.webhook.last_error)));
      }
      if (s.error) rows.push(h("div", { class: "bad" }, data("span", s.error)));
      box.replaceChildren(...rows);
    } catch (e) { box.replaceChildren(data("span", e.message)); }
  }

  async function chats() {
    const body = document.getElementById("chats");
    const out = await api("/telegram/api/admin/chats");
    body.replaceChildren();
    if (!out.chats.length) { body.append(h("tr", null, h("td", { colspan: 7, text: "No chats linked yet." }))); return; }
    out.chats.forEach(c => {
      const save = async patch => {
        try { await api(`/telegram/api/admin/chats/${c.id}`, patch); }
        catch (e) { alert(e.message); }
        chats();
      };
      const kinds = h("div", { class: "kinds" }, people.kinds.map(k => h("label", null,
        h("input", { type: "checkbox", value: k, checked: c.kinds.includes(k), disabled: !c.is_active,
          onchange: () => save({ kinds: [...kinds.querySelectorAll("input:checked")].map(x => x.value) }) }),
        h("span", { text: KIND[k] || k }))));
      const approves = h("label", { title: c.has_login ? "Gets new submissions to approve in Telegram" : "Needs an AZed login — link with a new code" },
        h("input", { type: "checkbox", checked: c.approver, disabled: !c.is_active || !c.has_login,
          onchange: e => save({ approver: e.target.checked }) }), " ", h("span", { text: "Approves" }));
      kinds.append(approves);
      const limit = h("input", { type: "number", min: 1, max: 500, value: c.daily_limit, style: "width:64px", disabled: !c.is_active,
        onchange: e => save({ daily_limit: Number(e.target.value) || 30 }) });
      const lang = h("select", { disabled: !c.is_active, onchange: e => save({ language: e.target.value }) },
        h("option", { value: "ar", text: "العربية" }), h("option", { value: "en", text: "English" }));
      lang.value = c.language;
      const unlink = h("span", { class: "act-buttons", style: "margin:0" },
        c.is_active
          ? h("button", { class: "act-btn", text: "Unlink", onclick: () => {
              if (confirm("Unlink this chat? It stops working until you relink it.")) save({ is_active: false });
            } })
          : h("button", { class: "act-btn", text: "Relink", onclick: () => save({ is_active: true }) }),
        h("button", { class: "act-btn", text: "Delete", onclick: async () => {
          if (!confirm("Delete this chat from the list? To use the bot again the worker will need a new code.")) return;
          try { await api(`/telegram/api/admin/chats/${c.id}`, undefined, "DELETE"); } catch (e) { alert(e.message); }
          chats();
        } }),
        c.is_active ? "" : h("span", { class: "bad", text: "unlinked" }));
      body.append(h("tr", null, h("td", null, data("span", c.person || "—")), h("td", null, data("span", c.username ? "@" + c.username : "—")),
        h("td", null, kinds), h("td", null, data("span", c.used_today)), h("td", null, limit), h("td", null, lang), h("td", null, unlink)));
    });
  }

  load().then(() => {
    const wanted = Number(new URLSearchParams(location.search).get("draft"));
    if (wanted) open(wanted, null);
  });
})();
