/*
 * Energy Control card — plan timeline, planned vs actual, summary.
 * Dependency-free web component; data via websocket `energy_control/subscribe`.
 */
const CARD_VERSION = "0.2.0";

// What the battery does in a slot: the plan action, with self consumption
// split by direction so "battery covers the house" is visible at a glance.
const KINDS = {
  cover: { color: "var(--ec-cover, #f0a94b)", label: "Battery → house" },
  store: { color: "var(--ec-store, #9ccc65)", label: "PV → battery" },
  idle: { color: "var(--ec-idle, #9aa3b2)", label: "Idle" },
  hold: { color: "var(--ec-hold, #5b8fd6)", label: "Hold (grid → house)" },
  charge_grid: { color: "var(--ec-charge, #2e9e5b)", label: "Charge from grid" },
  discharge_grid: { color: "var(--ec-discharge, #e0662f)", label: "Discharge to grid" },
};
const kindOf = (action, batteryKw) => {
  if (action !== "self_consume") return KINDS[action] ? action : "idle";
  if (batteryKw == null || Math.abs(batteryKw) < 1e-3) return "idle";
  return batteryKw < 0 ? "cover" : "store";
};
const C = {
  pv: "var(--ec-pv, #e0a800)",
  load: "var(--ec-load, #4f6d9a)",
  grid: "var(--ec-grid, #8e6cbf)",
  soc: "var(--ec-soc, #3b82f6)",
  actual: "var(--ec-actual, #d6455d)",
  price: "var(--ec-price, #9aa0a6)",
  neg: "var(--ec-neg, #d6455d)",
  charge: KINDS.charge_grid.color,
  discharge: KINDS.discharge_grid.color,
};

const AXIS_W = 48; // fixed y-axis column (stays visible while scrolling)
const RANGES = [12, 24, 36, 48];
const ZOOMS = [0, 24, 48, 96]; // px per hour; 0 = fit to card

const fmtTime = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtDay = (d) => d.toLocaleDateString([], { weekday: "short" });
const ct = (eur) => (eur == null ? "–" : `${(eur * 100).toFixed(1)} ct`);
const eur = (v) => (v == null ? "–" : `€ ${v.toFixed(2)}`);
const kw = (v) => (v == null ? "–" : `${v.toFixed(2)} kW`);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const legendHtml = (items) =>
  items.map(([c, l, style]) => `<span><i class="${style ?? ""}" style="--c:${c}"></i>${l}</span>`).join("");

function niceRange(min, max, pad = 0.08) {
  if (!isFinite(min) || !isFinite(max)) return [0, 1];
  if (min === max) return [min - 1, max + 1];
  const span = max - min;
  return [min - span * pad, max + span * pad];
}

class EnergyControlCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "timeline";
    this._data = null;
    this._width = 600;
    this._scroll = {};
  }

  static getStubConfig() {
    return { hours: 36, past_hours: 12 };
  }

  setConfig(config) {
    this._config = { hours: 36, past_hours: 12, hour_width: 0, ...config };
    // Older configs: "plan" / "history" tabs are now one timeline.
    if (this._config.tab) this._tab = this._config.tab === "summary" ? "summary" : "timeline";
    this._pastHours = Number(this._config.past_hours ?? this._config.history_hours ?? 12);
    this._hours = Number(this._config.hours);
    this._hourWidth = Number(this._config.hour_width) || 0;
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first || !this._unsub) this._subscribe();
  }

  getCardSize() {
    return 10;
  }

  getGridOptions() {
    return { columns: "full", min_columns: 6, rows: 10, min_rows: 6 };
  }

  connectedCallback() {
    this._ro = new ResizeObserver((entries) => {
      const w = Math.round(entries[0].contentRect.width);
      if (w > 0 && Math.abs(w - this._width) > 4) {
        this._width = w;
        this._render();
      }
    });
    this._ro.observe(this);
    if (this._hass && !this._unsub) this._subscribe();
  }

  disconnectedCallback() {
    this._ro?.disconnect();
    clearTimeout(this._retry);
    this._unsubscribe();
  }

  async _subscribe() {
    if (!this._hass || this._subscribing || !this.isConnected) return;
    this._subscribing = true;
    try {
      const msg = {
        type: "energy_control/subscribe",
        history_hours: Math.max(Number(this._config?.history_hours ?? 0), Number(this._config?.past_hours ?? 12)),
      };
      if (this._config?.entry_id) msg.entry_id = this._config.entry_id;
      this._unsub = await this._hass.connection.subscribeMessage((data) => {
        this._data = data;
        this._error = null;
        this._render();
      }, msg);
    } catch (err) {
      this._error = err?.message || String(err);
      this._render();
      // The entry may still be loading (e.g. right after a restart): retry.
      clearTimeout(this._retry);
      this._retry = setTimeout(() => {
        if (this.isConnected && !this._unsub) this._subscribe();
      }, 30000);
    } finally {
      this._subscribing = false;
    }
  }

  _unsubscribe() {
    if (this._unsub) {
      Promise.resolve(this._unsub).then((fn) => typeof fn === "function" && fn()).catch(() => {});
      this._unsub = null;
    }
  }

  // ------------------------------------------------------------------ render
  _render() {
    if (!this.shadowRoot) return;
    // Every update re-renders: keep the horizontal scroll position per tab.
    const scroller = this.shadowRoot.querySelector(".scroll");
    if (scroller) this._scroll[this._renderedTab] = { left: scroller.scrollLeft, atEnd: scroller.scrollLeft + scroller.clientWidth >= scroller.scrollWidth - 2 };
    const d = this._data;
    const title = this._config?.title ?? "Energy Control";
    let body;
    if (this._error) body = `<div class="msg err">${esc(this._error)}</div>`;
    else if (!d) body = `<div class="msg">Loading…</div>`;
    else if (this._tab === "timeline") body = this._timelineView(d);
    else body = this._summaryView(d);

    const tabs = [
      ["timeline", "Timeline"],
      ["summary", "Summary"],
    ]
      .map(([k, l]) => `<button class="tab ${this._tab === k ? "on" : ""}" data-tab="${k}">${l}</button>`)
      .join("");
    const controls = this._tab === "summary" ? "" : this._controls();

    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="head">
          <div class="title">${esc(title)}</div>
          ${d ? this._headline(d) : ""}
        </div>
        <div class="tabs">${tabs}<span class="spacer"></span>${controls}</div>
        <div class="body">${body}</div>
        <div class="tip" hidden></div>
      </ha-card>`;
    this._renderedTab = this._tab;
    this.shadowRoot.querySelectorAll(".tab").forEach((b) =>
      b.addEventListener("click", () => {
        this._tab = b.dataset.tab;
        this._render();
      }),
    );
    this.shadowRoot.querySelectorAll("[data-hours]").forEach((b) =>
      b.addEventListener("click", () => {
        this._hours = Number(b.dataset.hours);
        this._scroll = {};
        this._render();
      }),
    );
    this.shadowRoot.querySelectorAll("[data-zoom]").forEach((b) =>
      b.addEventListener("click", () => {
        const i = ZOOMS.indexOf(this._hourWidth);
        const next = Number(b.dataset.zoom) > 0 ? Math.min(i + 1, ZOOMS.length - 1) : Math.max(i - 1, 0);
        this._hourWidth = ZOOMS[next < 0 ? 0 : next];
        this._render();
      }),
    );
    this._restoreScroll();
    this._bindHover();
  }

  _controls() {
    const zoomIdx = Math.max(0, ZOOMS.indexOf(this._hourWidth));
    const ranges = RANGES.map(
      (h) => `<button class="seg ${this._hours === h ? "on" : ""}" data-hours="${h}" title="Plan hours ahead">${h}h</button>`,
    ).join("");
    return `<span class="ctl">${ranges}
      <button class="seg" data-zoom="-1" title="Zoom out (fit)" ${zoomIdx === 0 ? "disabled" : ""}>−</button>
      <button class="seg" data-zoom="1" title="Zoom in (scroll)" ${zoomIdx === ZOOMS.length - 1 ? "disabled" : ""}>+</button></span>`;
  }

  _restoreScroll() {
    const scroller = this.shadowRoot.querySelector(".scroll");
    if (!scroller) return;
    const saved = this._scroll[this._tab];
    if (saved) scroller.scrollLeft = saved.left;
    else if (this._nowX != null) scroller.scrollLeft = Math.max(0, this._nowX - scroller.clientWidth / 4);
  }

  _headline(d) {
    const now = new Date();
    const slot = d.plan?.slots?.find((s) => {
      const t = new Date(s.start);
      return t <= now && now < new Date(t.getTime() + s.hours * 3600e3);
    });
    const action = d.desired?.action ?? slot?.action;
    const kind = action ? KINDS[kindOf(action, slot?.battery_kw)] : null;
    const chips = [
      `<span class="chip">${esc(d.mode)}</span>`,
      kind ? `<span class="chip" style="background:${kind.color}">${esc(kind.label)}</span>` : "",
      d.soc != null ? `<span class="chip ghost">SoC ${Math.round(d.soc)}%</span>` : "",
      d.curtailing ? `<span class="chip" style="background:${C.neg}">PV curtailed</span>` : "",
      d.override ? `<span class="chip ghost">override until ${fmtTime(new Date(d.override.until))}</span>` : "",
      d.status && !["ok", "advisory", "off"].includes(d.status) ? `<span class="chip warn">${esc(d.status)}</span>` : "",
    ].join("");
    const reason = slot?.reason ? `<div class="reason">${esc(slot.reason)}</div>` : "";
    return `<div class="chips">${chips}</div>${reason}`;
  }

  /** Horizontal frame: fit to the card, or a fixed px/hour that scrolls. */
  _frame(t0, tEnd) {
    const avail = Math.max(120, this._width - AXIS_W);
    const hours = (tEnd - t0) / 3600e3;
    const W = Math.max(avail, this._hourWidth ? Math.round(hours * this._hourWidth) : 0);
    const pad = 6;
    const x = (t) => pad + ((t - t0) / (tEnd - t0 || 1)) * (W - 2 * pad);
    return { W, x, scrolls: W > avail + 1, pxPerHour: (W - 2 * pad) / (hours || 1) };
  }

  _chart(main, axis, legend) {
    this._chartWidth = main.w;
    return `<div class="chart">
        <div class="yaxis">${axis.render(AXIS_W)}</div>
        <div class="scroll">${main.render()}</div>
      </div>
      <div class="legend">${legend}</div>`;
  }

  // --------------------------------------------------------- timeline view
  /**
   * One time axis: the past (left of "now") shows what was planned for each
   * slot next to what actually happened; the future shows the plan.
   * Everywhere: bars / blue = plan, red = actual.
   */
  _timelineView(d) {
    const now = Date.now();
    const SLOT = 15 * 60e3;
    const t0 = Math.floor((now - this._pastHours * 3600e3) / SLOT) * SLOT;
    const past = (d.history ?? [])
      .map((r) => ({ ...r, t: new Date(r.start).getTime() }))
      .filter((r) => r.t >= t0 && r.t < now)
      .map((r) => ({ ...r, t1: r.t + SLOT, kind: r.planned_action ? kindOf(r.planned_action, r.planned_battery_kw) : null }));
    const pastEnd = past.length ? past[past.length - 1].t1 : -Infinity;
    const planSlots = (d.plan?.slots ?? []).map((s) => ({ ...s, t: new Date(s.start).getTime() }));
    const last = planSlots[planSlots.length - 1];
    const planEnd = last ? last.t + last.hours * 3600e3 : now;
    const tEnd = Math.max(now + 3600e3, Math.min(planEnd, now + this._hours * 3600e3));
    const future = planSlots
      .map((s) => ({ ...s, t1: s.t + s.hours * 3600e3, kind: kindOf(s.action, s.battery_kw) }))
      .filter((s) => s.t1 > Math.max(pastEnd, t0) && s.t < tEnd)
      .map((s) => ({ ...s, t: Math.max(s.t, pastEnd, t0) }));
    if (!past.length && !future.length) return `<div class="msg">No plan${d.last_error ? `: ${esc(d.last_error)}` : ""}</div>`;

    const { W, x, pxPerHour } = this._frame(t0, tEnd);
    const wOf = (r) => Math.max(1, x(r.t1) - x(r.t));
    const g = new Svg(W);
    const a = new Svg(AXIS_W);
    const nowX = x(now);
    this._nowX = nowX;

    this._hoverRows = [
      ...past.map((r) => ({
        t: r.t,
        t1: r.t1,
        html: `<b>${fmtDay(new Date(r.t))} ${fmtTime(new Date(r.t))}</b> <span class="muted">· past</span>
          <div><i style="--c:${r.kind ? KINDS[r.kind].color : C.price}"></i>Planned: ${esc(r.kind ? KINDS[r.kind].label : "–")}</div>
          <table class="cmp"><tr><th></th><th>plan</th><th>actual</th></tr>
            <tr><td>Battery</td><td>${kw(r.planned_battery_kw)}</td><td>${kw(r.actual_battery_kw)}</td></tr>
            <tr><td>Grid</td><td>${kw(r.planned_grid_kw)}</td><td>${kw(r.actual_grid_kw)}</td></tr>
            <tr><td>SoC</td><td>${r.planned_soc?.toFixed?.(0) ?? "–"}%</td><td>${r.actual_soc?.toFixed?.(0) ?? "–"}%</td></tr>
            <tr><td>Cost (observed)</td><td>${eur(r.planned_cost)}</td><td>${eur(r.actual_cost)}</td></tr></table>
          <div>Measured coverage ${r.coverage == null ? "unknown" : `${(r.coverage * 100).toFixed(0)}%`}</div>
          ${r.extrapolated_cost != null && r.coverage < 0.98 ? `<div>Full-slot estimate ${eur(r.extrapolated_cost)}</div>` : ""}
          <div>Load error ${kw(r.load_error_kw)} · PV error ${kw(r.pv_error_kw)}</div>
          <div class="muted">Import ${ct(r.import_price)} · mode ${esc(r.mode)}${r.curtailed ? " · curtailed" : ""}</div>`,
      })),
      ...future.map((s) => ({
        t: s.t,
        t1: s.t1,
        html: `<b>${fmtDay(new Date(s.t))} ${fmtTime(new Date(s.t))}</b> <span class="muted">· plan</span>
          <div><i style="--c:${KINDS[s.kind].color}"></i>${esc(KINDS[s.kind].label)}</div>
          ${s.reason ? `<div class="r">${esc(s.reason)}</div>` : ""}
          <div>Battery ${kw(s.battery_kw)} · Grid ${kw(s.grid_kw)}</div>
          <div>PV ${kw(s.pv_kw)} · Load ${kw(s.load_kw)}</div>
          ${s.curtail_kw > 0 ? `<div>Curtail ${kw(s.curtail_kw)}</div>` : ""}
          <div>Import ${ct(s.import_price)} · Export ${ct(s.export_price_known === false ? null : s.export_price)}</div>
          <div>SoC → ${s.soc_end_pct.toFixed(0)}%</div>`,
      })),
    ];

    // Action band: what was / is planned.
    const bandY = 20, bandH = 20;
    a.text(AXIS_W - 6, bandY + 14, "Action", "pane", "end");
    g.band(
      [...past.filter((r) => r.kind), ...future].map((r) => ({ x0: x(r.t), x1: x(r.t) + wOf(r), key: r.kind })),
      bandY,
      bandH,
      KINDS,
    );
    if (nowX > 150) g.text(nowX - 6, 13, "◀ past: plan vs actual", "era", "end");
    g.text(nowX + 6, 13, "plan ▶", "era");

    // Battery pane: planned bars (faded in the past), actual as a red line.
    const bY = bandY + bandH + 40, bH = 140;
    const bVals = [...past.flatMap((r) => [r.planned_battery_kw, r.actual_battery_kw]), ...future.map((s) => s.battery_kw)].filter((v) => v != null);
    const [bMin, bMax] = niceRange(Math.min(-0.5, ...bVals), Math.max(0.5, ...bVals));
    const by = (v) => bY + bH - ((v - bMin) / (bMax - bMin)) * bH;
    g.paneGrid(by, [bMin, bMax], W);
    a.paneAxis(by, [bMin, bMax], "Battery", "kW");
    const bar = (r, v, op) => {
      if (v == null || Math.abs(v) < 1e-3) return;
      const key = v > 0 ? (r.kind === "charge_grid" ? "charge_grid" : "store") : r.kind === "discharge_grid" ? "discharge_grid" : "cover";
      g.rect(x(r.t) + 0.5, Math.min(by(v), by(0)), Math.max(0.5, wOf(r) - 1), Math.abs(by(v) - by(0)), KINDS[key].color, op);
    };
    for (const r of past) bar(r, r.planned_battery_kw, 0.35);
    for (const s of future) bar(s, s.battery_kw, 0.9);
    g.path(stepLine(past, "actual_battery_kw", x, by), C.actual, { width: 2 });
    g.text(x(t0) + 4, by(bMax) + 12, "▲ charge", "hint");
    g.text(x(t0) + 4, by(bMin) - 4, "▼ discharge", "hint");

    // Grid pane: plan dashed, actual red.
    const gY = bY + bH + 40, gH = 110;
    const gVals = [...past.flatMap((r) => [r.planned_grid_kw, r.actual_grid_kw]), ...future.map((s) => s.grid_kw)].filter((v) => v != null);
    const [gMin, gMax] = niceRange(Math.min(0, ...gVals), Math.max(0.5, ...gVals));
    const gy = (v) => gY + gH - ((v - gMin) / (gMax - gMin)) * gH;
    g.paneGrid(gy, [gMin, gMax], W);
    a.paneAxis(gy, [gMin, gMax], "Grid", "kW");
    g.path([...stepLine(past, "planned_grid_kw", x, gy), ...stepLine(future, "grid_kw", x, gy)], C.grid, { width: 1.6, dash: "5 3" });
    g.path(stepLine(past, "actual_grid_kw", x, gy), C.actual, { width: 2 });
    g.text(x(t0) + 4, gy(gMax) + 12, "▲ import", "hint");
    if (gMin < 0) g.text(x(t0) + 4, gy(gMin) - 4, "▼ export", "hint");

    // SoC pane: plan blue, actual red.
    const sY = gY + gH + 40, sH = 90;
    const sy = (v) => sY + sH - (v / 100) * sH;
    g.paneGrid(sy, [0, 100], W, [0, 25, 50, 75, 100]);
    a.paneAxis(sy, [0, 100], "SoC", "%", [0, 25, 50, 75, 100]);
    g.socBand(sy(d.max_soc), sy(d.min_soc), W);
    if (future.length) {
      const f0 = future[0];
      g.area(future.map((s) => [x(s.t), x(s.t1), sy(s.soc_end_pct)]), sy(0), C.soc, 0.1);
      g.path([x(f0.t), sy(f0.soc_start_pct), ...future.flatMap((s) => [x(s.t1), sy(s.soc_end_pct)])], C.soc, { width: 2.2 });
    }
    g.path(past.filter((r) => r.planned_soc != null).flatMap((r) => [x(r.t1), sy(r.planned_soc)]), C.soc, { width: 1.6, dash: "5 3" });
    g.path(past.filter((r) => r.actual_soc != null).flatMap((r) => [x(r.t1), sy(r.actual_soc)]), C.actual, { width: 2.2 });

    // Price pane: context for every decision.
    const pY = sY + sH + 40, pH = 80;
    const priceRows = [...past.filter((r) => r.import_price != null), ...future];
    const prices = priceRows.flatMap((r) => [r.import_price * 100, (r.export_price ?? r.import_price) * 100]);
    const [pMin, pMax] = niceRange(Math.min(0, ...prices), Math.max(1, ...prices));
    const py = (v) => pY + pH - ((v - pMin) / (pMax - pMin)) * pH;
    g.paneGrid(py, [pMin, pMax], W);
    a.paneAxis(py, [pMin, pMax], "Price", "ct/kWh");
    g.area(priceRows.map((r) => [x(r.t), x(r.t) + wOf(r), py(Math.max(0, r.import_price * 100))]), py(0), C.price, 0.22);
    for (const r of priceRows) if (r.import_price < 0) g.rect(x(r.t), py(0), wOf(r), py(r.import_price * 100) - py(0), C.neg, 0.45);
    g.path(priceRows.flatMap((r) => [x(r.t), py(r.import_price * 100), x(r.t) + wOf(r), py(r.import_price * 100)]), C.price, { width: 1.6 });

    // Forecast pane: PV and load (future only).
    const fY = pY + pH + 40, fH = 80;
    const [fMin, fMax] = niceRange(0, Math.max(0.5, ...future.flatMap((s) => [s.pv_kw, s.load_kw])));
    const fy = (v) => fY + fH - ((v - fMin) / (fMax - fMin)) * fH;
    g.paneGrid(fy, [fMin, fMax], W);
    a.paneAxis(fy, [fMin, fMax], "PV/load", "kW");
    g.area(future.map((s) => [x(s.t), x(s.t1), fy(s.pv_kw)]), fy(0), C.pv, 0.35);
    g.path(stepLine(future, "load_kw", x, fy), C.load, { width: 1.8 });

    const bottom = fY + fH + 18;
    g.timeAxis(x, t0, tEnd, bottom, fY + fH, pxPerHour);
    g.nowLine(nowX, 20, fY + fH);
    g.height = a.height = bottom + 4;
    this._hoverGeom = { x };

    const key = `<div class="key"><b>Bars / blue</b> = plan · <b class="act">red line</b> = what actually happened (left of now)</div>`;
    return this._chart(
      g,
      a,
      key +
        legendHtml([
          ...Object.values(KINDS).map((k) => [k.color, k.label]),
          [C.actual, "Actual", "line"],
          [C.grid, "Grid plan", "dash"],
          [C.soc, "SoC plan", "line"],
          [C.price, "Import price", "line"],
          [C.pv, "PV forecast"],
          [C.load, "Load forecast", "line"],
        ]),
    );
  }

  // ---------------------------------------------------------- summary view
  _summaryView(d) {
    this._hoverRows = null;
    const plan = d.plan;
    const now = Date.now();
    const windows = (action) => {
      const out = [];
      let cur = null;
      for (const s of plan?.slots ?? []) {
        const t = new Date(s.start).getTime();
        const t1 = t + s.hours * 3600e3;
        if (t1 < now) continue;
        if (s.action === action) {
          if (cur && Math.abs(cur.t1 - t) < 1000) cur.t1 = t1;
          else out.push((cur = { t, t1 }));
        } else cur = null;
      }
      return out.slice(0, 3).map((w) => `${fmtDay(new Date(w.t))} ${fmtTime(new Date(w.t))}–${fmtTime(new Date(w.t1))}`).join(", ") || "none planned";
    };
    const rows = [
      ["Mode", esc(d.mode)],
      ["Controller", `${esc(d.status)}${d.control_active ? " (in control)" : ""}`],
      ["Battery", d.soc != null ? `${d.soc.toFixed(0)}% of ${esc(d.capacity_kwh)} kWh (min ${esc(d.min_soc)}%, max ${esc(d.max_soc)}%)` : "–"],
      ["Grid charge windows", windows("charge_grid")],
      ["Grid discharge windows", windows("discharge_grid")],
      ["Hold windows", windows("hold")],
      ["Grid constraints", plan?.feasible === false ? "Unavoidable overload — inspect slot reasons" : "Within forecast limits"],
      ["Export tariff", plan?.export_prices_complete === false ? "Missing spot price — check Repairs" : "Known"],
      ["Planned cost (horizon)", eur(plan?.total_cost)],
      ["Without battery", eur(plan?.baseline_cost)],
      ["Planned savings", eur(plan?.savings)],
      ["Cost today (actual / planned)", `${eur(d.cost_today)} / ${eur(d.planned_cost_today)}`],
      ["Measured coverage today", `${((d.coverage_today ?? 0) * 100).toFixed(0)}%`],
      ["Observed cost today (partial)", eur(d.observed_cost_today)],
      ["Pending restores", Object.keys(d.pending_restores ?? {}).map(esc).join(", ") || "none"],
      ["Strategies", ["grid_charge", "battery_to_grid", "curtail_negative"].filter((k) => d.settings?.[k]).join(", ") || "none"],
      ["Wear cost / margin", `${esc(d.settings?.cycle_cost)} / ${esc(d.settings?.arbitrage_margin)} ct/kWh`],
    ];
    if (d.desired) {
      const v = (w) => (w < 0 && w > -1.5 ? "unlimited" : `${Math.round(w)} W`);
      rows.push(["ESS setpoint", `${Math.round(d.desired.setpoint)} W`]);
      rows.push(["Charge / discharge limit", `${v(d.desired.charge_limit)} / ${v(d.desired.discharge_limit)}`]);
      rows.push(["Feed-in limit", v(d.desired.feed_in_limit)]);
    }
    if (d.last_error) rows.push(["Last error", `<span class="errtxt">${esc(d.last_error)}</span>`]);
    if (plan) rows.push(["Plan made", new Date(plan.created).toLocaleString()]);
    return `<table class="sum">${rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</table>`;
  }

  // ------------------------------------------------------------------ hover
  _bindHover() {
    const svg = this.shadowRoot.querySelector(".scroll svg");
    const tip = this.shadowRoot.querySelector(".tip");
    if (!svg || !this._hoverRows || !this._hoverGeom) return;
    const cursor = svg.querySelector(".cursor");
    const hide = () => {
      tip.hidden = true;
      cursor?.setAttribute("visibility", "hidden");
    };
    const move = (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = ((ev.clientX - rect.left) / rect.width) * this._chartWidth;
      const row = this._hoverRows.find((r) => this._hoverGeom.x(r.t) <= px && px < this._hoverGeom.x(r.t1));
      if (!row) return hide();
      tip.innerHTML = row.html;
      tip.hidden = false;
      const card = this.shadowRoot.querySelector("ha-card").getBoundingClientRect();
      const left = ev.clientX - card.left + 14;
      tip.style.left = `${Math.max(4, Math.min(left, card.width - tip.offsetWidth - 8))}px`;
      tip.style.top = `${ev.clientY - card.top + 14}px`;
      if (cursor) {
        const cx = this._hoverGeom.x(row.t);
        cursor.setAttribute("x", cx);
        cursor.setAttribute("width", Math.max(1, this._hoverGeom.x(row.t1) - cx));
        cursor.setAttribute("visibility", "visible");
      }
    };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerleave", hide);
    this.shadowRoot.querySelector(".scroll")?.addEventListener("scroll", hide, { passive: true });
  }
}

class Svg {
  constructor(width) {
    this.w = width;
    this.height = 300;
    this.parts = [];
  }
  rect(x, y, w, h, fill, op = 1) {
    this.parts.push(`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" fill="${fill}" fill-opacity="${op}"/>`);
  }
  text(x, y, s, cls = "", anchor = "start") {
    this.parts.push(`<text x="${x.toFixed ? x.toFixed(1) : x}" y="${y.toFixed ? y.toFixed(1) : y}" class="${cls}" text-anchor="${anchor}">${esc(s)}</text>`);
  }
  path(coords, stroke, { width = 1.4, dash = "" } = {}) {
    if (coords.length < 4) return;
    let d = `M${coords[0].toFixed(1)},${coords[1].toFixed(1)}`;
    for (let i = 2; i < coords.length; i += 2) d += `L${coords[i].toFixed(1)},${coords[i + 1].toFixed(1)}`;
    this.parts.push(`<path d="${d}" fill="none" stroke="${stroke}" stroke-width="${width}" stroke-dasharray="${dash}" stroke-linejoin="round"/>`);
  }
  area(steps, y0, fill, op) {
    if (!steps.length) return;
    let d = `M${steps[0][0].toFixed(1)},${y0.toFixed(1)}`;
    for (const [x0, x1, y] of steps) d += `L${x0.toFixed(1)},${y.toFixed(1)}L${x1.toFixed(1)},${y.toFixed(1)}`;
    d += `L${steps[steps.length - 1][1].toFixed(1)},${y0.toFixed(1)}Z`;
    this.parts.push(`<path d="${d}" fill="${fill}" fill-opacity="${op}" stroke="none"/>`);
  }
  /** Coloured action band; consecutive equal segments merge and get a label when they fit. */
  band(segments, y, h, kinds) {
    const merged = [];
    for (const s of segments) {
      const last = merged[merged.length - 1];
      if (last && last.key === s.key && Math.abs(last.x1 - s.x0) < 1.5) last.x1 = s.x1;
      else merged.push({ ...s });
    }
    for (const m of merged) {
      const k = kinds[m.key];
      this.parts.push(`<rect x="${m.x0.toFixed(1)}" y="${y}" width="${Math.max(1, m.x1 - m.x0).toFixed(1)}" height="${h}" rx="3" fill="${k.color}"/>`);
      const label = k.label.length * 6.2 + 10 < m.x1 - m.x0 ? k.label : null;
      if (label) this.text((m.x0 + m.x1) / 2, y + h / 2 + 4, label, "bandlbl", "middle");
    }
  }
  paneGrid(yf, [min, max], x1, ticks) {
    for (const v of ticks ?? niceTicks(min, max, 4)) {
      const y = yf(v);
      this.parts.push(`<line x1="0" x2="${x1}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" class="grid${v === 0 ? " zero" : ""}"/>`);
    }
  }
  paneAxis(yf, [min, max], title, unit, ticks) {
    this.text(this.w - 6, yf(max) - 20, title, "pane", "end");
    this.text(this.w - 6, yf(max) - 9, unit, "lbl unit", "end");
    for (const v of ticks ?? niceTicks(min, max, 4)) this.text(this.w - 6, yf(v) + 4, `${+v.toFixed(2)}`, "lbl", "end");
  }
  socBand(yTop, yBottom, x1) {
    this.parts.push(`<rect x="0" y="${yTop.toFixed(1)}" width="${x1}" height="${Math.max(0, yBottom - yTop).toFixed(1)}" class="socband"/>`);
  }
  timeAxis(x, t0, t1, y, gridBottom, pxPerHour = 20) {
    const stepH = [1, 2, 3, 6, 12].find((h) => h * pxPerHour >= 46) ?? 12;
    const start = new Date(t0);
    start.setMinutes(0, 0, 0);
    for (let t = start.getTime(); t <= t1; t += 3600e3) {
      const d = new Date(t);
      if (t < t0) continue;
      const px = x(t);
      const midnight = d.getHours() === 0;
      if (d.getHours() % stepH && !midnight) {
        if (pxPerHour >= 12) this.parts.push(`<line x1="${px.toFixed(1)}" x2="${px.toFixed(1)}" y1="${gridBottom}" y2="${gridBottom + 3}" class="tick"/>`);
        continue;
      }
      this.parts.push(`<line x1="${px.toFixed(1)}" x2="${px.toFixed(1)}" y1="0" y2="${gridBottom}" class="vgrid${midnight ? " day" : ""}"/>`);
      this.text(px, y, midnight ? fmtDay(d) : `${String(d.getHours()).padStart(2, "0")}:00`, midnight ? "lbl day" : "lbl", "middle");
    }
  }
  nowLine(px, y0, y1) {
    this.parts.push(`<rect x="0" y="${y0}" width="${Math.max(0, px).toFixed(1)}" height="${y1 - y0}" class="past"/>`);
    this.parts.push(`<line x1="${px.toFixed(1)}" x2="${px.toFixed(1)}" y1="${y0}" y2="${y1}" class="now"/>`);
  }
  render(fixedWidth) {
    const w = fixedWidth ?? this.w;
    const cursor = fixedWidth ? "" : `<rect class="cursor" x="0" y="0" width="1" height="${this.height}" visibility="hidden"/>`;
    return `<svg viewBox="0 0 ${w} ${this.height}" width="${w}" height="${this.height}">${cursor}${this.parts.join("")}</svg>`;
  }
}

