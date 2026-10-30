/*
 * Treasury — balance cards for the bank and the cash box, money in / out by source, and a statement with a running
 * balance. People with action_treasury_manage can also move money between the two and set an opening balance.
 * Uses h() and num() from plans.js; figures are marked data-no-i18n, fixed labels stay plain for i18n.js.
 */
(function () {
  "use strict";
  const { h, num } = window.PlanPage;
  const $ = id => document.getElementById(id);
  const fmt = v => Number(v || 0).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const money = v => num((v < 0 ? "−" : "") + "EGP " + fmt(Math.abs(v)));
  const iso = d => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  const ACCOUNT_LABEL = { "1000": "Cash box", "1010": "Bank" };

  const today = new Date();
  const params = new URLSearchParams(location.search);
  $("from").value = params.get("from") || iso(new Date(today.getFullYear(), today.getMonth(), 1));
  $("to").value = params.get("to") || iso(today);
  let account = "all";
  let canManage = false;

  async function api(path, opts) {
    const r = await fetch("/treasury" + path, Object.assign({ credentials: "same-origin" }, opts || {}));
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof out.detail === "string" ? out.detail : r.statusText);
    return out;
  }
  const period = () => `from_date=${$("from").value}&to_date=${$("to").value}`;

  function accountCard(a, hasOpening) {
    const card = h("div", { class: "card" },
      h("div", { class: "card-label" }, ACCOUNT_LABEL[a.code] || a.name),
      h("div", { class: "card-big" + (a.closing < 0 ? " bad" : "") }, money(a.closing)),
      h("div", { class: "flow" },
        h("span", { class: "ok" }, "+ ", num(fmt(a.money_in))),
        h("span", { class: "bad" }, "− ", num(fmt(a.money_out)))),
      h("div", { class: "card-note" }, "Started the period at ", money(a.opening)));
    if (!hasOpening && canManage) {
      card.append(h("div", { class: "card-note" }, "No opening balance yet — ",
        h("button", { type: "button", onclick: () => showForm("opening-form", a.code) }, "set one"), "."));
    }
    return card;
  }

  function drawSummary(data) {
    const cards = $("cards");
    cards.replaceChildren(
      ...data.accounts.map(a => accountCard(a, data.has_opening[a.code])),
      h("div", { class: "card total" },
        h("div", { class: "card-label" }, "Total money"),
        h("div", { class: "card-big" + (data.total.closing < 0 ? " bad" : "") }, money(data.total.closing)),
        h("div", { class: "flow" },
          h("span", { class: "ok" }, "+ ", num(fmt(data.total.money_in))),
          h("span", { class: "bad" }, "− ", num(fmt(data.total.money_out)))),
        h("div", { class: "card-note" }, "Transfers between the cash box and the bank are not counted here.")));

    const body = $("sources");
    if (!data.by_source.length) {
      body.replaceChildren(h("tr", {}, h("td", { colspan: 4, class: "empty" }, "No money moved in this period.")));
      return;
    }
    body.replaceChildren(...data.by_source.map(s => {
      const net = s.money_in - s.money_out;
      return h("tr", {}, h("td", {}, s.source),
        h("td", { class: "num" }, s.money_in ? num(fmt(s.money_in)) : "—"),
        h("td", { class: "num" }, s.money_out ? num(fmt(s.money_out)) : "—"),
        h("td", { class: "num " + (net < 0 ? "bad" : "ok") }, money(net)));
    }), h("tr", { class: "total" }, h("td", {}, "Total"),
      h("td", { class: "num" }, num(fmt(data.total.money_in))),
      h("td", { class: "num" }, num(fmt(data.total.money_out))),
      h("td", { class: "num" }, money(data.total.money_in - data.total.money_out))));
  }

  function drawStatement(data) {
    const rows = [h("tr", { class: "opening" }, h("td", {}, num(data.from)), h("td", {}, "Balance at the start"),
      h("td", {}, ""), h("td", {}, ""), h("td", {}, ""), h("td", {}, ""),
      h("td", { class: "num" }, money(data.opening)), h("td", {}, ""))];
    data.lines.forEach(l => rows.push(h("tr", {},
      h("td", {}, num(l.date || "")),
      h("td", {}, num(l.description)),
      h("td", {}, l.source),
      h("td", {}, ACCOUNT_LABEL[l.account_code] || l.account_name),
      h("td", { class: "num ok" }, l.money_in ? num(fmt(l.money_in)) : ""),
      h("td", { class: "num bad" }, l.money_out ? num(fmt(l.money_out)) : ""),
      h("td", { class: "num" }, money(l.balance)),
      h("td", {}, l.manual && canManage
        ? h("button", { class: "del", title: "Delete", "aria-label": "Delete", onclick: () => remove(l) }, "×")
        : ""))));
    if (!data.lines.length) rows.push(h("tr", {}, h("td", { colspan: 8, class: "empty" }, "No movements in this period.")));
    $("lines").replaceChildren(...rows);
  }

  async function load() {
    history.replaceState(null, "", `?from=${$("from").value}&to=${$("to").value}`);
    try {
      const [summary, stmt] = await Promise.all([
        api(`/api/summary?${period()}`),
        api(`/api/statement?account=${account}&${period()}`),
      ]);
      canManage = summary.can_manage;
      $("manage").hidden = !canManage;
      drawSummary(summary);
      drawStatement(stmt);
    } catch (e) {
      $("lines").replaceChildren(h("tr", {}, h("td", { colspan: 8, class: "empty bad" }, "Could not load: " + e.message)));
    }
  }

  async function remove(line) {
    if (!confirm(`Delete "${line.description}"?`)) return;
    try {
      await api(`/api/movements/${line.journal_id}`, { method: "DELETE" });
      load();
    } catch (e) { alert(e.message); }
  }

  function showForm(id, accountCode) {
    ["transfer-form", "opening-form"].forEach(f => { $(f).hidden = f !== id; });
    if (id === "opening-form" && accountCode) $("o-account").value = accountCode;
    const dateInput = id === "transfer-form" ? $("t-date") : $("o-date");
    if (!dateInput.value) dateInput.value = iso(today);
    $(id).scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function bindForm(id, statusId, path, payload) {
    $(id).addEventListener("submit", async ev => {
      ev.preventDefault();
      $(statusId).textContent = "Saving…";
      try {
        await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
        $(id).reset();
        $(id).hidden = true;
        $(statusId).textContent = "";
        load();
      } catch (e) { $(statusId).textContent = e.message; }
    });
  }

  bindForm("transfer-form", "t-status", "/api/transfers", () => ({
    from_code: $("t-from").value, to_code: $("t-to").value, amount: Number($("t-amount").value),
    on_date: $("t-date").value || null, note: $("t-note").value || null,
  }));
  bindForm("opening-form", "o-status", "/api/opening-balance", () => ({
    account_code: $("o-account").value, amount: Number($("o-amount").value),
    on_date: $("o-date").value || null, note: $("o-note").value || null,
  }));
  // Picking one side of a transfer flips the other, since there are only two accounts.
  $("t-from").addEventListener("change", () => { $("t-to").value = $("t-from").value === "1000" ? "1010" : "1000"; });
  $("t-to").addEventListener("change", () => { $("t-from").value = $("t-to").value === "1000" ? "1010" : "1000"; });
  $("open-transfer").addEventListener("click", () => showForm("transfer-form"));
  $("open-opening").addEventListener("click", () => showForm("opening-form"));
  document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => { $(b.dataset.close).hidden = true; }));
  document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", async () => {
    document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("on", x === b));
    account = b.dataset.account;
    try { drawStatement(await api(`/api/statement?account=${account}&${period()}`)); }
    catch (e) { $("lines").replaceChildren(h("tr", {}, h("td", { colspan: 8, class: "empty bad" }, e.message))); }
  }));
  $("from").addEventListener("change", load);
  $("to").addEventListener("change", load);
  load();
})();
