/*
 * Energy Control card — plan timeline, planned vs actual, summary.
 * Dependency-free web component; data via websocket `energy_control/subscribe`.
 */
const CARD_VERSION = "0.3.0";

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
const AXIS_R = 34; // fixed right axis (SoC %)
const RANGES = [12, 24, 36, 48];
const ZOOMS = [0, 24, 48, 96]; // px per hour; 0 = fit to card

const fmtTime = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtDay = (d) => d.toLocaleDateString([], { weekday: "short" });
const ct = (eur) => (eur == null ? "–" : `${(eur * 100).toFixed(1)} ct`);
const eur = (v) => (v == null ? "–" : `€ ${v.toFixed(2)}`);
const kw = (v) => (v == null ? "–" : `${v.toFixed(2)} kW`);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const TABS = ["timeline", "schedule", "history", "summary"];
const MINUS = "\u2212";
/** Number with a real minus sign; `+` only when asked. */
const numS = (v, d = 1, plus = false) => {
  if (v == null) return "–";
  const r = Number(v.toFixed(d));
  return `${r < 0 ? MINUS : plus && r > 0 ? "+" : ""}${Math.abs(r).toFixed(d)}`;
};
const kwS = (v) => (v == null ? "–" : `${numS(v, 2)} kW`);
const cents = (v) => Math.round(v * 100);
const eurS = (v) => (v == null ? "–" : `${cents(v) < 0 ? MINUS : ""}€ ${(Math.abs(cents(v)) / 100).toFixed(2)}`);
const signedEur = (v) => (v == null ? "–" : `${cents(v) > 0 ? "+" : cents(v) < 0 ? MINUS : ""}€ ${(Math.abs(cents(v)) / 100).toFixed(2)}`);
/** Cost difference colour: more expensive than planned is red. */
const deltaCls = (v) => (v == null || cents(v) === 0 ? "muted" : v > 0 ? "neg" : "pos");
const kindChip = (k, text) => `<span class="kc" style="--c:${KINDS[k].color}"><i></i>${esc(text ?? KINDS[k].label)}</span>`;
function dayLabel(t) {
  const d = new Date(t);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((new Date(d).setHours(0, 0, 0, 0) - today.getTime()) / 86400e3);
  const name = { 0: "Today", 1: "Tomorrow", [-1]: "Yesterday" }[diff];
  const date = d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  return name ? `${name} · ${date}` : date;
}
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
    // Older configs: the "plan" tab is now part of the timeline.
    if (this._config.tab) this._tab = TABS.includes(this._config.tab) ? this._config.tab : "timeline";
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
    else if (this._tab === "schedule") body = this._scheduleView(d);
    else if (this._tab === "history") body = this._historyView(d);
    else body = this._summaryView(d);

    const tabs = [
      ["timeline", "Timeline"],
      ["schedule", "Schedule"],
      ["history", "History"],
      ["summary", "Summary"],
    ]
      .map(([k, l]) => `<button class="tab ${this._tab === k ? "on" : ""}" data-tab="${k}">${l}</button>`)
      .join("");
    const controls = this._tab === "timeline" ? this._controls() : "";

    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="head">
          <div class="title">${esc(title)}</div>
          ${d ? this._headline(d) : ""}
        </div>
        <div class="tabs">${tabs}<span class="spacer"></span>${controls}</div>
        <div class="body">${body}</div>
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
    const slot = this._currentSlot(d);
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
    const avail = Math.max(120, this._width - AXIS_W - AXIS_R);
    const hours = (tEnd - t0) / 3600e3;
    const W = Math.max(avail, this._hourWidth ? Math.round(hours * this._hourWidth) : 0);
    const pad = 6;
    const x = (t) => pad + ((t - t0) / (tEnd - t0 || 1)) * (W - 2 * pad);
    return { W, x, scrolls: W > avail + 1, pxPerHour: (W - 2 * pad) / (hours || 1) };
  }

  _chart(main, axis, right, legend) {
    this._chartWidth = main.w;
    // Fixed height so the chart does not jump while hovering; narrow cards wrap to more lines.
    return `<div class="readout" style="min-height:${this._width >= 640 ? 3.2 : this._width >= 460 ? 5 : 7}em">${this._readoutDefault ?? ""}</div>
      <div class="chart">
        <div class="yaxis">${axis.render(AXIS_W)}</div>
        <div class="scroll">${main.render()}</div>
        <div class="yaxis r">${right.render(AXIS_R)}</div>
      </div>
      <div class="legend">${legend}</div>`;
  }

  // --------------------------------------------------------- timeline view
  /**
   * One time axis: the past (left of "now") shows what was planned for each
   * slot next to what actually happened; the future shows the plan.
   * Panes: price bars coloured by action, battery power with SoC (right
   * axis), grid power, PV/load forecast. Red = actual.
   */
  _timelineView(d) {
    const now = Date.now();
    const SLOT = 15 * 60e3;
    const t0 = now - this._pastHours * 3600e3;
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
    const g = new Svg(W);
    const a = new Svg(AXIS_W);
    const ar = new Svg(AXIS_R);
    const nowX = x(now);
    this._nowX = nowX;
    this._hoverRows = [...past.map((r) => this._pastRow(r)), ...future.map((s) => this._planRow(s))];
    this._readoutDefault = this._hoverRows.find((r) => r.t <= now && now < r.t1)?.html ?? `<span class="muted">Hover the chart for details</span>`;

    if (nowX > 150) g.text(nowX - 6, 13, "◀ past: plan vs actual", "era", "end");
    g.text(nowX + 6, 13, "plan ▶", "era");

    // Price pane: bar height = import price, colour = what the battery does.
    const pY = 44, pH = 110;
    const priceRows = [
      ...past.filter((r) => r.import_price != null).map((r) => ({ t: r.t, t1: r.t1, p: r.import_price * 100, kind: r.kind ?? "idle", past: true })),
      ...future.map((s) => ({ t: s.t, t1: s.t1, p: s.import_price * 100, kind: s.kind, past: false })),
    ];
    // Thin 15-minute bars read as stripes: below ~7 px per slot, show hourly bars.
    const bars = pxPerHour / 4 >= 7 ? priceRows : hourly(priceRows);
    const ps = bars.map((r) => r.p);
    const [pMin, pMax] = niceRange(Math.min(0, ...ps), Math.max(1, ...ps), 0.12);
    const py = (v) => pY + pH - ((v - pMin) / (pMax - pMin)) * pH;
    g.paneGrid(py, [pMin, pMax], W);
    a.paneAxis(py, [pMin, pMax], "Price", "ct/kWh");
    for (const r of bars) {
      const w = x(r.t1) - x(r.t);
      const gap = w > 4 ? 1 : 0;
      g.rect(x(r.t) + gap / 2, Math.min(py(r.p), py(0)), Math.max(0.5, w - gap), Math.abs(py(r.p) - py(0)), KINDS[r.kind].color, r.past ? 0.4 : 0.9);
    }
    const ahead = bars.filter((r) => !r.past).sort((u, v) => u.p - v.p);
    if (ahead.length > 6) {
      const mark = (r, cls, sym) => g.text((x(r.t) + x(r.t1)) / 2, Math.min(py(r.p), py(0)) - 3, sym, `mark ${cls}`, "middle");
      ahead.slice(0, 3).forEach((r) => mark(r, "lo", "▼"));
      ahead.slice(-3).forEach((r) => mark(r, "hi", "▲"));
    }

    // Battery pane: planned power as solid blocks (faded in the past), actual
    // as a red line; SoC as a blue line on the right axis.
    const bY = pY + pH + 44, bH = 170;
    const bVals = [...past.flatMap((r) => [r.planned_battery_kw, r.actual_battery_kw]), ...future.map((s) => s.battery_kw)].filter((v) => v != null);
    const [bMin, bMax] = niceRange(Math.min(-0.5, ...bVals), Math.max(0.5, ...bVals));
    const by = (v) => bY + bH - ((v - bMin) / (bMax - bMin)) * bH;
    const sy = (v) => bY + bH - (v / 100) * bH;
    g.socBand(sy(d.max_soc), sy(d.min_soc), W);
    g.paneGrid(by, [bMin, bMax], W);
    a.paneAxis(by, [bMin, bMax], "Battery", "kW");
    ar.paneAxisRight(sy, "SoC", "%", [0, 25, 50, 75, 100]);
    const blockKey = (r, v) => (v > 0 ? (r.kind === "charge_grid" ? "charge_grid" : "store") : r.kind === "discharge_grid" ? "discharge_grid" : "cover");
    g.blocks(past, "planned_battery_kw", blockKey, x, by, 0.3);
    g.blocks(future, "battery_kw", blockKey, x, by, 0.85);
    g.path(stepLine(past, "actual_battery_kw", x, by), C.actual, { width: 2 });
    g.path(past.filter((r) => r.planned_soc != null).flatMap((r) => [x(r.t1), sy(r.planned_soc)]), C.soc, { width: 1.4, dash: "4 3" });
    const socPast = past.filter((r) => r.actual_soc != null).flatMap((r) => [x(r.t1), sy(r.actual_soc)]);
    const f0 = future[0];
    const socPlan = f0 ? [x(f0.t), sy(f0.soc_start_pct), ...future.flatMap((s) => [x(s.t1), sy(s.soc_end_pct)])] : [];
    g.path(socPast, C.soc, { width: 2.4 });
    g.path(socPlan, C.soc, { width: 2.4 });
    g.text(x(t0) + 4, by(bMax) + 12, "▲ charge", "hint");
    g.text(x(t0) + 4, by(bMin) - 4, "▼ discharge", "hint");

    // Grid pane: plan dashed, actual red.
    const gY = bY + bH + 44, gH = 100;
    const gVals = [...past.flatMap((r) => [r.planned_grid_kw, r.actual_grid_kw]), ...future.map((s) => s.grid_kw)].filter((v) => v != null);
    const [gMin, gMax] = niceRange(Math.min(0, ...gVals), Math.max(0.5, ...gVals));
    const gy = (v) => gY + gH - ((v - gMin) / (gMax - gMin)) * gH;
    g.paneGrid(gy, [gMin, gMax], W);
    a.paneAxis(gy, [gMin, gMax], "Grid", "kW");
    g.path([...stepLine(past, "planned_grid_kw", x, gy), ...stepLine(future, "grid_kw", x, gy)], C.grid, { width: 1.6, dash: "5 3" });
    g.path(stepLine(past, "actual_grid_kw", x, gy), C.actual, { width: 2 });
    g.text(x(t0) + 4, gy(gMax) + 12, "▲ import", "hint");
    if (gMin < 0) g.text(x(t0) + 4, gy(gMin) - 4, "▼ export", "hint");

    // Forecast pane: PV and load (future only).
    const fY = gY + gH + 44, fH = 80;
    const [fMin, fMax] = niceRange(0, Math.max(0.5, ...future.flatMap((s) => [s.pv_kw, s.load_kw])));
    const fy = (v) => fY + fH - ((v - fMin) / (fMax - fMin)) * fH;
    g.paneGrid(fy, [fMin, fMax], W);
    a.paneAxis(fy, [fMin, fMax], "PV/load", "kW");
    g.area(future.map((s) => [x(s.t), x(s.t1), fy(s.pv_kw)]), fy(0), C.pv, 0.35);
    g.path(stepLine(future, "load_kw", x, fy), C.load, { width: 1.8 });

    const bottom = fY + fH + 18;
    g.timeAxis(x, t0, tEnd, bottom, fY + fH, pxPerHour);
    g.nowLine(nowX, 20, fY + fH);
    g.height = a.height = ar.height = bottom + 4;
    this._hoverGeom = { x };

    const key = `<div class="key">Bar height = price, colour = plan · <b class="act">red line</b> = what actually happened · <b class="soc">blue line</b> = SoC (right axis)</div>`;
    return this._chart(
      g,
      a,
      ar,
      key +
        legendHtml([
          ...Object.values(KINDS).map((k) => [k.color, k.label]),
          [C.actual, "Actual", "line"],
          [C.soc, "SoC (dashed = planned)", "line"],
          [C.grid, "Grid plan", "dash"],
          [C.pv, "PV forecast"],
          [C.load, "Load forecast", "line"],
          ["var(--success-color, #2e9e5b)", "▼ cheapest / ▲ dearest", "none"],
        ]),
    );
  }

  /** Readout for a past slot: plan next to actual. */
  _pastRow(r) {
    const pair = (label, a, p) => `<span>${label} <b>${a}</b> <span class="muted">plan ${p}</span></span>`;
    const pct = (v) => (v == null ? "–" : `${v.toFixed(0)}%`);
    return {
      t: r.t,
      t1: r.t1,
      html: `<b>${fmtDay(new Date(r.t))} ${fmtTime(new Date(r.t))}</b> <span class="muted">past</span>
        ${r.kind ? kindChip(r.kind) : ""}
        ${pair("Battery", kwS(r.actual_battery_kw), kwS(r.planned_battery_kw))}
        ${pair("Grid", kwS(r.actual_grid_kw), kwS(r.planned_grid_kw))}
        ${pair("SoC", pct(r.actual_soc), pct(r.planned_soc))}
        ${pair("Cost (observed)", eurS(r.actual_cost), eurS(r.planned_cost))}
        <span>Measured <b>${r.coverage == null ? "unknown" : `${(r.coverage * 100).toFixed(0)}%`}</b></span>
        ${r.extrapolated_cost != null && r.coverage < 0.98 ? `<span>Full-slot estimate <b>${eurS(r.extrapolated_cost)}</b></span>` : ""}
        <span>Load error <b>${kwS(r.load_error_kw)}</b></span><span>PV error <b>${kwS(r.pv_error_kw)}</b></span>
        <span>Price <b>${ct(r.import_price)}</b></span>${r.curtailed ? `<span>PV curtailed</span>` : ""}`,
    };
  }

  /** Readout for a planned slot. */
  _planRow(s) {
    return {
      t: s.t,
      t1: s.t1,
      html: `<b>${fmtDay(new Date(s.t))} ${fmtTime(new Date(s.t))}</b> <span class="muted">plan</span>
        ${kindChip(s.kind)}
        <span>Battery <b>${kwS(s.battery_kw)}</b></span><span>Grid <b>${kwS(s.grid_kw)}</b></span>
        <span>PV <b>${kwS(s.pv_kw)}</b></span><span>Load <b>${kwS(s.load_kw)}</b></span>
        <span>SoC <b>→ ${s.soc_end_pct.toFixed(0)}%</b></span>
        <span>Price <b>${ct(s.import_price)}</b> <span class="muted">export ${s.export_price_known === false ? "unknown" : ct(s.export_price)}</span></span>
        ${s.curtail_kw > 0 ? `<span>Curtail <b>${kwS(s.curtail_kw)}</b></span>` : ""}
        ${s.reason ? `<div class="r">${esc(s.reason)}</div>` : ""}`,
    };
  }

  /** The plan slot running now, if any. */
  _currentSlot(d) {
    const now = Date.now();
    return d.plan?.slots?.find((s) => {
      const t = new Date(s.start).getTime();
      return t <= now && now < t + s.hours * 3600e3;
    });
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
      return out.slice(0, 3).map((w) => `${fmtDay(new Date(w.t))} ${fmtTime(new Date(w.t))}–${fmtTime(new Date(w.t1))}`).join("<br>") || `<span class="muted">none planned</span>`;
    };

    // Tiles: the three numbers looked at most.
    const pct = (v) => Math.max(0, Math.min(100, Number(v) || 0));
    const todayDiff = d.cost_today != null && d.planned_cost_today != null ? d.cost_today - d.planned_cost_today : null;
    const todayCost = d.cost_today ?? d.observed_cost_today;
    const coverage = d.coverage_today != null ? `${(d.coverage_today * 100).toFixed(0)}% measured` : "partly measured";
    const todaySub = d.cost_today == null && d.observed_cost_today != null ? `partial · ${coverage}` : todayDiff == null ? "–" : `${signedEur(todayDiff)} vs plan`;
    const tiles = `<div class="tiles">
      <div class="tile"><div class="k">Battery</div>
        <div class="v">${d.soc != null ? `${d.soc.toFixed(0)}%` : "–"}</div>
        <div class="socbar"><b style="width:${pct(d.soc)}%"></b><u style="left:${pct(d.min_soc)}%"></u><u style="left:${pct(d.max_soc)}%"></u></div>
        <div class="s">${esc(d.capacity_kwh)} kWh · ${esc(d.min_soc)}–${esc(d.max_soc)}%</div></div>
      <div class="tile"><div class="k">Planned savings</div>
        <div class="v ${plan?.savings > 0 ? "pos" : ""}">${eurS(plan?.savings)}</div>
        <div class="s">vs ${eurS(plan?.baseline_cost)} without battery</div></div>
      <div class="tile"><div class="k">Cost today</div>
        <div class="v">${eurS(todayCost)}</div>
        <div class="s ${d.cost_today == null ? "" : deltaCls(todayDiff)}">${todaySub}</div></div>
    </div>`;

    const slot = this._currentSlot(d);
    const kind = slot ? kindOf(d.desired?.action ?? slot.action, slot.battery_kw) : null;
    const groups = [
      [
        "Now",
        [
          ["Doing", kind ? `${kindChip(kind)}${slot.battery_kw ? ` <span class="muted">· ${kwS(slot.battery_kw)}</span>` : ""}` : "–"],
          ["Mode", esc(d.mode)],
          ["Controller", `${esc(d.status)}${d.control_active ? ` <span class="pill">in control</span>` : ""}`],
          d.override ? ["Override", `${esc(d.override.action)} until ${fmtTime(new Date(d.override.until))}`] : null,
        ],
      ],
      d.desired
        ? [
            "Inverter",
            (() => {
              const v = (w) => (w < 0 && w > -1.5 ? "unlimited" : `${Math.round(w)} W`);
              return [
                ["ESS setpoint", `${numS(d.desired.setpoint, 0)} W`],
                ["Charge / discharge limit", `${v(d.desired.charge_limit)} / ${v(d.desired.discharge_limit)}`],
                ["Feed-in limit", v(d.desired.feed_in_limit)],
              ];
            })(),
          ]
        : null,
      [
        "Plan ahead",
        [
          ["Charge from grid", windows("charge_grid")],
          ["Discharge to grid", windows("discharge_grid")],
          ["Hold", windows("hold")],
          ["Planned cost", `<span class="n">${eurS(plan?.total_cost)}</span>`],
          ["Without battery", `<span class="n">${eurS(plan?.baseline_cost)}</span>`],
          ["Today actual / planned", `<span class="n">${eurS(d.cost_today)} / ${eurS(d.planned_cost_today)}</span>`],
          ["Observed cost today (partial)", `<span class="n">${eurS(d.observed_cost_today)}</span>`],
          plan ? ["Plan made", new Date(plan.created).toLocaleString()] : null,
        ],
      ],
      [
        "Checks",
        [
          ["Grid constraints", plan?.feasible === false ? `<span class="errtxt">Unavoidable overload — inspect slot reasons</span>` : "Within forecast limits"],
          ["Export tariff", plan?.export_prices_complete === false ? `<span class="errtxt">Missing spot price — check Repairs</span>` : "Known"],
          ["Measured coverage today", `${((d.coverage_today ?? 0) * 100).toFixed(0)}%`],
          ["Pending restores", Object.keys(d.pending_restores ?? {}).map(esc).join(", ") || "none"],
        ],
      ],
      [
        "Settings",
        [
          ["Strategies", ["grid_charge", "battery_to_grid", "curtail_negative"].filter((k) => d.settings?.[k]).map((k) => k.replace(/_/g, " ")).join(", ") || "none"],
          ["Wear cost / margin", `${esc(d.settings?.cycle_cost)} / ${esc(d.settings?.arbitrage_margin)} ct/kWh`],
        ],
      ],
      d.last_error ? ["Problem", [["Last error", `<span class="errtxt">${esc(d.last_error)}</span>`]]] : null,
    ].filter(Boolean);
    const body = groups
      .map(([h, rows]) => `<div class="grp"><h3>${h}</h3><table class="kv">${rows.filter(Boolean).map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</table></div>`)
      .join("");
    return tiles + body;
  }

  // --------------------------------------------------------- schedule view
  /** The plan as blocks: consecutive slots doing the same thing merge into one row. */
  _scheduleView(d) {
    this._hoverRows = null;
    const now = Date.now();
    const blocks = [];
    for (const s of d.plan?.slots ?? []) {
      const t = new Date(s.start).getTime();
      const t1 = t + s.hours * 3600e3;
      if (t1 <= now) continue;
      const kind = kindOf(s.action, s.battery_kw);
      let b = blocks[blocks.length - 1];
      if (!b || b.kind !== kind || Math.abs(b.t1 - t) > 1000) {
        b = { kind, t, t1, h: 0, kwh: 0, priceH: 0, socStart: s.soc_start_pct, socEnd: s.soc_end_pct, reason: s.reason };
        blocks.push(b);
      }
      b.t1 = t1;
      b.h += s.hours;
      b.kwh += (s.battery_kw ?? 0) * s.hours;
      b.priceH += (s.import_price ?? 0) * s.hours;
      b.socEnd = s.soc_end_pct;
    }
    if (!blocks.length) return `<div class="msg">No plan${d.last_error ? `: ${esc(d.last_error)}` : ""}</div>`;

    const narrow = this._width < 520;
    let day = null;
    const rows = blocks
      .map((b, i) => {
        const dk = new Date(b.t).toDateString();
        const head = dk !== day ? `<tr class="day"><td colspan="5">${dayLabel(b.t)}</td></tr>` : "";
        day = dk;
        const s0 = Math.round(b.socStart), s1 = Math.round(b.socEnd);
        const soc = s0 === s1 ? `${s1}%` : narrow ? `${s1}%<div class="sub">from ${s0}%</div>` : `${s0} → ${s1}%`;
        return `${head}<tr class="${i === 0 ? "now" : ""}" ${b.reason ? `title="${esc(b.reason)}"` : ""}>
          <td class="n l">${narrow ? `${fmtTime(new Date(b.t))}<div class="sub">to ${fmtTime(new Date(b.t1))}</div>` : `${fmtTime(new Date(b.t))}–${fmtTime(new Date(b.t1))}`}</td>
          <td>${kindChip(b.kind)}</td>
          <td class="n">${numS(b.kwh / b.h, 1, true)}<span class="u">kW</span></td>
          <td class="n">${(b.priceH / b.h * 100).toFixed(1)}<span class="u">ct</span></td>
          <td class="n">${soc}</td></tr>`;
      })
      .join("");
    const sav = d.plan?.savings;
    return `<div class="tscroll"><table class="dt ${narrow ? "tight" : ""}">
      <thead><tr><th>Time</th><th>Action</th><th class="n">Battery</th><th class="n">Price</th><th class="n">SoC</th></tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr><td colspan="3">Planned cost</td><td class="n" colspan="2">${eurS(d.plan?.total_cost)}${sav != null ? ` <span class="${sav > 0 ? "pos" : "neg"}">(${signedEur(-sav)})</span>` : ""}</td></tr></tfoot>
    </table></div>
    <div class="note">Battery = average over the block · Price = average import price · Hover a row for the reason.</div>`;
  }

  // ---------------------------------------------------------- history view
  /** Past hours: plan next to actual. Newest first; narrow cards get a compact layout. */
  _historyView(d) {
    this._hoverRows = null;
    const byHour = new Map();
    for (const r of d.history ?? []) {
      const t = new Date(r.start);
      t.setMinutes(0, 0, 0);
      const k = t.getTime();
      if (!byHour.has(k)) byHour.set(k, []);
      byHour.get(k).push(r);
    }
    if (!byHour.size) return `<div class="msg">No history yet. It fills in as time passes.</div>`;
    const avg = (rows, key) => {
      const v = rows.map((r) => r[key]).filter((x) => x != null);
      return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
    };
    const last = (rows, key) => [...rows].reverse().find((r) => r[key] != null)?.[key] ?? null;
    const sum = (rows, key) => {
      const v = rows.map((r) => r[key]).filter((x) => x != null);
      return v.length ? v.reduce((a, b) => a + b, 0) : null;
    };
    const hours = [...byHour.entries()]
      .sort((a, b) => b[0] - a[0])
      .map(([t, rows]) => {
        const counts = {};
        for (const r of rows) if (r.planned_action) {
          const k = kindOf(r.planned_action, r.planned_battery_kw);
          counts[k] = (counts[k] ?? 0) + 1;
        }
        const kind = Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
        const pc = sum(rows, "planned_cost"), ac = sum(rows, "actual_cost");
        const cov = avg(rows, "coverage");
        return {
          t, kind,
          pb: avg(rows, "planned_battery_kw"), ab: avg(rows, "actual_battery_kw"),
          pg: avg(rows, "planned_grid_kw"), ag: avg(rows, "actual_grid_kw"),
          ps: last(rows, "planned_soc"), as: last(rows, "actual_soc"),
          pc, ac, dc: pc != null && ac != null ? ac - pc : null, partial: cov != null && cov < 0.98,
        };
      });
    const tot = (k) => hours.reduce((a, h) => a + (h.dc != null ? h[k] : 0), 0);
    const [tp, ta] = [tot("pc"), tot("ac")];
    const pctS = (v) => (v == null ? "–" : `${v.toFixed(0)}%`);
    const acS = (h) => (h.partial ? `<span title="Only partly measured">${eurS(h.ac)}*</span>` : eurS(h.ac));
    const dcell = (v) => `<td class="n ${deltaCls(v)}">${v == null ? "–" : signedEur(v)}</td>`;
    const wide = this._width >= 640;
    let day = null;
    const cols = wide ? 11 : 4;
    const rows = hours
      .map((h) => {
        const dk = new Date(h.t).toDateString();
        const head = dk !== day ? `<tr class="day"><td colspan="${cols}">${dayLabel(h.t)}</td></tr>` : "";
        day = dk;
        const label = h.kind ? kindChip(h.kind, fmtTime(new Date(h.t))) : fmtTime(new Date(h.t));
        if (!wide)
          return `${head}<tr title="${h.kind ? esc(KINDS[h.kind].label) : ""}">
            <td>${label}</td>
            <td class="n">${numS(h.ab)} kW<div class="sub">plan ${numS(h.pb)}</div></td>
            <td class="n">${pctS(h.as)}<div class="sub">plan ${pctS(h.ps)}</div></td>
            <td class="n">${acS(h)}<div class="sub ${deltaCls(h.dc)}">${h.dc == null ? "–" : signedEur(h.dc)}</div></td></tr>`;
        return `${head}<tr>
          <td class="n l">${fmtTime(new Date(h.t))}</td><td>${h.kind ? kindChip(h.kind) : "–"}</td>
          <td class="n p">${numS(h.pb)}</td><td class="n">${numS(h.ab)}</td>
          <td class="n p">${numS(h.pg)}</td><td class="n">${numS(h.ag)}</td>
          <td class="n p">${pctS(h.ps)}</td><td class="n">${pctS(h.as)}</td>
          <td class="n p">${eurS(h.pc)}</td><td class="n">${acS(h)}</td>${dcell(h.dc)}</tr>`;
      })
      .join("");
    const head = wide
      ? `<tr class="grph"><th colspan="2"></th><th colspan="2" class="c">Battery kW</th><th colspan="2" class="c">Grid kW</th><th colspan="2" class="c">SoC</th><th colspan="3" class="c">Cost</th></tr>
         <tr><th>Hour</th><th>Planned</th><th class="n">plan</th><th class="n">actual</th><th class="n">plan</th><th class="n">actual</th><th class="n">plan</th><th class="n">actual</th><th class="n">plan</th><th class="n">actual</th><th class="n">Δ</th></tr>`
      : `<tr><th>Hour</th><th class="n">Battery</th><th class="n">SoC</th><th class="n">Cost</th></tr>`;
    const foot = wide
      ? `<tr><td colspan="8">Total</td><td class="n">${eurS(tp)}</td><td class="n">${eurS(ta)}</td>${dcell(ta - tp)}</tr>`
      : `<tr><td colspan="3">Total (plan ${eurS(tp)})</td><td class="n">${eurS(ta)}<div class="sub ${deltaCls(ta - tp)}">${signedEur(ta - tp)}</div></td></tr>`;
    return `<div class="tscroll"><table class="dt ${wide ? "" : "tight"}">
      <thead>${head}</thead><tbody>${rows}</tbody><tfoot>${foot}</tfoot></table></div>
      <div class="note">kW = hourly average · SoC = end of hour · Δ: <span class="pos">green</span> cheaper than planned, <span class="neg">red</span> more expensive${hours.some((h) => h.partial) ? " · * only partly measured (observed cost)" : ""}.</div>`;
  }

  // ------------------------------------------------------------------ hover
  _bindHover() {
    const svg = this.shadowRoot.querySelector(".scroll svg");
    const out = this.shadowRoot.querySelector(".readout");
    if (!svg || !out || !this._hoverRows || !this._hoverGeom) return;
    const cursor = svg.querySelector(".cursor");
    let shown = null;
    const reset = () => {
      shown = null;
      out.innerHTML = this._readoutDefault ?? "";
      out.classList.remove("live");
      cursor?.setAttribute("visibility", "hidden");
    };
    const move = (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = ((ev.clientX - rect.left) / rect.width) * this._chartWidth;
      const row = this._hoverRows.find((r) => this._hoverGeom.x(r.t) <= px && px < this._hoverGeom.x(r.t1));
      if (!row) return reset();
      if (row === shown) return;
      shown = row;
      out.innerHTML = row.html;
      out.classList.add("live");
      if (cursor) {
        const cx = this._hoverGeom.x(row.t);
        cursor.setAttribute("x", cx);
        cursor.setAttribute("width", Math.max(1, this._hoverGeom.x(row.t1) - cx));
        cursor.setAttribute("visibility", "visible");
      }
    };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerdown", move);
    svg.addEventListener("pointerleave", reset);
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
  /** Solid step blocks: touching slots with the same colour merge into one shape. */
  blocks(rows, key, colorKey, x, yf, op) {
    let run = null;
    const flush = () => {
      if (!run) return;
      const y0 = yf(0).toFixed(1);
      let d = `M${x(run.rows[0].t).toFixed(1)},${y0}`;
      for (const r of run.rows) d += `L${x(r.t).toFixed(1)},${yf(r[key]).toFixed(1)}L${x(r.t1).toFixed(1)},${yf(r[key]).toFixed(1)}`;
      d += `L${x(run.rows[run.rows.length - 1].t1).toFixed(1)},${y0}Z`;
      this.parts.push(`<path d="${d}" fill="${KINDS[run.k].color}" fill-opacity="${op}"/>`);
      run = null;
    };
    for (const r of rows) {
      const v = r[key];
      if (v == null || Math.abs(v) < 1e-3) {
        flush();
        continue;
      }
      const k = colorKey(r, v);
      const prev = run?.rows[run.rows.length - 1];
      if (!run || run.k !== k || Math.abs(prev.t1 - r.t) > 1000) {
        flush();
        run = { k, rows: [] };
      }
      run.rows.push(r);
    }
    flush();
  }
  paneAxisRight(yf, title, unit, ticks) {
    this.text(6, yf(100) - 20, title, "pane soc");
    this.text(6, yf(100) - 9, unit, "lbl unit soc");
    for (const v of ticks) this.text(6, yf(v) + 4, `${v}`, "lbl soc");
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

/** Merge 15-minute price rows into hourly bars (average price, most common action). */
function hourly(rows) {
  const out = [];
  for (const r of rows) {
    const h = Math.floor(r.t / 3600e3);
    let b = out[out.length - 1];
    if (!b || b.h !== h || b.past !== r.past) out.push((b = { h, t: r.t, t1: r.t1, past: r.past, sum: 0, n: 0, kinds: {} }));
    b.t1 = r.t1;
    b.sum += r.p;
    b.n += 1;
    b.kinds[r.kind] = (b.kinds[r.kind] ?? 0) + 1;
  }
  return out.map((b) => ({ t: b.t, t1: b.t1, past: b.past, p: b.sum / b.n, kind: Object.entries(b.kinds).sort((u, v) => v[1] - u[1])[0][0] }));
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
  .yaxis.r { flex-basis: ${AXIS_R}px; }
  .scroll { flex: 1 1 auto; overflow-x: auto; overflow-y: hidden; scrollbar-width: thin; }
  .chart svg { display:block; touch-action: pan-x pan-y; }
  .chart .lbl { font-size: 11px; fill: var(--secondary-text-color); }
  .chart .lbl.unit { font-size: 10px; }
  .chart .lbl.day { font-weight: 600; fill: var(--primary-text-color); }
  .chart .pane { font-size: 10.5px; font-weight: 600; fill: var(--primary-text-color); }
  .chart .hint { font-size: 10px; fill: var(--secondary-text-color); opacity: .8; }
  .chart .era { font-size: 11px; font-weight: 600; fill: var(--primary-color); }
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
  .key .soc { color: var(--ec-soc, #3b82f6); }
  .chart .soc { fill: var(--ec-soc, #3b82f6); }
  .chart .mark { font-size: 10px; font-weight: 700; }
  .chart .mark.lo { fill: var(--success-color, #2e9e5b); }
  .chart .mark.hi { fill: var(--error-color, #d6455d); }
  .legend i.none { display: none; }
  .readout { display:flex; flex-wrap:wrap; align-items:center; align-content:flex-start; gap:2px 14px; font-size:.82em; line-height:1.5;
             padding: 2px 0 6px ${AXIS_W}px; color: var(--primary-text-color); font-variant-numeric: tabular-nums; }
  .readout b { font-weight: 500; }
  .readout .r { flex-basis: 100%; color: var(--secondary-text-color); font-style: italic; }
  .legend { display:flex; flex-wrap:wrap; gap:4px 14px; font-size:.8em; color: var(--secondary-text-color); margin-top:6px; padding-left: ${AXIS_W}px; }
  .legend i { display:inline-block; width:12px; height:10px; border-radius:2px; margin-right:5px; vertical-align:-1px; background: var(--c); }
  .legend i.dash, .legend i.line { height: 0; border-top: 2px dashed var(--c); background: none; vertical-align: 3px; }
  .legend i.line { border-top-style: solid; }
  .msg { padding: 24px 8px; color: var(--secondary-text-color); text-align:center; }
  .msg.err, .errtxt { color: var(--error-color, #d6455d); }
         border: 1px solid var(--divider-color); border-radius: 6px; padding: 6px 8px; font-size: .8em; line-height: 1.45; box-shadow: 0 2px 8px rgba(0,0,0,.2); width: max-content; max-width: 270px; }
  .muted { color: var(--secondary-text-color); }
  .pos { color: var(--success-color, #2e9e5b); }
  .neg { color: var(--error-color, #d6455d); }
  .note { font-size: .75em; color: var(--secondary-text-color); margin-top: 8px; }
  .kc { display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }
  .kc i { width:10px; height:10px; border-radius:3px; background: var(--c); flex:none; }
  .pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:.85em; background: rgba(46,158,91,.18); }
  .tiles { display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:8px; margin-bottom:14px; }
  .tile { background: var(--secondary-background-color, rgba(127,127,127,.08)); border-radius: 8px; padding: 10px; min-width:0; }
  .tile .k { font-size:.75em; color: var(--secondary-text-color); }
  .tile .v { font-size:1.35em; font-weight:500; font-variant-numeric: tabular-nums; margin-top:2px; }
  .tile .s { font-size:.72em; color: var(--secondary-text-color); margin-top:3px; }
  .tile .s.pos { color: var(--success-color, #2e9e5b); } .tile .s.neg { color: var(--error-color, #d6455d); }
  .socbar { height:6px; border-radius:3px; background: var(--divider-color); margin-top:6px; position:relative; overflow:hidden; }
  .socbar b { position:absolute; inset:0 auto 0 0; background: var(--ec-soc, #3b82f6); border-radius:3px; }
  .socbar u { position:absolute; top:0; bottom:0; width:2px; background: var(--secondary-text-color); }
  .grp + .grp { margin-top: 14px; }
  .grp h3 { font-size:.7em; font-weight:500; letter-spacing:.08em; text-transform:uppercase; color: var(--secondary-text-color); margin:0 0 2px; }
  table.kv { width:100%; border-collapse: collapse; font-size:.9em; }
  table.kv th { text-align:left; font-weight:400; color: var(--secondary-text-color); padding:5px 8px 5px 0; vertical-align:top; width:42%; }
  table.kv td { padding:5px 0; color: var(--primary-text-color); }
  table.kv tr + tr { border-top: 1px solid var(--divider-color); }
  table.kv .n { font-variant-numeric: tabular-nums; }
  .tscroll { overflow-x:auto; scrollbar-width: thin; }
  table.dt { width:100%; border-collapse: collapse; font-size:.86em; color: var(--primary-text-color); }
  table.dt th { font-size:.82em; font-weight:500; color: var(--secondary-text-color); text-align:left; padding:6px 8px; border-bottom:1px solid var(--divider-color); white-space:nowrap; }
  table.dt tr.grph th { border-bottom: 0; padding-bottom: 0; }
  table.dt th.c { text-align:center; }
  table.dt td { padding:6px 8px; border-bottom:1px solid var(--divider-color); vertical-align: top; }
  table.dt .n { text-align:right; font-variant-numeric: tabular-nums; white-space:nowrap; }
  table.dt .n.l { text-align:left; }
  table.dt.tight th, table.dt.tight td { padding-left: 5px; padding-right: 5px; }
  table.dt.tight th:first-child, table.dt.tight td:first-child { padding-left: 0; }
  table.dt.tight th:last-child, table.dt.tight td:last-child { padding-right: 0; }
  table.dt td.p { color: var(--secondary-text-color); }
  table.dt .u { color: var(--secondary-text-color); font-size:.8em; margin-left:2px; }
  table.dt .sub { font-size:.8em; color: var(--secondary-text-color); }
  table.dt .sub.pos { color: var(--success-color, #2e9e5b); } table.dt .sub.neg { color: var(--error-color, #d6455d); }
  table.dt tbody tr:not(.day):hover td { background: var(--secondary-background-color, rgba(127,127,127,.06)); }
  table.dt tr.now td { box-shadow: inset 0 2px 0 var(--primary-color); }
  table.dt tr.day td { border-bottom:0; font-size:.75em; font-weight:500; letter-spacing:.06em; text-transform:uppercase; color: var(--secondary-text-color); padding-top:14px; }
  table.dt tfoot td { font-weight:500; border-bottom:0; border-top:2px solid var(--divider-color); }
`;

if (!customElements.get("energy-control-card")) {
  customElements.define("energy-control-card", EnergyControlCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "energy-control-card",
    name: "Energy Control",
    description: "Battery plan timeline, schedule, plan vs actual history and summary.",
    preview: false,
  });
  console.info(`%c ENERGY-CONTROL-CARD %c ${CARD_VERSION} `, "background:#2e9e5b;color:#fff", "");
}
