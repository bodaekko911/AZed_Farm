/*
 * Sales dashboard — this month's sales target and spending budget, from /budget/api/current.
 *
 * Stays hidden for anyone without the Budget & targets page (the request is refused) or if it fails. The marker on
 * each bar is where the month should be by today. Figures are marked data-no-i18n.
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

  function meter(label, value, goal, pace, kind, note, noteClass) {
    const share = Math.min(100, value / goal * 100);
    return h("div", {},
      h("div", { class: "budget-meter-head" }, h("span", {}, label),
        h("span", {}, num(money(value)), " / ", num(money(goal)))),
      h("div", { class: "budget-meter-bar" + (kind ? " " + kind : "") },
        h("i", { style: `width:${share}%` }),
        h("b", { style: `left:${pace * 100}%`, title: "Where the month should be by today" })),
      h("div", { class: "budget-meter-note " + (noteClass || "") }, note));
  }

  fetch("/budget/api/current", { credentials: "same-origin" })
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (!d) return;
      const pace = d.day / d.days_in_month;
      const month = MONTHS[d.month - 1];
      const parts = [];
      if (d.sales_target) {
        const expected = d.sales_target * pace;
        const pct = Math.round(d.sales / d.sales_target * 100);
        const done = d.sales >= d.sales_target, behind = !done && d.sales < expected;
        parts.push(meter(`${month} sales target`, d.sales, d.sales_target, pace, behind ? "warn" : "",
          done ? `Target reached — ${pct}%`
               : behind ? `${pct}% — behind pace by ${money(expected - d.sales)}` : `${pct}% — on pace`,
          done || !behind ? "ok" : "warn"));
      }
      if (d.budget) {
        const over = d.spent > d.budget, fast = !over && d.spent > d.budget * pace;
        parts.push(meter(`${month} budget`, d.spent, d.budget, pace, over ? "over" : fast ? "warn" : "",
          over ? `Over budget by ${money(d.spent - d.budget)}`
               : `${money(d.budget - d.spent)} left` + (fast ? " — spending faster than the month" : ""),
          over ? "bad" : fast ? "warn" : "ok"));
      }
      if (!parts.length) {
        parts.push(h("div", { class: "budget-strip-empty" }, `No sales target or budget set for ${month}.`));
      } else if (parts.length === 1) {
        parts.push(h("div", {}));
      }
      parts.push(h("a", { class: "budget-strip-link", href: "/budget/" }, "Budget & targets ›"));
      strip.replaceChildren(...parts);
      strip.hidden = false;
    })
    .catch(() => {});
})();
