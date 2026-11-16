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
    this._tab = "plan";
    this._data = null;
    this._width = 600;
    this._scroll = {};
  }

  static getStubConfig() {
    return { hours: 36, history_hours: 24 };
  }

  setConfig(config) {
    this._config = { hours: 36, history_hours: 24, hour_width: 0, ...config };
    if (this._config.tab) this._tab = this._config.tab;
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
      const msg = { type: "energy_control/subscribe", history_hours: Number(this._config?.history_hours ?? 24) };
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
    else if (this._tab === "plan") body = this._planView(d);
    else if (this._tab === "history") body = this._historyView(d);
    else body = this._summaryView(d);

    const tabs = [
      ["plan", "Plan"],
      ["history", "Planned vs actual"],
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
    const ranges =
      this._tab === "plan"
        ? RANGES.map((h) => `<button class="seg ${this._hours === h ? "on" : ""}" data-hours="${h}">${h}h</button>`).join("")
        : "";
    return `<span class="ctl">${ranges}
      <button class="seg" data-zoom="-1" title="Zoom out (fit)" ${zoomIdx === 0 ? "disabled" : ""}>−</button>
      <button class="seg" data-zoom="1" title="Zoom in (scroll)" ${zoomIdx === ZOOMS.length - 1 ? "disabled" : ""}>+</button></span>`;
  }

  _restoreScroll() {
    const scroller = this.shadowRoot.querySelector(".scroll");
    if (!scroller) return;
    const saved = this._scroll[this._tab];
    if (this._tab === "history") {
      // Newest data on the right: follow it unless the user scrolled back.
      scroller.scrollLeft = !saved || saved.atEnd ? scroller.scrollWidth : saved.left;
    } else if (saved) {
      scroller.scrollLeft = saved.left;
    }
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

  // ------------------------------------------------------------- plan view
  _planView(d) {
    const slots = (d.plan?.slots ?? []).map((s) => ({ ...s, t: new Date(s.start).getTime() }));
    if (!slots.length) return `<div class="msg">No plan${d.last_error ? `: ${esc(d.last_error)}` : ""}</div>`;
    const t0 = slots[0].t;
    const tEnd = Math.min(slots[slots.length - 1].t + slots[slots.length - 1].hours * 3600e3, t0 + this._hours * 3600e3);
    const shown = slots.filter((s) => s.t < tEnd).map((s) => ({ ...s, kind: kindOf(s.action, s.battery_kw) }));
    this._hoverRows = shown.map((s) => ({
      t: s.t,
      t1: s.t + s.hours * 3600e3,
      html: `<b>${fmtDay(new Date(s.t))} ${fmtTime(new Date(s.t))}</b>
        <div><i style="--c:${KINDS[s.kind].color}"></i>${esc(KINDS[s.kind].label)}</div>
        ${s.reason ? `<div class="r">${esc(s.reason)}</div>` : ""}
        <div>Import ${ct(s.import_price)} · Export ${ct(s.export_price)}</div>
        <div>PV ${kw(s.pv_kw)} · Load ${kw(s.load_kw)}</div>
        <div>Battery ${kw(s.battery_kw)} · Grid ${kw(s.grid_kw)}</div>
        ${s.curtail_kw > 0 ? `<div>Curtail ${kw(s.curtail_kw)}</div>` : ""}
        <div>SoC → ${s.soc_end_pct.toFixed(0)}%</div>`,
    }));

    const { W, x } = this._frame(t0, tEnd);
    const sw = (s) => Math.max(1, x(s.t + s.hours * 3600e3) - x(s.t));
    const g = new Svg(W);
    const a = new Svg(AXIS_W);

    // Action band with labelled segments.
    const bandY = 4, bandH = 20;
    a.text(AXIS_W - 6, bandY + 14, "Action", "pane", "end");
    g.band(shown.map((s) => ({ x0: x(s.t), x1: x(s.t) + sw(s), key: s.kind })), bandY, bandH, KINDS);

    // Price pane.
    const pY = bandY + bandH + 34, pH = 110;
    const prices = shown.flatMap((s) => [s.import_price * 100, s.export_price * 100]);
    const [pMin, pMax] = niceRange(Math.min(0, ...prices), Math.max(...prices));
    const py = (v) => pY + pH - ((v - pMin) / (pMax - pMin)) * pH;
    g.paneGrid(py, [pMin, pMax], W);
    a.paneAxis(py, [pMin, pMax], "Price", "ct/kWh");
    g.area(shown.map((s) => [x(s.t), x(s.t) + sw(s), py(Math.max(0, s.import_price * 100))]), py(0), C.price, 0.22);
    for (const s of shown) {
      const v = s.import_price * 100;
      if (v < 0) g.rect(x(s.t), py(0), sw(s), py(v) - py(0), C.neg, 0.45);
    }
    g.path(shown.flatMap((s) => [x(s.t), py(s.import_price * 100), x(s.t) + sw(s), py(s.import_price * 100)]), C.price, { width: 1.6 });
    g.path(shown.flatMap((s) => [x(s.t), py(s.export_price * 100), x(s.t) + sw(s), py(s.export_price * 100)]), C.grid, { dash: "5 3", width: 1.4 });

    // Power pane: battery bars (+ charge / − discharge), PV area, load and grid lines.
    const wY = pY + pH + 34, wH = 170;
    const powers = shown.flatMap((s) => [s.pv_kw, s.load_kw, s.battery_kw, s.grid_kw]);
    const [wMin, wMax] = niceRange(Math.min(0, ...powers), Math.max(0, ...powers));
    const wy = (v) => wY + wH - ((v - wMin) / (wMax - wMin)) * wH;
    g.paneGrid(wy, [wMin, wMax], W);
    a.paneAxis(wy, [wMin, wMax], "Power", "kW");
    g.area(shown.map((s) => [x(s.t), x(s.t) + sw(s), wy(s.pv_kw)]), wy(0), C.pv, 0.3);
    for (const s of shown) {
      if (Math.abs(s.battery_kw) < 1e-3) continue;
      const col = KINDS[s.kind === "idle" || s.kind === "hold" ? (s.battery_kw > 0 ? "store" : "cover") : s.kind].color;
      g.rect(x(s.t) + 1, Math.min(wy(s.battery_kw), wy(0)), Math.max(0.5, sw(s) - 2), Math.abs(wy(s.battery_kw) - wy(0)), col, 0.9);
    }
    g.path(shown.flatMap((s) => [x(s.t), wy(s.load_kw), x(s.t) + sw(s), wy(s.load_kw)]), C.load, { width: 1.8 });
    g.path(shown.flatMap((s) => [x(s.t), wy(s.grid_kw), x(s.t) + sw(s), wy(s.grid_kw)]), C.grid, { width: 1.4, dash: "5 3" });
    if (wMin < 0) {
      g.text(x(t0) + 4, wy(wMin) - 4, "battery discharging ↓", "hint");
    }

    // SoC pane.
    const sY = wY + wH + 34, sH = 90;
    const sy = (v) => sY + sH - (v / 100) * sH;
    g.paneGrid(sy, [0, 100], W, [0, 25, 50, 75, 100]);
    a.paneAxis(sy, [0, 100], "SoC", "%", [0, 25, 50, 75, 100]);
    g.socBand(sy(d.max_soc), sy(d.min_soc), W);
    const socPts = [[x(shown[0].t), sy(shown[0].soc_start_pct)], ...shown.map((s) => [x(s.t) + sw(s), sy(s.soc_end_pct)])];
    g.area(socPts.slice(1).map(([px, py2], i) => [socPts[i][0], px, py2]), sy(0), C.soc, 0.12);
    g.path(socPts.flat(), C.soc, { width: 2.2 });

    const bottom = sY + sH + 18;
    g.timeAxis(x, t0, tEnd, bottom, sY + sH, this._frame(t0, tEnd).pxPerHour);
    g.nowLine(x(Date.now()), 2, sY + sH);
    g.height = a.height = bottom + 4;
    this._hoverGeom = { x };

    const legend = legendHtml([
      ...Object.values(KINDS).map((k) => [k.color, k.label]),
      [C.price, "Import price", "line"],
      [C.grid, "Export price / grid", "dash"],
      [C.pv, "PV forecast"],
      [C.load, "Load forecast", "line"],
      [C.soc, "SoC plan", "line"],
    ]);
    return this._chart(g, a, legend);
  }

  // ---------------------------------------------------------- history view
  _historyView(d) {
    const rows = (d.history ?? []).map((r) => ({ ...r, t: new Date(r.start).getTime() }));
    if (!rows.length) return `<div class="msg">No history yet — it fills per 15-minute slot while the integration runs.</div>`;
    const t0 = rows[0].t;
    const tEnd = rows[rows.length - 1].t + 15 * 60e3;
    const { W, x, pxPerHour } = this._frame(t0, tEnd);
    const sw = Math.max(1, x(t0 + 15 * 60e3) - x(t0));
    this._hoverRows = rows.map((r) => {
      const kind = r.planned_action ? kindOf(r.planned_action, r.planned_battery_kw) : null;
      return {
        t: r.t,
        t1: r.t + 15 * 60e3,
        html: `<b>${fmtDay(new Date(r.t))} ${fmtTime(new Date(r.t))}</b>
          <div>Mode ${esc(r.mode)}${r.curtailed ? " · curtailed" : ""}</div>
          <div><i style="--c:${kind ? KINDS[kind].color : C.price}"></i>Planned ${esc(kind ? KINDS[kind].label : "–")}</div>
          <div>SoC plan ${r.planned_soc?.toFixed?.(0) ?? "–"}% · actual ${r.actual_soc?.toFixed?.(0) ?? "–"}%</div>
          <div>Grid plan ${kw(r.planned_grid_kw)} · actual ${kw(r.actual_grid_kw)}</div>
          <div>Battery plan ${kw(r.planned_battery_kw)} · actual ${kw(r.actual_battery_kw)}</div>
          <div>Cost plan ${eur(r.planned_cost)} · actual ${eur(r.actual_cost)}</div>`,
      };
    });

    const g = new Svg(W);
    const a = new Svg(AXIS_W);
    const bandY = 4, bandH = 20;
    a.text(AXIS_W - 6, bandY + 14, "Plan", "pane", "end");
    g.band(
      rows.filter((r) => r.planned_action).map((r) => ({ x0: x(r.t), x1: x(r.t) + sw, key: kindOf(r.planned_action, r.planned_battery_kw) })),
      bandY,
      bandH,
      KINDS,
    );

    // SoC
    const sY = bandY + bandH + 34, sH = 100;
    const sy = (v) => sY + sH - (v / 100) * sH;
    g.paneGrid(sy, [0, 100], W, [0, 25, 50, 75, 100]);
    a.paneAxis(sy, [0, 100], "SoC", "%", [0, 25, 50, 75, 100]);
    const line = (key, yf) => rows.filter((r) => r[key] != null).flatMap((r) => [x(r.t) + sw, yf(r[key])]);
    g.path(line("planned_soc", sy), C.soc, { width: 2, dash: "5 3" });
    g.path(line("actual_soc", sy), C.actual, { width: 2.2 });

    // Battery power
    const bY = sY + sH + 34, bH = 110;
    const bVals = rows.flatMap((r) => [r.planned_battery_kw, r.actual_battery_kw]).filter((v) => v != null);
    const [bMin, bMax] = niceRange(Math.min(0, ...bVals), Math.max(0, ...bVals));
    const by = (v) => bY + bH - ((v - bMin) / (bMax - bMin)) * bH;
    g.paneGrid(by, [bMin, bMax], W);
    a.paneAxis(by, [bMin, bMax], "Battery", "kW");
    const step = (key, yf) => rows.filter((r) => r[key] != null).flatMap((r) => [x(r.t), yf(r[key]), x(r.t) + sw, yf(r[key])]);
    g.path(step("planned_battery_kw", by), C.soc, { dash: "5 3", width: 1.6 });
    g.path(step("actual_battery_kw", by), C.actual, { width: 1.8 });

    // Grid power
    const gY = bY + bH + 34, gH = 110;
    const gVals = rows.flatMap((r) => [r.planned_grid_kw, r.actual_grid_kw]).filter((v) => v != null);
    const [gMin, gMax] = niceRange(Math.min(0, ...gVals), Math.max(0, ...gVals));
    const gy = (v) => gY + gH - ((v - gMin) / (gMax - gMin)) * gH;
    g.paneGrid(gy, [gMin, gMax], W);
    a.paneAxis(gy, [gMin, gMax], "Grid", "kW");
    g.path(step("planned_grid_kw", gy), C.soc, { dash: "5 3", width: 1.6 });
    g.path(step("actual_grid_kw", gy), C.actual, { width: 1.8 });

    // Cumulative cost
    const cY = gY + gH + 34, cH = 80;
    let accP = 0, accA = 0;
    const cum = rows.map((r) => ({ t: r.t, p: (accP += r.planned_cost ?? 0), a: (accA += r.actual_cost ?? 0) }));
    const [cMin, cMax] = niceRange(Math.min(0, ...cum.map((c) => Math.min(c.p, c.a))), Math.max(0, ...cum.map((c) => Math.max(c.p, c.a))));
    const cy = (v) => cY + cH - ((v - cMin) / (cMax - cMin)) * cH;
    g.paneGrid(cy, [cMin, cMax], W);
    a.paneAxis(cy, [cMin, cMax], "Cost", "€");
    g.path(cum.flatMap((c) => [x(c.t) + sw, cy(c.p)]), C.soc, { width: 2, dash: "5 3" });
    g.path(cum.flatMap((c) => [x(c.t) + sw, cy(c.a)]), C.actual, { width: 2.2 });

    const bottom = cY + cH + 18;
    g.timeAxis(x, t0, tEnd, bottom, cY + cH, pxPerHour);
    g.height = a.height = bottom + 4;
    this._hoverGeom = { x };
    const legend = legendHtml([
      ...Object.values(KINDS).map((k) => [k.color, k.label]),
      [C.soc, "Planned", "dash"],
      [C.actual, "Actual", "line"],
    ]);
    return this._chart(g, a, legend);
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
      ["Planned cost (horizon)", eur(plan?.total_cost)],
      ["Without battery", eur(plan?.baseline_cost)],
      ["Planned savings", eur(plan?.savings)],
      ["Cost today (actual / planned)", `${eur(d.cost_today)} / ${eur(d.planned_cost_today)}`],
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
