/*
 * Sales dashboard — this month's sales target (with its floor and stretch) and spending budget, from
 * /sales-targets/api/current and /budget/api/current.
 *
 * Each half shows only for people who may open that page (the other request is refused); the strip stays hidden if
 * neither does. The dark marker on a bar is where the month should be by today. Figures are marked data-no-i18n.
 */
(function () {
  "use strict";
  const strip = document.getElementById("budget-strip");
  if (!strip) return;
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                  "November", "December"];
  const money = v => "EGP " + Math.round(Number(v || 0)).toLocaleString("en-GB");

  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const k in attrs || {}) {
      if (k === "class") node.className = attrs[k];
      else if (k === "text") node.textContent = attrs[k];
      else node.setAttribute(k, attrs[k] === true ? "" : attrs[k]);
    }
    kids.forEach(k => node.append(k instanceof Node ? k : String(k)));
    return node;
  }
  const num = text => h("strong", { "data-no-i18n": true, text });
  const get = url => fetch(url, { credentials: "same-origin" }).then(r => r.ok ? r.json() : null).catch(() => null);

  // A bar from 0 to `top`: filled to `value`, with thin ticks and the pace marker.
  function meter(label, value, goal, top, ticks, pace, kind, note, noteClass) {
    const at = v => `${Math.min(100, v / top * 100)}%`;
    const bar = h("div", { class: "budget-meter-bar" + (kind ? " " + kind : "") }, h("i", { style: `width:${at(value)}` }));
    ticks.forEach(([name, v]) => bar.append(h("u", { style: `left:${at(v)}`, title: `${name} ${money(v)}` })));
    bar.append(h("b", { style: `left:${at(goal * pace)}`, title: "Where the month should be by today" }));
    return h("div", {},
      h("div", { class: "budget-meter-head" }, h("span", {}, label),
        h("span", {}, num(money(value)), " / ", num(money(goal)))),
      bar,
      h("div", { class: "budget-meter-note " + (noteClass || "") }, note));
  }

  function salesMeter(d, month) {
    const pace = d.day / d.days_in_month;
    const t = d.sales_target, s = d.sales;
    const top = Math.max(d.sales_stretch || t, s);
    const pctOf = Math.round(s / t * 100);
    let note, cls, kind = "";
    if (d.sales_stretch && s >= d.sales_stretch) { note = `Stretch reached — ${pctOf}% of target`; cls = "best"; kind = "stretch"; }
    else if (s >= t) { note = `Target reached — ${pctOf}%`; cls = "ok"; }
    else if (s < t * pace) {
      note = `${pctOf}% — behind target pace by ${money(t * pace - s)}` + (d.sales_floor && s >= d.sales_floor ? " · floor reached" : "");
      cls = "warn"; kind = "warn";
    } else { note = `${pctOf}% — on pace for the target`; cls = "ok"; }
    const ticks = [["Floor", d.sales_floor], ["Target", t]].filter(([, v]) => v);
    return meter(`${month} sales target`, s, t, top, ticks, pace, kind, note, cls);
  }

  function budgetMeter(d, month) {
    const pace = d.day / d.days_in_month;
    const over = d.spent > d.budget, fast = !over && d.spent > d.budget * pace;
    return meter(`${month} budget`, d.spent, d.budget, d.budget, [], pace, over ? "over" : fast ? "warn" : "",
      over ? `Over budget by ${money(d.spent - d.budget)}`
           : `${money(d.budget - d.spent)} left` + (fast ? " — spending faster than the month" : ""),
      over ? "bad" : fast ? "warn" : "ok");
  }

  Promise.all([get("/sales-targets/api/current"), get("/budget/api/current")]).then(([sales, budget]) => {
    if (!sales && !budget) return;
    const month = MONTHS[(sales || budget).month - 1];
    const parts = [];
    const links = h("div", { class: "budget-strip-links" });
    if (sales) {
      parts.push(sales.sales_target ? salesMeter(sales, month)
                                    : h("div", { class: "budget-strip-empty" }, `No sales target set for ${month}.`));
      links.append(h("a", { class: "budget-strip-link", href: "/sales-targets/" }, "Sales targets ›"));
    }
    if (budget) {
      parts.push(budget.budget ? budgetMeter(budget, month)
                               : h("div", { class: "budget-strip-empty" }, `No budget set for ${month}.`));
      links.append(h("a", { class: "budget-strip-link", href: "/budget/" }, "Budget ›"));
    }
    if (parts.length === 1) parts.push(h("div", {}));
    parts.push(links);
    strip.replaceChildren(...parts);
    strip.hidden = false;
  });
})();