/** Step-line coordinates for rows with t/t1 and a numeric field. */
function stepLine(rows, key, x, yf) {
  return rows.filter((r) => r[key] != null).flatMap((r) => [x(r.t), yf(r[key]), x(r.t1), yf(r[key])]);
}

function niceTicks(min, max, n) {
  const span = max - min;
  if (span <= 0) return [min];
  const raw = span / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((f) => f * mag).find((s) => s >= raw) ?? raw;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(Math.abs(v) < 1e-9 ? 0 : v);
  return out;
}

const STYLE = `
  :host { display:block; }
  ha-card { padding: 12px 12px 8px; position: relative; overflow: hidden; }
  .head { display:flex; flex-direction:column; gap:6px; margin-bottom:6px; }
  .title { font-size: 1.15em; font-weight: 500; color: var(--primary-text-color); }
  .chips { display:flex; flex-wrap:wrap; gap:6px; }
  .chip { font-size: .8em; padding: 2px 8px; border-radius: 10px; color: #fff; background: var(--primary-color); }
  .chip.ghost { background: transparent; color: var(--primary-text-color); border: 1px solid var(--divider-color); }
  .chip.warn { background: var(--warning-color, #e0a800); }
  .reason { font-size: .85em; color: var(--secondary-text-color); }
  .tabs { display:flex; flex-wrap:wrap; align-items:center; gap:4px; border-bottom: 1px solid var(--divider-color); margin: 4px 0 8px; }
  .tabs .spacer { flex: 1; }
  .tab { background:none; border:none; padding:6px 10px; cursor:pointer; color: var(--secondary-text-color); font: inherit; font-size:.9em; border-bottom: 2px solid transparent; }
  .tab.on { color: var(--primary-color); border-bottom-color: var(--primary-color); }
  .ctl { display:inline-flex; gap:2px; padding-bottom: 4px; }
  .seg { background:none; border:1px solid var(--divider-color); color: var(--secondary-text-color); font: inherit; font-size:.78em;
         padding: 2px 8px; border-radius: 6px; cursor:pointer; min-width: 28px; }
  .seg.on { color: var(--text-primary-color, #fff); background: var(--primary-color); border-color: var(--primary-color); }
  .seg:disabled { opacity: .4; cursor: default; }
  .chart { display:flex; align-items:flex-start; }
  .yaxis { flex: 0 0 ${AXIS_W}px; }
  .scroll { flex: 1 1 auto; overflow-x: auto; overflow-y: hidden; scrollbar-width: thin; }
  .chart svg { display:block; touch-action: pan-x pan-y; }
  .chart .lbl { font-size: 11px; fill: var(--secondary-text-color); }
  .chart .lbl.unit { font-size: 10px; }
  .chart .lbl.day { font-weight: 600; fill: var(--primary-text-color); }
  .chart .pane { font-size: 10.5px; font-weight: 600; fill: var(--primary-text-color); }
  .chart .hint { font-size: 10px; fill: var(--secondary-text-color); opacity: .8; }
  .chart .era { font-size: 11px; font-weight: 600; fill: var(--primary-color); }
  .chart .bandlbl { font-size: 11px; font-weight: 600; fill: #fff; paint-order: stroke; stroke: rgba(0,0,0,.25); stroke-width: 2px; }
  .chart .grid { stroke: var(--divider-color); stroke-width: .7; }
  .chart .grid.zero { stroke: var(--secondary-text-color); stroke-width: 1; }
  .chart .vgrid { stroke: var(--divider-color); stroke-width: .6; }
  .chart .vgrid.day { stroke: var(--secondary-text-color); stroke-width: 1; }
  .chart .tick { stroke: var(--secondary-text-color); stroke-width: .6; }
  .chart .socband { fill: var(--ec-soc, #3b82f6); fill-opacity: .05; }
  .chart .past { fill: var(--secondary-text-color); fill-opacity: .07; }
  .chart .now { stroke: var(--primary-color); stroke-width: 2; stroke-dasharray: 4 3; }
  .chart .cursor { fill: var(--primary-text-color); fill-opacity: .1; }
  .key { flex-basis: 100%; font-size: 1.03em; color: var(--primary-text-color); }
  .key .act { color: var(--ec-actual, #d6455d); }
  .tip .muted, .tip .muted * { color: var(--secondary-text-color); font-weight: 400; }
  .tip table.cmp { border-collapse: collapse; margin: 2px 0; }
  .tip table.cmp th { font-weight: 500; color: var(--secondary-text-color); text-align: right; padding: 0 0 0 10px; }
  .tip table.cmp td { text-align: right; padding: 0 0 0 10px; }
  .tip table.cmp td:first-child { text-align: left; padding-left: 0; color: var(--secondary-text-color); }
  .legend { display:flex; flex-wrap:wrap; gap:4px 14px; font-size:.8em; color: var(--secondary-text-color); margin-top:6px; padding-left: ${AXIS_W}px; }
  .legend i, .tip i { display:inline-block; width:12px; height:10px; border-radius:2px; margin-right:5px; vertical-align:-1px; background: var(--c); }
  .legend i.dash, .legend i.line { height: 0; border-top: 2px dashed var(--c); background: none; vertical-align: 3px; }
  .legend i.line { border-top-style: solid; }
  .msg { padding: 24px 8px; color: var(--secondary-text-color); text-align:center; }
  .msg.err, .errtxt { color: var(--error-color, #d6455d); }
  .tip { position:absolute; z-index:2; pointer-events:none; background: var(--card-background-color, #fff); color: var(--primary-text-color);
         border: 1px solid var(--divider-color); border-radius: 6px; padding: 6px 8px; font-size: .8em; line-height: 1.45; box-shadow: 0 2px 8px rgba(0,0,0,.2); width: max-content; max-width: 270px; }
  .tip .r { color: var(--secondary-text-color); font-style: italic; }
  table.sum { width:100%; border-collapse: collapse; font-size:.9em; }
  table.sum th { text-align:left; font-weight:400; color: var(--secondary-text-color); padding:4px 8px 4px 0; vertical-align:top; width:42%; }
  table.sum td { padding:4px 0; color: var(--primary-text-color); }
  table.sum tr + tr { border-top: 1px solid var(--divider-color); }
`;

if (!customElements.get("energy-control-card")) {
  customElements.define("energy-control-card", EnergyControlCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "energy-control-card",
    name: "Energy Control",
    description: "Battery plan (prices, PV, load, SoC), planned vs actual and summary.",
    preview: false,
  });
  console.info(`%c ENERGY-CONTROL-CARD %c ${CARD_VERSION} `, "background:#2e9e5b;color:#fff", "");
}
