/*
 * Budget & targets — one spending budget and one sales target per month, against actual sales and spending.
 *
 * Numbers come from /budget/api/year; people with action_budget_manage edit the target and budget cells in place and
 * save the whole year at once. Figures are set with textContent and marked data-no-i18n; fixed labels stay plain text
 * so i18n.js can translate them.
 */
(function () {
  "use strict";
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const thisYear = new Date().getFullYear();
  let year = Number(new URLSearchParams(location.search).get("year")) || thisYear;
  let data = null;
  let dirty = false;

  const $ = id => document.getElementById(id);
  const money = v => "EGP " + Math.round(Number(v || 0)).toLocaleString("en-GB");
  const pct = (a, b) => b > 0 ? Math.round(a / b * 100) : null;

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
  const num = text => h("span", { "data-no-i18n": true, text });

  // A progress bar: `value` of `goal`, with an optional marker where we should be by today.
  function bar(value, goal, kind, marker) {
    const share = goal > 0 ? Math.min(100, value / goal * 100) : 0;
    const el = h("div", { class: "bar" + (kind ? " " + kind : "") }, h("i", { style: `width:${share}%` }));
    if (marker !== undefined) el.append(h("b", { style: `left:${Math.min(100, marker * 100)}%`, title: "Where we should be by today" }));
    return el;
  }

  function salesCard(label, sales, target, pace) {
    const card = h("div", { class: "card" }, h("div", { class: "card-label" }, label));
    if (!target) {
      card.append(h("div", { class: "card-big" }, num(money(sales))),
                  h("div", { class: "card-note" }, "No sales target set."));
      return card;
    }
    const p = pct(sales, target);
    card.append(h("div", { class: "card-big" }, num(money(sales)), " ", h("small", {}, "of ", num(money(target)))),
                bar(sales, target, sales >= target ? "" : (pace !== undefined && sales < target * pace ? "warn" : ""), pace));
    let note;
    if (sales >= target) note = h("span", { class: "ok" }, "Target reached — ", num(p + "%"));
    else if (pace !== undefined) {
      const expected = target * pace;
      note = sales >= expected
        ? h("span", { class: "ok" }, "On pace — ", num(p + "%"), " of target")
        : h("span", { class: "warn" }, "Behind pace by ", num(money(expected - sales)), " — ", num(p + "%"), " of target");
    } else note = h("span", {}, num(p + "%"), " of target — ", num(money(target - sales)), " to go");
    card.append(h("div", { class: "card-note" }, note));
    return card;
  }

  function spendCard(label, spent, budget, pace) {
    const card = h("div", { class: "card" }, h("div", { class: "card-label" }, label));
    if (!budget) {
      card.append(h("div", { class: "card-big" }, num(money(spent))),
                  h("div", { class: "card-note" }, "No budget set."));
      return card;
    }
    const over = spent > budget;
    const ahead = pace !== undefined && spent > budget * pace;
    card.append(h("div", { class: "card-big" }, num(money(spent)), " ", h("small", {}, "of ", num(money(budget)))),
                bar(spent, budget, over ? "over" : ahead ? "warn" : "", pace),
                h("div", { class: "card-note" }, over
                  ? h("span", { class: "bad" }, "Over budget by ", num(money(spent - budget)))
                  : h("span", { class: ahead ? "warn" : "ok" }, num(money(budget - spent)), " left",
                      ahead ? " — spending faster than the month" : "")));
    return card;
  }

  function renderCards() {
    const box = $("cards");
    box.replaceChildren();
    const ms = data.months;
    const sum = key => ms.reduce((t, m) => t + (m[key] || 0), 0);
    const cur = ms.find(m => m.current);
    if (cur) {
      const now = new Date();
      const days = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
      const pace = now.getDate() / days;
      const name = MONTHS[cur.month - 1];
      box.append(salesCard(`Sales — ${name} so far`, cur.sales || 0, cur.sales_target, pace),
                 spendCard(`Spending — ${name} so far`, cur.spent || 0, cur.budget, pace));
    }
    // The year: only the months that have a number set count toward its target and budget.
    const withTarget = ms.filter(m => m.sales_target && m.sales !== null);
    const withBudget = ms.filter(m => m.budget && m.spent !== null);
    box.append(salesCard(`Sales — ${data.year} to date`, sum("sales"),
                         withTarget.reduce((t, m) => t + m.sales_target, 0)),
               spendCard(`Spending — ${data.year} to date`, sum("spent"),
                         withBudget.reduce((t, m) => t + m.budget, 0)));
  }

  function cell(m, key) {
    const value = m[key];
    if (!data.can_manage) return h("td", { class: "num" }, value ? num(money(value)) : "—");
    const input = h("input", {
      class: "amt", inputmode: "decimal", "data-month": m.month, "data-key": key, "data-no-i18n": true,
      value: value ? String(value) : "", placeholder: "—", "aria-label": `${MONTHS[m.month - 1]} ${key === "budget" ? "budget" : "sales target"}`,
      oninput: e => { e.target.classList.toggle("changed", e.target.value !== e.target.defaultValue); markDirty(); },
    });
    return h("td", {}, input);
  }

  function renderRows() {
    const body = $("rows");
    body.replaceChildren();
    let tT = 0, tS = 0, tB = 0, tSp = 0;
    data.months.forEach(m => {
      const future = m.sales === null;
      const reached = m.sales_target && !future ? pct(m.sales, m.sales_target) : null;
      const left = m.budget && !future ? m.budget - m.spent : null;
      tT += m.sales_target || 0; tB += m.budget || 0; tS += m.sales || 0; tSp += m.spent || 0;
      body.append(h("tr", { class: m.current ? "now" : "" },
        h("td", {}, MONTHS[m.month - 1] + (m.current ? " · now" : "")),
        cell(m, "sales_target"),
        h("td", { class: "num" + (future ? " future" : "") }, future ? "—" : num(money(m.sales))),
        h("td", { class: "num " + (reached === null ? "" : reached >= 100 ? "ok" : m.current ? "" : "warn") },
          reached === null ? "—" : num(reached + "%")),
        cell(m, "budget"),
        h("td", { class: "num" + (future ? " future" : "") }, future ? "—" : num(money(m.spent))),
        h("td", { class: "num " + (left === null ? "" : left < 0 ? "bad" : "ok") },
          left === null ? "—" : num((left < 0 ? "−" : "") + money(Math.abs(left))))));
    });
    body.append(h("tr", { class: "total" },
      h("td", {}, "Year"),
      h("td", { class: "num" }, tT ? num(money(tT)) : "—"),
      h("td", { class: "num" }, num(money(tS))),
      h("td", {}, ""),
      h("td", { class: "num" }, tB ? num(money(tB)) : "—"),
      h("td", { class: "num" }, num(money(tSp))),
      h("td", {}, "")));
  }

  function markDirty() {
    dirty = true;
    $("save").disabled = false;
    $("status").textContent = "Unsaved changes";
  }

  async function load() {
    $("year-label").textContent = year;
    history.replaceState(null, "", year === thisYear ? location.pathname : `?year=${year}`);
    try {
      const r = await fetch(`/budget/api/year?year=${year}`, { credentials: "same-origin" });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      data = await r.json();
    } catch (e) {
      $("rows").replaceChildren(h("tr", {}, h("td", { colspan: 7, class: "empty" }, "Couldn't load: " + e.message)));
      $("cards").replaceChildren();
      $("actions").hidden = true;
      return;
    }
    renderCards();
    renderRows();
    $("actions").hidden = !data.can_manage;
    $("save").disabled = true;
    $("status").textContent = "";
    const from = data.months.find(m => m.current) || data.months[0];
    $("fill").textContent = `Copy ${MONTHS[from.month - 1]}'s numbers to the months after it`;
    $("fill").dataset.from = from.month;
  }

  function goTo(y) {
    if (dirty && !confirm("You have unsaved changes. Leave them?")) return;
    dirty = false;
    year = y;
    load();
  }

  async function save() {
    const months = [];
    for (let m = 1; m <= 12; m++) {
      const get = key => document.querySelector(`input.amt[data-month="${m}"][data-key="${key}"]`).value.trim();
      months.push({ month: m, budget: get("budget"), sales_target: get("sales_target") });
    }
    $("save").disabled = true;
    $("status").textContent = "Saving…";
    try {
      const r = await fetch(`/budget/api/year/${year}`, {
        method: "PUT", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ months }),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(out.detail || r.statusText);
      dirty = false;
      await load();
      $("status").textContent = "Saved";
    } catch (e) {
      $("save").disabled = false;
      $("status").textContent = "Not saved: " + e.message;
    }
  }

  function fill() {
    const from = Number($("fill").dataset.from);
    const src = key => document.querySelector(`input.amt[data-month="${from}"][data-key="${key}"]`).value;
    for (let m = from + 1; m <= 12; m++) {
      ["budget", "sales_target"].forEach(key => {
        const input = document.querySelector(`input.amt[data-month="${m}"][data-key="${key}"]`);
        input.value = src(key);
        input.classList.toggle("changed", input.value !== input.defaultValue);
      });
    }
    markDirty();
  }

  $("prev-year").addEventListener("click", () => goTo(year - 1));
  $("next-year").addEventListener("click", () => goTo(year + 1));
  $("save").addEventListener("click", save);
  $("fill").addEventListener("click", fill);
  window.addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  load();
})();
