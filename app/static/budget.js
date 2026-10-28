/*
 * Budget — one spending budget per month against what was spent. The page frame is plans.js; this file draws the
 * cards and rows.
 */
(function () {
  "use strict";
  const { MONTHS, h, num, money, pct, monthPace, mount } = window.PlanPage;

  function spendCard(label, spent, budget, pace) {
    const card = h("div", { class: "card" }, h("div", { class: "card-label" }, label));
    if (!budget) {
      card.append(h("div", { class: "card-big" }, num(money(spent))),
                  h("div", { class: "card-note" }, "No budget set."));
      return card;
    }
    const over = spent > budget;
    const fast = !over && pace !== undefined && spent > budget * pace;
    const barEl = h("div", { class: "bar" + (over ? " over" : fast ? " warn" : "") },
                    h("i", { style: `width:${Math.min(100, spent / budget * 100)}%` }));
    if (pace !== undefined) barEl.append(h("b", { style: `left:${pace * 100}%`, title: "Where the month should be by today" }));
    card.append(h("div", { class: "card-big" }, num(money(spent)), " ", h("small", {}, "of ", num(money(budget)))),
                barEl,
                h("div", { class: "card-note" }, over
                  ? h("span", { class: "bad" }, "Over budget by ", num(money(spent - budget)))
                  : h("span", { class: fast ? "warn" : "ok" }, num(money(budget - spent)), " left",
                      fast ? " — spending faster than the month" : "")));
    return card;
  }

  mount({
    api: "/budget",
    fields: ["budget"],
    colspan: 5,
    cards(data) {
      const out = [];
      const cur = data.months.find(m => m.current);
      if (cur) out.push(spendCard(`${MONTHS[cur.month - 1]} so far`, cur.spent || 0, cur.budget, monthPace()));
      // The year: only months that have started and have a budget count toward it.
      const counted = data.months.filter(m => m.budget && m.spent !== null);
      out.push(spendCard(`${data.year} to date`, data.months.reduce((t, m) => t + (m.spent || 0), 0),
                         counted.reduce((t, m) => t + m.budget, 0)));
      return out;
    },
    row(m, cell) {
      const future = m.spent === null;
      const used = m.budget && !future ? pct(m.spent, m.budget) : null;
      const left = m.budget && !future ? m.budget - m.spent : null;
      return h("tr", { class: m.current ? "now" : "" },
        h("td", {}, MONTHS[m.month - 1] + (m.current ? " · now" : "")),
        cell(m, "budget", "budget"),
        h("td", { class: "num" + (future ? " future" : "") }, future ? "—" : num(money(m.spent))),
        h("td", { class: "num " + (used === null ? "" : used > 100 ? "bad" : "") }, used === null ? "—" : num(used + "%")),
        h("td", { class: "num " + (left === null ? "" : left < 0 ? "bad" : "ok") },
          left === null ? "—" : num((left < 0 ? "−" : "") + money(Math.abs(left)))));
    },
    total(data) {
      const sum = key => data.months.reduce((t, m) => t + (m[key] || 0), 0);
      return h("tr", { class: "total" }, h("td", {}, "Year"),
        h("td", { class: "num" }, sum("budget") ? num(money(sum("budget"))) : "—"),
        h("td", { class: "num" }, num(money(sum("spent")))), h("td", {}, ""), h("td", {}, ""));
    },
  });
})();
