/*
 * Sales targets — one target per month with a floor and a stretch, against actual sales. The page frame is plans.js;
 * this file draws the cards and rows, and fills in the floor (85%) and stretch (120%) as a target is typed — unless
 * that floor or stretch was typed by hand.
 */
(function () {
  "use strict";
  const { MONTHS, h, num, money, pct, monthPace, mount } = window.PlanPage;
  let shares = { floor: 0.85, stretch: 1.2 };

  // The highest level reached: [label, class].
  function level(sales, m) {
    if (!m.sales_target || sales === null) return null;
    if (m.sales_stretch && sales >= m.sales_stretch) return ["Stretch reached", "best"];
    if (sales >= m.sales_target) return ["Target reached", "ok"];
    if (m.sales_floor && sales >= m.sales_floor) return ["Floor reached", "warn"];
    return [m.current ? "Below floor so far" : "Below floor", m.current ? "" : "bad"];
  }

  function targetCard(label, sales, m, pace) {
    const card = h("div", { class: "card" }, h("div", { class: "card-label" }, label));
    if (!m.sales_target) {
      card.append(h("div", { class: "card-big" }, num(money(sales))),
                  h("div", { class: "card-note" }, "No sales target set."));
      return card;
    }
    const top = Math.max(m.sales_stretch || m.sales_target, sales);   // the bar runs to the stretch
    const at = v => `${Math.min(100, v / top * 100)}%`;
    const lv = level(sales, m);
    const behind = pace !== undefined && sales < m.sales_target * pace;
    const barEl = h("div", { class: "bar" + (lv[1] === "best" ? " stretch" : behind ? " warn" : "") },
                    h("i", { style: `width:${at(sales)}` }));
    const scale = h("div", { class: "bar-scale" });
    [["Floor", m.sales_floor], ["Target", m.sales_target], ["Stretch", m.sales_stretch]].forEach(([name, v]) => {
      if (!v) return;
      if (name !== "Stretch") barEl.append(h("u", { style: `left:${at(v)}`, title: `${name} ${money(v)}` }));
      scale.append(h("span", { style: `left:${name === "Stretch" ? "calc(100% - 22px)" : at(v)}` }, name));
    });
    if (pace !== undefined) barEl.append(h("b", { style: `left:${at(m.sales_target * pace)}`, title: "Where sales should be by today for the target" }));

    let note;
    if (lv[1] === "best" || lv[1] === "ok") {
      note = h("span", { class: lv[1] }, lv[0], " — ", num(pct(sales, m.sales_target) + "%"), " of target");
    } else if (pace !== undefined) {
      note = behind
        ? h("span", { class: "warn" }, "Behind target pace by ", num(money(m.sales_target * pace - sales)))
        : h("span", { class: "ok" }, "On pace for the target");
    } else {
      note = h("span", { class: lv[1] }, lv[0], " — ", num(pct(sales, m.sales_target) + "%"), " of target");
    }
    card.append(h("div", { class: "card-big" }, num(money(sales)), " ", h("small", {}, "of ", num(money(m.sales_target)))),
                barEl, scale, h("div", { class: "card-note" }, note));
    return card;
  }

  mount({
    api: "/sales-targets",
    fields: ["sales_floor", "sales_target", "sales_stretch"],
    colspan: 7,
    cards(data) {
      shares = { floor: data.floor_share, stretch: data.stretch_share };
      const out = [];
      const cur = data.months.find(m => m.current);
      if (cur) out.push(targetCard(`${MONTHS[cur.month - 1]} so far`, cur.sales || 0, cur, monthPace()));
      // The year: only months that have started and have a target count toward it.
      const counted = data.months.filter(m => m.sales_target && m.sales !== null);
      const sum = key => counted.reduce((t, m) => t + (m[key] || 0), 0);
      out.push(targetCard(`${data.year} to date`, data.months.reduce((t, m) => t + (m.sales || 0), 0),
                          { sales_floor: sum("sales_floor"), sales_target: sum("sales_target"),
                            sales_stretch: sum("sales_stretch") }));
      return out;
    },
    row(m, cell) {
      const future = m.sales === null;
      const lv = level(m.sales, m);
      const of = m.sales_target && !future ? pct(m.sales, m.sales_target) : null;
      return h("tr", { class: m.current ? "now" : "" },
        h("td", {}, MONTHS[m.month - 1] + (m.current ? " · now" : "")),
        cell(m, "sales_floor", "floor", "small"),
        cell(m, "sales_target", "sales target"),
        cell(m, "sales_stretch", "stretch", "small"),
        h("td", { class: "num" + (future ? " future" : "") }, future ? "—" : num(money(m.sales))),
        h("td", { class: "num" }, of === null ? "—" : num(of + "%")),
        h("td", {}, lv ? h("span", { class: "pill " + lv[1] }, lv[0]) : "—"));
    },
    total(data) {
      const sum = key => data.months.reduce((t, m) => t + (m[key] || 0), 0);
      const fig = key => h("td", { class: "num" }, sum(key) ? num(money(sum(key))) : "—");
      return h("tr", { class: "total" }, h("td", {}, "Year"), fig("sales_floor"), fig("sales_target"),
               fig("sales_stretch"), h("td", { class: "num" }, num(money(sum("sales")))), h("td", {}, ""), h("td", {}, ""));
    },
    onInput(input, month, key, { inputOf, setValue }) {
      if (key !== "sales_target") {
        input.dataset.manual = "1";            // typed by hand: a new target no longer overwrites it
        return;
      }
      const target = Number(input.value.replace(/,/g, ""));
      const blank = !input.value.trim() || !isFinite(target);
      const saved = Number(input.defaultValue);
      const auto = (t, share) => Math.round(t * share * 100) / 100;
      [["sales_floor", shares.floor], ["sales_stretch", shares.stretch]].forEach(([k, share]) => {
        const other = inputOf(month, k);
        if (blank) { setValue(other, ""); delete other.dataset.manual; return; }
        // A saved floor/stretch that isn't the 85% / 120% of the saved target was set by hand: keep it.
        if (other.defaultValue && other.value === other.defaultValue && Math.abs(Number(other.defaultValue) - auto(saved, share)) > 0.01) {
          other.dataset.manual = "1";
        }
        if (!other.dataset.manual) setValue(other, String(auto(target, share)));
      });
    },
  });
})();
