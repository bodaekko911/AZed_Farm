/*
 * Shared by the Budget and Sales targets pages: a year of monthly numbers, edited in place and saved at once.
 *
 * PlanPage.mount({api, fields, colspan, cards, row, total, onInput}) wires the year picker, loading, the editable
 * cells, "Copy to the months after" and Save. Figures are set with textContent and marked data-no-i18n; fixed labels
 * stay plain text so i18n.js can translate them.
 */
(function () {
  "use strict";
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
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

  // How far through the current month we are (0–1), for the "where we should be by today" marker.
  function monthPace() {
    const now = new Date();
    return now.getDate() / new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
  }

  function mount(cfg) {
    const thisYear = new Date().getFullYear();
    let year = Number(new URLSearchParams(location.search).get("year")) || thisYear;
    let data = null;
    let dirty = false;

    const inputOf = (m, key) => document.querySelector(`input.amt[data-month="${m}"][data-key="${key}"]`);

    function markDirty() {
      dirty = true;
      $("save").disabled = false;
      $("status").textContent = "Unsaved changes";
    }

    function setValue(input, value) {
      input.value = value;
      input.classList.toggle("changed", input.value !== input.defaultValue);
    }

    // An amount cell: an input for people who may set it, the figure for everyone else.
    function cell(m, key, label, extraClass) {
      const value = m[key];
      if (!data.can_manage) return h("td", { class: "num" }, value ? num(money(value)) : "—");
      const input = h("input", {
        class: "amt" + (extraClass ? " " + extraClass : ""), inputmode: "decimal", "data-month": m.month,
        "data-key": key, "data-no-i18n": true, value: value ? String(value) : "", placeholder: "—",
        "aria-label": `${MONTHS[m.month - 1]} ${label}`,
        oninput: e => {
          e.target.classList.toggle("changed", e.target.value !== e.target.defaultValue);
          if (cfg.onInput) cfg.onInput(e.target, m.month, key, { inputOf, setValue });
          markDirty();
        },
      });
      return h("td", {}, input);
    }

    async function load() {
      $("year-label").textContent = year;
      history.replaceState(null, "", year === thisYear ? location.pathname : `?year=${year}`);
      try {
        const r = await fetch(`${cfg.api}/api/year?year=${year}`, { credentials: "same-origin" });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
        data = await r.json();
      } catch (e) {
        $("rows").replaceChildren(h("tr", {}, h("td", { colspan: cfg.colspan, class: "empty" }, "Couldn't load: " + e.message)));
        $("cards").replaceChildren();
        $("actions").hidden = true;
        return;
      }
      $("cards").replaceChildren(...cfg.cards(data));
      $("rows").replaceChildren(...data.months.map(m => cfg.row(m, cell)), cfg.total(data));
      $("actions").hidden = !data.can_manage;
      $("save").disabled = true;
      $("status").textContent = "";
      const from = data.months.find(m => m.current) || data.months[0];
      $("fill").textContent = `Copy ${MONTHS[from.month - 1]} to the months after`;
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
        const item = { month: m };
        cfg.fields.forEach(key => { item[key] = inputOf(m, key).value.trim(); });
        months.push(item);
      }
      $("save").disabled = true;
      $("status").textContent = "Saving…";
      try {
        const r = await fetch(`${cfg.api}/api/year/${year}`, {
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
      for (let m = from + 1; m <= 12; m++) {
        cfg.fields.forEach(key => setValue(inputOf(m, key), inputOf(from, key).value));
      }
      markDirty();
    }

    $("prev-year").addEventListener("click", () => goTo(year - 1));
    $("next-year").addEventListener("click", () => goTo(year + 1));
    $("save").addEventListener("click", save);
    $("fill").addEventListener("click", fill);
    window.addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
    load();
  }

  window.PlanPage = { MONTHS, h, num, money, pct, monthPace, mount };
})();
