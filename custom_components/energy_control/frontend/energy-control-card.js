/*
 * Energy Control card — plan timeline, planned vs actual, summary.
 * Dependency-free web component; data via websocket `energy_control/subscribe`.
 */
const CARD_VERSION = "0.1.0";

const ACTION_COLORS = {
  self_consume: "var(--ec-self, #8a94a6)",
  hold: "var(--ec-hold, #6c8ebf)",
  charge_grid: "var(--ec-charge, #2e9e5b)",
  discharge_grid: "var(--ec-discharge, #e8833a)",
};
const ACTION_LABELS = {
  self_consume: "Self consumption",
  hold: "Hold",
  charge_grid: "Charge from grid",
  discharge_grid: "Discharge to grid",
};
const C = {
  pv: "var(--ec-pv, #e0a800)",
  load: "var(--ec-load, #5b7fb3)",
  grid: "var(--ec-grid, #8e6cbf)",
  soc: "var(--ec-soc, #3b82f6)",
  actual: "var(--ec-actual, #d6455d)",
  price: "var(--ec-price, #9aa0a6)",
  neg: "var(--ec-neg, #d6455d)",
  charge: ACTION_COLORS.charge_grid,
  discharge: ACTION_COLORS.discharge_grid,
};

const fmtTime = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtDay = (d) => d.toLocaleDateString([], { weekday: "short" });
const ct = (eur) => (eur == null ? "–" : `${(eur * 100).toFixed(1)} ct`);
const eur = (v) => (v == null ? "–" : `€ ${v.toFixed(2)}`);
const kw = (v) => (v == null ? "–" : `${v.toFixed(2)} kW`);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

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
    this._hover = null;
  }

  static getStubConfig() {
    return { hours: 36, history_hours: 24 };
  }

  setConfig(config) {
    this._config = { hours: 36, history_hours: 24, ...config };
    if (this._config.tab) this._tab = this._config.tab;
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first || !this._unsub) this._subscribe();
  }

  getCardSize() {
    return 8;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: 8, min_rows: 6 };
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

    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        <div class="head">
          <div class="title">${esc(title)}</div>
          ${d ? this._headline(d) : ""}
        </div>
        <div class="tabs">${tabs}</div>
        <div class="body">${body}</div>
        <div class="tip" hidden></div>
      </ha-card>`;
    this.shadowRoot.querySelectorAll(".tab").forEach((b) =>
      b.addEventListener("click", () => {
        this._tab = b.dataset.tab;
        this._render();
      }),
    );
    this._bindHover();
  }

  _headline(d) {
    const now = new Date();
    const slot = d.plan?.slots?.find((s) => {
      const t = new Date(s.start);
      return t <= now && now < new Date(t.getTime() + s.hours * 3600e3);
    });
    const action = d.desired?.action ?? slot?.action;
    const chips = [
      `<span class="chip">${esc(d.mode)}</span>`,
      action ? `<span class="chip" style="background:${ACTION_COLORS[action]}">${esc(ACTION_LABELS[action] ?? action)}</span>` : "",
      d.soc != null ? `<span class="chip ghost">SoC ${Math.round(d.soc)}%</span>` : "",
      d.curtailing ? `<span class="chip" style="background:${C.neg}">PV curtailed</span>` : "",
      d.override ? `<span class="chip ghost">override until ${fmtTime(new Date(d.override.until))}</span>` : "",
      d.status && !["ok", "advisory", "off"].includes(d.status) ? `<span class="chip warn">${esc(d.status)}</span>` : "",
    ].join("");
    const reason = slot?.reason ? `<div class="reason">${esc(slot.reason)}</div>` : "";
    return `<div class="chips">${chips}</div>${reason}`;
  }

  // ------------------------------------------------------------- plan view
  _planView(d) {
    const slots = (d.plan?.slots ?? []).map((s) => ({ ...s, t: new Date(s.start).getTime() }));
    if (!slots.length) return `<div class="msg">No plan${d.last_error ? `: ${esc(d.last_error)}` : ""}</div>`;
    const t0 = slots[0].t;
    const tEnd = Math.min(
      slots[slots.length - 1].t + slots[slots.length - 1].hours * 3600e3,
      t0 + Number(this._config.hours) * 3600e3,
    );
    const shown = slots.filter((s) => s.t < tEnd);
    this._hoverRows = shown.map((s) => ({
      t: s.t,
      t1: s.t + s.hours * 3600e3,
      html: `<b>${fmtDay(new Date(s.t))} ${fmtTime(new Date(s.t))}</b>
        <div><i style="background:${ACTION_COLORS[s.action]}"></i>${esc(ACTION_LABELS[s.action] ?? s.action)}</div>
        <div>Import ${ct(s.import_price)} · Export ${ct(s.export_price)}</div>
        <div>PV ${kw(s.pv_kw)} · Load ${kw(s.load_kw)}</div>
        <div>Battery ${kw(s.battery_kw)} · Grid ${kw(s.grid_kw)}</div>
        ${s.curtail_kw > 0 ? `<div>Curtail ${kw(s.curtail_kw)}</div>` : ""}
        <div>SoC → ${s.soc_end_pct.toFixed(0)}%</div>
        ${s.reason ? `<div class="r">${esc(s.reason)}</div>` : ""}`,
    }));

    const W = this._width;
    const m = { l: 42, r: 10 };
    const x = (t) => m.l + ((t - t0) / (tEnd - t0)) * (W - m.l - m.r);
    const sw = (s) => Math.max(1, x(s.t + s.hours * 3600e3) - x(s.t));

    const g = new Svg(W);
    // Action band
    const bandY = 4;
    g.text(4, bandY + 9, "action", "lbl");
    for (const s of shown) g.rect(x(s.t), bandY, sw(s), 10, ACTION_COLORS[s.action]);

    // Price pane
    const pY = 22, pH = 90;
    const prices = shown.flatMap((s) => [s.import_price * 100, s.export_price * 100]);
    const [pMin, pMax] = niceRange(Math.min(0, ...prices), Math.max(...prices));
    const py = (v) => pY + pH - ((v - pMin) / (pMax - pMin)) * pH;
    g.axis(m.l, py, [pMin, pMax], "ct", W - m.r);
    for (const s of shown) {
      const v = s.import_price * 100;
      g.rect(x(s.t) + 0.5, Math.min(py(v), py(0)), Math.max(0.5, sw(s) - 1), Math.abs(py(v) - py(0)), v < 0 ? C.neg : C.price, 0.55);
    }
    g.path(shown.map((s) => [x(s.t), py(s.export_price * 100), x(s.t) + sw(s), py(s.export_price * 100)]).flat(), C.grid, { step: true, dash: "4 3" });

    // Power pane
    const wY = pY + pH + 18, wH = 120;
    const powers = shown.flatMap((s) => [s.pv_kw, s.load_kw, s.battery_kw, s.grid_kw, -s.load_kw]);
    const [wMin, wMax] = niceRange(Math.min(0, ...powers), Math.max(...powers));
    const wy = (v) => wY + wH - ((v - wMin) / (wMax - wMin)) * wH;
    g.axis(m.l, wy, [wMin, wMax], "kW", W - m.r);
    g.area(shown.map((s) => [x(s.t), x(s.t) + sw(s), wy(s.pv_kw)]), wy(0), C.pv, 0.35);
    for (const s of shown) {
      if (Math.abs(s.battery_kw) < 1e-3) continue;
      const col = s.battery_kw > 0 ? C.charge : C.discharge;
      g.rect(x(s.t) + 1, Math.min(wy(s.battery_kw), wy(0)), Math.max(0.5, sw(s) - 2), Math.abs(wy(s.battery_kw) - wy(0)), col, 0.85);
    }
    g.path(shown.map((s) => [x(s.t), wy(s.load_kw), x(s.t) + sw(s), wy(s.load_kw)]).flat(), C.load, { step: true });
    g.path(shown.map((s) => [x(s.t), wy(s.grid_kw), x(s.t) + sw(s), wy(s.grid_kw)]).flat(), C.grid, { step: true, width: 1.2 });

    // SoC pane
    const sY = wY + wH + 18, sH = 70;
    const sy = (v) => sY + sH - (v / 100) * sH;
    g.axis(m.l, sy, [0, 100], "%", W - m.r, [0, 50, 100]);
    g.hline(m.l, W - m.r, sy(d.min_soc), "var(--secondary-text-color)", "2 3");
    g.hline(m.l, W - m.r, sy(d.max_soc), "var(--secondary-text-color)", "2 3");
    const socPts = [[x(shown[0].t), sy(shown[0].soc_start_pct)], ...shown.map((s) => [x(s.t) + sw(s), sy(s.soc_end_pct)])];
    g.path(socPts.flat(), C.soc, { width: 2 });

    const bottom = sY + sH + 16;
    g.timeAxis(x, t0, tEnd, bottom, sY + sH);
    g.nowLine(x(Date.now()), 2, sY + sH);
    g.height = bottom + 4;

    const legend = [
      [C.price, "Import price"],
      [C.grid, "Export price / grid"],
      [C.pv, "PV forecast"],
      [C.load, "Load forecast"],
      [C.charge, "Charge"],
      [C.discharge, "Discharge"],
      [C.soc, "SoC plan"],
    ]
      .map(([c, l]) => `<span><i style="background:${c}"></i>${l}</span>`)
      .join("");
    this._hoverGeom = { x, top: 0, bottom: sY + sH };
    return `<div class="chart">${g.render()}</div><div class="legend">${legend}</div>`;
  }

  // ---------------------------------------------------------- history view
  _historyView(d) {
    const rows = (d.history ?? []).map((r) => ({ ...r, t: new Date(r.start).getTime() }));
    if (!rows.length) return `<div class="msg">No history yet — it fills per 15-minute slot while the integration runs.</div>`;
    const t0 = rows[0].t;
    const tEnd = rows[rows.length - 1].t + 15 * 60e3;
    const W = this._width;
    const m = { l: 42, r: 10 };
    const x = (t) => m.l + ((t - t0) / (tEnd - t0 || 1)) * (W - m.l - m.r);
    const sw = Math.max(1, x(t0 + 15 * 60e3) - x(t0));
    this._hoverRows = rows.map((r) => ({
      t: r.t,
      t1: r.t + 15 * 60e3,
      html: `<b>${fmtDay(new Date(r.t))} ${fmtTime(new Date(r.t))}</b>
        <div>Mode ${esc(r.mode)}${r.curtailed ? " · curtailed" : ""}</div>
        <div><i style="background:${ACTION_COLORS[r.planned_action] ?? C.price}"></i>Planned ${esc(ACTION_LABELS[r.planned_action] ?? r.planned_action ?? "–")}</div>
        <div>SoC plan ${r.planned_soc?.toFixed?.(0) ?? "–"}% · actual ${r.actual_soc?.toFixed?.(0) ?? "–"}%</div>
        <div>Grid plan ${kw(r.planned_grid_kw)} · actual ${kw(r.actual_grid_kw)}</div>
        <div>Battery plan ${kw(r.planned_battery_kw)} · actual ${kw(r.actual_battery_kw)}</div>
        <div>Cost plan ${eur(r.planned_cost)} · actual ${eur(r.actual_cost)}</div>`,
    }));

    const g = new Svg(W);
    const bandY = 4;
    g.text(4, bandY + 9, "plan", "lbl");
    for (const r of rows) if (r.planned_action) g.rect(x(r.t), bandY, sw, 10, ACTION_COLORS[r.planned_action]);

    // SoC
    const sY = 22, sH = 90;
    const sy = (v) => sY + sH - (v / 100) * sH;
    g.axis(m.l, sy, [0, 100], "%", W - m.r, [0, 50, 100]);
    const line = (key, yf) =>
      rows.filter((r) => r[key] != null).map((r) => [x(r.t) + sw, yf(r[key])]).flat();
    g.path(line("planned_soc", sy), C.soc, { width: 2, dash: "5 3" });
    g.path(line("actual_soc", sy), C.actual, { width: 2 });

    // Grid & battery power
    const wY = sY + sH + 18, wH = 120;
    const vals = rows.flatMap((r) => [r.planned_grid_kw, r.actual_grid_kw, r.planned_battery_kw, r.actual_battery_kw]).filter((v) => v != null);
    const [wMin, wMax] = niceRange(Math.min(0, ...vals), Math.max(0, ...vals));
    const wy = (v) => wY + wH - ((v - wMin) / (wMax - wMin)) * wH;
    g.axis(m.l, wy, [wMin, wMax], "kW", W - m.r);
    const step = (key) =>
      rows.filter((r) => r[key] != null).map((r) => [x(r.t), wy(r[key]), x(r.t) + sw, wy(r[key])]).flat();
    g.path(step("planned_grid_kw"), C.grid, { step: true, dash: "5 3" });
    g.path(step("actual_grid_kw"), C.grid, { step: true, width: 1.6 });
    g.path(step("planned_battery_kw"), C.charge, { step: true, dash: "5 3" });
    g.path(step("actual_battery_kw"), C.charge, { step: true, width: 1.6 });

    // Cumulative cost
    const cY = wY + wH + 18, cH = 70;
    let accP = 0, accA = 0;
    const cum = rows.map((r) => ({ t: r.t, p: (accP += r.planned_cost ?? 0), a: (accA += r.actual_cost ?? 0) }));
    const [cMin, cMax] = niceRange(Math.min(0, ...cum.map((c) => Math.min(c.p, c.a))), Math.max(0, ...cum.map((c) => Math.max(c.p, c.a))));
    const cy = (v) => cY + cH - ((v - cMin) / (cMax - cMin)) * cH;
    g.axis(m.l, cy, [cMin, cMax], "€", W - m.r);
    g.path(cum.map((c) => [x(c.t) + sw, cy(c.p)]).flat(), C.price, { width: 2, dash: "5 3" });
    g.path(cum.map((c) => [x(c.t) + sw, cy(c.a)]).flat(), C.actual, { width: 2 });

    const bottom = cY + cH + 16;
    g.timeAxis(x, t0, tEnd, bottom, cY + cH);
    g.height = bottom + 4;
    this._hoverGeom = { x, top: 0, bottom: cY + cH };
    const legend = [
      [C.soc, "SoC planned (dashed)"],
      [C.actual, "SoC / cost actual"],
      [C.grid, "Grid planned/actual"],
      [C.charge, "Battery planned/actual"],
      [C.price, "Cost planned (cum.)"],
    ]
      .map(([c, l]) => `<span><i style="background:${c}"></i>${l}</span>`)
      .join("");
    return `<div class="chart">${g.render()}</div><div class="legend">${legend}</div>`;
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
    const svg = this.shadowRoot.querySelector("svg");
    const tip = this.shadowRoot.querySelector(".tip");
    if (!svg || !this._hoverRows || !this._hoverGeom) return;
    const cursor = this.shadowRoot.querySelector(".cursor");
    const move = (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = ((ev.clientX - rect.left) / rect.width) * this._width;
      const row = this._hoverRows.find((r) => this._hoverGeom.x(r.t) <= px && px < this._hoverGeom.x(r.t1));
      if (!row) {
        tip.hidden = true;
        cursor?.setAttribute("visibility", "hidden");
        return;
      }
      tip.innerHTML = row.html;
      tip.hidden = false;
      const card = this.shadowRoot.querySelector("ha-card").getBoundingClientRect();
      const left = ev.clientX - card.left + 12;
      tip.style.left = `${Math.min(left, card.width - tip.offsetWidth - 8)}px`;
      tip.style.top = `${ev.clientY - card.top + 12}px`;
      if (cursor) {
        const cx = this._hoverGeom.x(row.t);
        cursor.setAttribute("x", cx);
        cursor.setAttribute("width", Math.max(1, this._hoverGeom.x(row.t1) - cx));
        cursor.setAttribute("visibility", "visible");
      }
    };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerleave", () => {
      tip.hidden = true;
      cursor?.setAttribute("visibility", "hidden");
    });
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
    this.parts.push(`<text x="${x}" y="${y}" class="${cls}" text-anchor="${anchor}">${esc(s)}</text>`);
  }
  hline(x1, x2, y, stroke, dash = "") {
    this.parts.push(`<line x1="${x1}" x2="${x2}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${stroke}" stroke-dasharray="${dash}" stroke-width="1" opacity="0.6"/>`);
  }
  path(coords, stroke, { width = 1.4, dash = "", step = false } = {}) {
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
  axis(x0, yf, [min, max], unit, x1, ticks) {
    const t = ticks ?? niceTicks(min, max, 4);
    for (const v of t) {
      const y = yf(v);
      this.parts.push(`<line x1="${x0}" x2="${x1}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" class="grid${v === 0 ? " zero" : ""}"/>`);
      this.text(x0 - 4, y + 3, `${+v.toFixed(2)}`, "lbl", "end");
    }
    this.text(x0 - 4, yf(max) - 3 < 10 ? 10 : yf(max) - 3, unit, "lbl unit", "end");
  }
  timeAxis(x, t0, t1, y, gridBottom) {
    const span = t1 - t0;
    const stepH = span > 30 * 3600e3 ? 6 : span > 12 * 3600e3 ? 3 : 1;
    const start = new Date(t0);
    start.setMinutes(0, 0, 0);
    for (let t = start.getTime(); t <= t1; t += 3600e3) {
      const d = new Date(t);
      if (t < t0 || d.getHours() % stepH) continue;
      const px = x(t);
      const midnight = d.getHours() === 0;
      this.parts.push(`<line x1="${px.toFixed(1)}" x2="${px.toFixed(1)}" y1="0" y2="${gridBottom}" class="vgrid${midnight ? " day" : ""}"/>`);
      this.text(px, y, midnight ? fmtDay(d) : `${String(d.getHours()).padStart(2, "0")}:00`, "lbl", "middle");
    }
  }
  nowLine(px, y0, y1) {
    this.parts.push(`<line x1="${px.toFixed(1)}" x2="${px.toFixed(1)}" y1="${y0}" y2="${y1}" class="now"/>`);
  }
  render() {
    const cursor = `<rect class="cursor" x="0" y="0" width="1" height="${this.height}" visibility="hidden"/>`;
    return `<svg viewBox="0 0 ${this.w} ${this.height}" width="100%" height="${this.height}" preserveAspectRatio="none">${cursor}${this.parts.join("")}</svg>`;
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
  .tabs { display:flex; gap:4px; border-bottom: 1px solid var(--divider-color); margin: 4px 0 8px; }
  .tab { background:none; border:none; padding:6px 10px; cursor:pointer; color: var(--secondary-text-color); font: inherit; font-size:.9em; border-bottom: 2px solid transparent; }
  .tab.on { color: var(--primary-color); border-bottom-color: var(--primary-color); }
  .chart svg { display:block; touch-action: pan-y; }
  .chart .lbl { font-size: 10px; fill: var(--secondary-text-color); }
  .chart .grid { stroke: var(--divider-color); stroke-width: .6; }
  .chart .grid.zero { stroke: var(--secondary-text-color); stroke-width: .8; }
  .chart .vgrid { stroke: var(--divider-color); stroke-width: .5; opacity: .6; }
  .chart .vgrid.day { stroke: var(--secondary-text-color); opacity: .8; }
  .chart .now { stroke: var(--primary-color); stroke-width: 1.5; stroke-dasharray: 3 2; }
  .chart .cursor { fill: var(--primary-text-color); fill-opacity: .08; }
  .legend { display:flex; flex-wrap:wrap; gap:4px 12px; font-size:.78em; color: var(--secondary-text-color); margin-top:4px; }
  .legend i, .tip i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:-1px; }
  .msg { padding: 24px 8px; color: var(--secondary-text-color); text-align:center; }
  .msg.err, .errtxt { color: var(--error-color, #d6455d); }
  .tip { position:absolute; z-index:2; pointer-events:none; background: var(--card-background-color, #fff); color: var(--primary-text-color);
         border: 1px solid var(--divider-color); border-radius: 6px; padding: 6px 8px; font-size: .78em; line-height: 1.45; box-shadow: 0 2px 8px rgba(0,0,0,.2); max-width: 260px; }
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
