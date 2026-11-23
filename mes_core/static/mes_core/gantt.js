/* Diagrama de Gantt interactivo.
   Datos: API del programador (mismos bloques, conflictos, turnos y mantenimientos).
   Edición: arrastrar (mover / cambiar de línea compatible), estirar el borde derecho y panel lateral. */
(() => {
  "use strict";
  const root = document.getElementById("gt");
  if (!root || !document.getElementById("gt-wrap")) return;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const num = (n, d = 1) => Number(n ?? 0).toLocaleString("es", { maximumFractionDigits: d });
  const csrf = () => document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
  const store = (k, v) => {
    try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { /* sin almacenamiento */ }
    return null;
  };
  const HOUR = 3600e3;
  const ZOOMS = [0.5, 0.75, 1, 1.5, 2, 3, 4];
  const MIN_PXH = { dia: 48, semana: 14, mes: 2 };          // px por hora mínimos (legibilidad)
  const SNAP_MIN = { dia: 15, semana: 30, mes: 120 };          // ajuste al arrastrar
  const CONFLICT = {
    solape: "Supera la capacidad de la línea en ese horario",
    secuencia: "Empieza antes de que termine la etapa anterior del pedido",
    mantenimiento: "Se cruza con un mantenimiento de la línea",
    operarios: "Supera los operarios disponibles del turno",
  };
  const STATE_LABEL = { planned: "Planificada", released: "Liberada", running: "En proceso", done: "Terminada" };

  const canPlan = root.dataset.canPlan === "1";
  const params = new URLSearchParams(location.search);
  const state = {
    site: params.get("site") || root.dataset.site || "",
    view: ["dia", "semana", "mes"].includes(params.get("view")) ? params.get("view") : (store("gt.view") || "semana"),
    date: params.get("date") || "",
    group: store("gt.group") === "orden" ? "orden" : "linea",
    zoom: Math.max(0, ZOOMS.indexOf(Number(store("gt.zoom")) || 1)),
    q: "",
    data: null,
    first: true,
    drag: null,
    sel: null,
  };
  if (!["dia", "semana", "mes"].includes(state.view)) state.view = "semana";
  const wrap = $("#gt-wrap");
  const tipEl = $("#gt-tip");
  const panel = $("#gt-panel");

  // ---------- zona horaria de la planta ----------
  let tz = "UTC";
  const dtf = {};
  const fmt = (opts) => {
    const k = JSON.stringify(opts) + tz;
    return (dtf[k] ||= new Intl.DateTimeFormat("es", { timeZone: tz, hour12: false, ...opts }));
  };
  function offsetMs(ms) {
    const p = {};
    new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit" }).formatToParts(new Date(ms)).forEach((x) => (p[x.type] = x.value));
    return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - ms;
  }
  function wallToMs(dateStr, timeStr = "00:00") {
    const [y, mo, d] = dateStr.split("-").map(Number);
    const [h, mi] = timeStr.split(":").map(Number);
    const guess = Date.UTC(y, mo - 1, d, h, mi);
    const first = guess - offsetMs(guess);
    return guess - offsetMs(first);
  }
  function msToWall(ms) {  // "YYYY-MM-DDTHH:MM" en hora de la planta
    const p = {};
    new Intl.DateTimeFormat("en-CA", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit" }).formatToParts(new Date(ms)).forEach((x) => (p[x.type] = x.value));
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
  }
  const addDays = (dateStr, n) => {
    const d = new Date(`${dateStr}T12:00:00Z`);
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  };
  const tHM = (ms) => fmt({ hour: "2-digit", minute: "2-digit" }).format(ms);
  const tDay = (ms) => fmt({ weekday: "short", day: "2-digit", month: "2-digit" }).format(ms);
  const tFull = (ms) => fmt({ weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(ms);
  const span = (s, e) => `${tFull(s)} → ${tDay(s) === tDay(e) ? tHM(e) : tFull(e)}`;
  const dur = (ms) => {
    const m = Math.round(ms / 60000);
    const h = Math.floor(m / 60);
    return h >= 24 ? `${num(m / 1440, 1)} d (${h} h)` : `${h} h ${String(m % 60).padStart(2, "0")} min`;
  };

  // ---------- red ----------
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (r.status === 409 && d.frozen && !body.force) {
      if (window.confirm(d.error)) return post(url, { ...body, force: true });
      throw new Error("Cambio cancelado: el plan congelado no se tocó.");
    }
    if (!r.ok) throw new Error(d.error || `Error ${r.status}`);
    return d;
  }
  let toastT;
  function toast(msg, err = false) {
    const t = $("#gt-toast");
    t.textContent = msg;
    t.classList.toggle("err", err);
    t.classList.add("show");
    clearTimeout(toastT);
    toastT = setTimeout(() => t.classList.remove("show"), err ? 6000 : 3000);
  }

  async function load() {
    const url = new URL(root.dataset.api, location.href);
    url.searchParams.set("view", state.view);
    if (state.site) url.searchParams.set("site", state.site);
    if (state.date) url.searchParams.set("date", state.date);
    try {
      const r = await fetch(url, { headers: { Accept: "application/json" } });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || `Error ${r.status}`);
      state.data = d;
      state.site = String(d.site.id);
      state.date = d.anchor;
      tz = d.tz || tz;
      const q = new URLSearchParams({ site: state.site, view: state.view, date: state.date });
      history.replaceState(null, "", `${location.pathname}?${q}`);
      render();
    } catch (e) {
      wrap.innerHTML = `<div class="gt-empty">No se pudo cargar el diagrama: ${esc(e.message)}</div>`;
    }
  }

  // ---------- geometría ----------
  let G = null;  // { t0, t1, pxh, lblW }
  const xOf = (ms) => ((ms - G.t0) / HOUR) * G.pxh;

  function shiftWindow(sh, day) {
    let s = wallToMs(day, sh.start), e;
    if (sh.end === "00:00") e = wallToMs(addDays(day, 1), "00:00");
    else if (sh.end <= sh.start) { s = wallToMs(addDays(day, -1), sh.start); e = wallToMs(day, sh.end); }
    else e = wallToMs(day, sh.end);
    return [s, e];
  }
  function offFromWindows(wins) {
    const off = [];
    let cur = G.t0;
    for (const [a, b] of wins.map(([x, y]) => [Date.parse(x), Date.parse(y)]).sort((p, q) => p[0] - q[0])) {
      if (b <= cur) continue;
      if (a > cur) off.push([cur, Math.min(a, G.t1)]);
      cur = Math.max(cur, b);
      if (cur >= G.t1) break;
    }
    if (cur < G.t1) off.push([cur, G.t1]);
    return off.filter(([a, b]) => b > a);
  }
  function offSegments(shifts, lineId) {
    const lw = state.data.line_windows;
    if (lw && lineId && lw[lineId]) return offFromWindows(lw[lineId]);
    if (lw && !lineId) return offFromWindows(Object.values(lw).flat());
    if (!shifts.length) return [];
    const d = state.data;
    const wins = [];
    for (let day = addDays(d.days[0].date, -1); day <= addDays(d.days.at(-1).date, 1); day = addDays(day, 1)) {
      shifts.forEach((sh) => wins.push(shiftWindow(sh, day)));
    }
    wins.sort((a, b) => a[0] - b[0]);
    const off = [];
    let cur = G.t0;
    for (const [s, e] of wins) {
      if (e <= cur) continue;
      if (s > cur) off.push([cur, Math.min(s, G.t1)]);
      cur = Math.max(cur, e);
      if (cur >= G.t1) break;
    }
    if (cur < G.t1) off.push([cur, G.t1]);
    return off.filter(([a, b]) => b > a);
  }

  function lanes(items) {
    const ends = [];
    items.sort((a, b) => a.s - b.s);
    for (const it of items) {
      let i = ends.findIndex((e) => e <= it.s);
      if (i < 0) { i = ends.length; ends.push(0); }
      ends[i] = it.e;
      it.lane = i;
    }
    return Math.max(1, ends.length);
  }

  const matches = (b) => {
    if (!state.q) return true;
    const hay = `${b.order} ${b.product} ${b.sku} ${b.customer} ${b.op} ${lineById(b.line)?.code || ""}`.toLowerCase();
    return state.q.split(/\s+/).every((w) => hay.includes(w));
  };
  const lineById = (id) => state.data.all_lines.find((l) => l.id === id);

  // ---------- render ----------
  function render() {
    const d = state.data;
    if (!d) return;
    $("#gt-range").textContent = d.range_label;
    $$("#gt-view button").forEach((b) => b.classList.toggle("on", b.dataset.view === state.view));
    $$("#gt-group button").forEach((b) => b.classList.toggle("on", b.dataset.group === state.group));
    $("#gt-zlbl").textContent = `${Math.round(ZOOMS[state.zoom] * 100)}%`;
    $("#gt-sub").textContent = `${d.site.code} · ${d.site.name} · ${d.blocks.length} operaciones programadas · ${d.backlog.length} en backlog`;

    const cs = getComputedStyle(root);
    const lblW = parseFloat(cs.getPropertyValue("--lbl-w")) || 240;
    const barH = parseFloat(cs.getPropertyValue("--bar-h")) || 30;
    const pad = 8, gap = 4;
    const t0 = wallToMs(d.days[0].date);
    const t1 = wallToMs(addDays(d.days.at(-1).date, 1));
    const hours = (t1 - t0) / HOUR;
    const avail = Math.max(300, wrap.clientWidth - lblW - 2);
    const pxh = Math.max(avail / hours, MIN_PXH[state.view]) * ZOOMS[state.zoom];
    G = { t0, t1, pxh, lblW, barH, pad, gap };
    const tlW = Math.round(hours * pxh);
    const now = Date.parse(d.now);

    // filas
    const shiftsById = Object.fromEntries(d.shifts.map((s) => [s.id, s]));
    const blocks = d.blocks.map((b) => ({ ...b, s: Date.parse(b.start), e: Date.parse(b.end) }));
    let rows;
    if (state.group === "linea") {
      rows = d.lines.map((l) => {
        const meta = lineById(l.id);
        const own = (meta?.shifts || []).map((id) => shiftsById[id]).filter(Boolean);
        return { key: `l${l.id}`, line: l, kind: l.kind, shifts: own.length ? own : d.shifts,
          items: blocks.filter((b) => b.line === l.id), maint: d.downtimes.filter((m) => m.line === l.id) };
      });
    } else {
      const byOrder = new Map();
      blocks.forEach((b) => {
        if (!byOrder.has(b.order_id)) byOrder.set(b.order_id, []);
        byOrder.get(b.order_id).push(b);
      });
      rows = [...byOrder.values()].map((items) => ({ key: `o${items[0].order_id}`, order: items[0], items, shifts: d.shifts, maint: [] }))
        .sort((a, b) => Math.min(...a.items.map((x) => x.s)) - Math.min(...b.items.map((x) => x.s)));
      if (state.q) rows = rows.filter((r) => r.items.some(matches));
    }

    // encabezado de tiempo
    const t1Ticks = [], t2Ticks = [], vlines = [];
    const todayStr = d.today;
    if (state.view === "mes") {
      let i = 0;
      while (i < d.days.length) {
        const start = d.days[i].date;
        let j = i;
        while (j + 1 < d.days.length && new Date(`${d.days[j + 1].date}T12:00:00Z`).getUTCDay() !== 1) j++;
        const a = wallToMs(start), b = wallToMs(addDays(d.days[j].date, 1));
        t1Ticks.push({ x: xOf(a), w: xOf(b) - xOf(a), label: `${tDay(a)} – ${tDay(b - HOUR)}` });
        i = j + 1;
      }
      d.days.forEach((day) => {
        const a = wallToMs(day.date), b = wallToMs(addDays(day.date, 1));
        t2Ticks.push({ x: xOf(a), w: xOf(b) - xOf(a), label: (xOf(b) - xOf(a)) >= 22 ? String(day.day) : "", today: day.date === todayStr });
        vlines.push({ x: xOf(a), day: new Date(`${day.date}T12:00:00Z`).getUTCDay() === 1 });
      });
    } else {
      const step = state.view === "dia" ? (pxh >= 40 ? 1 : 2) : (pxh * 6 >= 44 ? 6 : 12);
      d.days.forEach((day) => {
        const a = wallToMs(day.date), b = wallToMs(addDays(day.date, 1));
        const hol = (d.holidays || []).find((x) => x.date === day.date);
        t1Ticks.push({ x: xOf(a), w: xOf(b) - xOf(a), label: fmt({ weekday: "long", day: "numeric", month: "short" }).format(a) + (hol ? ` · ${hol.name}` : ""),
                       today: day.date === todayStr, holiday: Boolean(hol) });
        vlines.push({ x: xOf(a), day: true });
        for (let h = step; h < 24; h += step) {
          const t = wallToMs(day.date, `${String(h).padStart(2, "0")}:00`);
          vlines.push({ x: xOf(t), day: false });
        }
        for (let h = 0; h < 24; h += step) {
          const t = wallToMs(day.date, `${String(h).padStart(2, "0")}:00`);
          t2Ticks.push({ x: xOf(t), w: step * pxh, label: `${String(h).padStart(2, "0")}:00`, minor: state.view === "dia" && h % 2 === 1 && pxh < 60 });
        }
      });
    }
    const tick = (t) => `<div class="gt-tk ${t.today ? "today" : ""} ${t.minor ? "minor" : ""} ${t.holiday ? "holiday" : ""}" style="left:${t.x}px;width:${t.w}px" title="${esc(t.label)}">${esc(t.label)}</div>`;

    let html = `<div class="gt-grid" style="--tl-w:${tlW}px">
      <div class="gt-corner">${state.group === "linea" ? "Línea" : "Pedido"}</div>
      <div class="gt-head"><div class="gt-t1">${t1Ticks.map(tick).join("")}</div><div class="gt-t2">${t2Ticks.map(tick).join("")}</div>
        ${now >= t0 && now <= t1 ? `<div class="gt-now-tag" style="left:${xOf(now)}px">Ahora ${tHM(now)}</div>` : ""}</div>`;

    if (!rows.length) {
      html += `<div class="gt-empty" style="grid-column:1/-1">${state.q ? "Ningún pedido coincide con la búsqueda." :
        state.group === "linea" ? "Esta planta no tiene líneas activas." : "No hay operaciones programadas en este periodo."}</div>`;
    }

    let top = 0;
    const rowTops = [];
    rows.forEach((r, idx) => {
      const items = r.items.filter((b) => b.e > t0 && b.s < t1);
      const n = lanes(items);
      const h = Math.max(54, pad * 2 + n * barH + (n - 1) * gap);
      r.h = h;
      rowTops.push(top);
      top += h;
      const alt = idx % 2 ? "alt" : "";
      let label;
      if (r.line) {
        const l = r.line;
        const lc = l.load >= 100 ? "over" : l.load >= 85 ? "hi" : "";
        label = `<div class="l1">${l.running ? '<i class="run" title="Produciendo"></i>' : ""}<span class="t">${esc(l.code)}</span>
            <span class="load ${lc}" title="Ocupación del periodo">${l.load}%</span></div>
          <div class="l2" title="${esc(l.name)}">${esc(l.name)} · ${esc(l.kind)}</div>`;
      } else {
        const o = r.order;
        label = `<div class="l1"><span class="tdot t-${esc(o.tier)}"></span><a class="t" href="${esc(o.order_url)}">${esc(o.order)}</a>
            ${o.late ? '<span class="load over">Atrasado</span>' : ""}</div>
          <div class="l2" title="${esc(o.product)}">${esc(o.product)}${o.customer ? ` · ${esc(o.customer)}` : ""}</div>`;
      }
      const off = offSegments(r.shifts, r.line ? r.line.id : null).map(([a, b]) => `<div class="gt-off" style="left:${xOf(a)}px;width:${xOf(b) - xOf(a)}px"></div>`).join("");
      const maint = r.maint.filter((m) => Date.parse(m.end) > t0 && Date.parse(m.start) < t1).map((m) => {
        const a = Math.max(Date.parse(m.start), t0), b = Math.min(Date.parse(m.end), t1);
        return `<div class="gt-maint" style="left:${xOf(a)}px;width:${Math.max(3, xOf(b) - xOf(a))}px;top:3px;bottom:3px"
          title="Mantenimiento: ${esc(m.reason)} · ${tFull(Date.parse(m.start))} → ${tFull(Date.parse(m.end))}"></div>`;
      }).join("");
      let due = "";
      if (r.order?.due) {
        const dm = Date.parse(r.order.due);
        if (dm >= t0 && dm <= t1) due = `<div class="gt-due" style="left:${xOf(dm)}px" title="Compromiso ${tFull(dm)}"></div>`;
      }
      const bars = items.map((b) => {
        const a = Math.max(b.s, t0), e = Math.min(b.e, t1);
        const left = xOf(a), w = Math.max(4, xOf(e) - xOf(a));
        const y = pad + b.lane * (barH + gap);
        const st = b.state;
        const conf = b.conflicts.length > 0;
        const drag = canPlan && st !== "done" && b.e > b.s;
        const pg = b.qty ? Math.min(100, (b.done / b.qty) * 100) : 0;
        const text = r.line ? `<b>${esc(b.order)}</b> · ${esc(b.op)}` : `<b>${esc(b.op)}</b> · ${esc(lineById(b.line)?.code || "")}`;
        return `<div class="gt-bar ${st} ${conf ? "conflict" : ""} ${drag ? "can-drag" : ""} ${w < 46 ? "narrow" : ""} ${matches(b) ? "" : "dim"} ${state.sel === b.id ? "hl" : ""}"
            data-id="${b.id}" style="left:${left}px;width:${w}px;top:${y}px">
            ${conf ? '<span class="w"><svg class="ico"><use href="#i-alert"/></svg></span>' : ""}<span class="t">${linkMark(b)}${text}</span>
            ${pg > 0 ? `<i class="pg" style="width:${pg}%"></i>` : ""}${drag ? '<span class="rz" title="Estirar"></span>' : ""}</div>`;
      }).join("");
      html += `<div class="gt-label ${alt}" data-key="${r.key}" style="height:${h}px">${label}</div>
        <div class="gt-row ${alt}" data-key="${r.key}" ${r.line ? `data-line="${r.line.id}" data-kind="${esc(r.kind)}"` : ""} style="height:${h}px">${off}${maint}${due}${bars}</div>`;
    });

    const fz = d.frozen_until ? Date.parse(d.frozen_until) : null;
    let frozenHtml = "";
    if (fz && fz > t0 && now < t1) {
      const a = Math.max(now, t0), b = Math.min(fz, t1);
      frozenHtml = `<div class="gt-frozen" style="left:${xOf(a)}px;width:${Math.max(0, xOf(b) - xOf(a))}px"><span>Plan congelado hasta ${tFull(fz)}</span></div>`;
    }
    const overlay = `<div style="position:absolute;left:${lblW}px;top:58px;width:${tlW}px;height:${top}px;pointer-events:none;z-index:2">${frozenHtml}
        ${vlines.map((v) => `<div class="gt-vline ${v.day ? "day" : ""}" style="left:${v.x}px"></div>`).join("")}
        ${now >= t0 && now <= t1 ? `<div class="gt-now" style="left:${xOf(now)}px"></div>` : ""}</div>`;
    const keepX = wrap.scrollLeft, keepY = wrap.scrollTop;
    wrap.innerHTML = html + overlay + "</div>";
    state.rows = rows;
    if (state.first || state.scrollToNow) {
      const target = now >= t0 && now <= t1 ? xOf(now) - (wrap.clientWidth - lblW) * 0.25 : 0;
      wrap.scrollLeft = Math.max(0, target);
      state.first = false;
      state.scrollToNow = false;
    } else {
      wrap.scrollLeft = keepX;
      wrap.scrollTop = keepY;
    }
  }

  const blockById = (id) => state.data?.blocks.find((b) => b.id === id);

  // ---------- tooltip ----------
  function showTip(b, ev) {
    const s = Date.parse(b.start), e = Date.parse(b.end);
    const line = lineById(b.line);
    tipEl.innerHTML = `<b>${esc(b.order)} · ${esc(b.op)}</b>
      <div class="r"><span>Producto</span><span>${esc(b.product)}</span></div>
      ${b.customer ? `<div class="r"><span>Cliente</span><span>${esc(b.customer)}</span></div>` : ""}
      <div class="r"><span>Línea</span><span>${esc(line ? line.code : "—")}</span></div>
      <div class="r"><span>Horario</span><span>${tFull(s)} → ${tHM(e)}${tDay(s) !== tDay(e) ? ` (${tDay(e)})` : ""}</span></div>
      <div class="r"><span>Duración</span><span>${dur(e - s)}</span></div>
      <div class="r"><span>Avance</span><span>${num(b.done, 0)} / ${num(b.qty, 0)} ${esc(b.unit)}</span></div>
      <div class="r"><span>Estado</span><span>${STATE_LABEL[b.state] || b.status_label}</span></div>
      ${b.conflicts.length ? `<div class="warn">${b.conflicts.map((c) => esc(CONFLICT[c] || c)).join("<br>")}</div>` : ""}`;
    tipEl.hidden = false;
    moveTip(ev);
  }
  function moveTip(ev) {
    const r = tipEl.getBoundingClientRect();
    let x = ev.clientX + 14, y = ev.clientY + 16;
    if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 12;
    tipEl.style.left = `${Math.max(8, x)}px`;
    tipEl.style.top = `${Math.max(8, y)}px`;
  }
  wrap.addEventListener("mouseover", (ev) => {
    if (state.drag) return;
    const el = ev.target.closest(".gt-bar");
    if (!el) return;
    const b = blockById(Number(el.dataset.id));
    if (b) showTip(b, ev);
  });
  wrap.addEventListener("mousemove", (ev) => { if (!tipEl.hidden) moveTip(ev); });
  wrap.addEventListener("mouseout", (ev) => { if (ev.target.closest(".gt-bar") && !ev.relatedTarget?.closest?.(".gt-bar")) tipEl.hidden = true; });
  wrap.addEventListener("scroll", () => { tipEl.hidden = true; }, { passive: true });

  const LINKS = [["cola", "En cola: cuando termine la anterior"], ["flujo", "A la par: en flujo con la anterior"], ["paralelo", "Al mismo tiempo: independiente"]];
  const LINK_MARK = { flujo: ["⤳", "Va a la par con la etapa anterior"], paralelo: ["∥", "Va al mismo tiempo que la etapa anterior"] };
  const linkText = (b) => (b.link === "flujo" ? `A la par con la etapa anterior${b.link_lag ? ` (arranca cuando lleva ${num(b.link_lag, 3)} ${esc(b.unit)})` : ""}`
    : b.link === "paralelo" ? "Al mismo tiempo que la etapa anterior, sin esperarla" : "En cola: empieza cuando termina la etapa anterior");
  const linkMark = (b) => (LINK_MARK[b.link] ? `<span class="link-mark" title="${LINK_MARK[b.link][1]}">${LINK_MARK[b.link][0]}</span>` : "");

  // ---------- panel de detalle / edición ----------
  function openPanel(b) {
    state.sel = b.id;
    $$(".gt-bar.hl").forEach((x) => x.classList.remove("hl"));
    $(`.gt-bar[data-id="${b.id}"]`)?.classList.add("hl");
    const s = Date.parse(b.start), e = Date.parse(b.end);
    const line = lineById(b.line);
    const pg = b.qty ? Math.min(100, Math.round((b.done / b.qty) * 100)) : 0;
    $("#gp-eyebrow").innerHTML = `<span class="badge ${b.status}">${esc(STATE_LABEL[b.state] || b.status_label)}</span><span>Etapa ${b.seq}</span>`;
    $("#gp-title").textContent = `${b.order} · ${b.op}`;
    $("#gp-body").innerHTML = `<dl>
        <dt>Producto</dt><dd>${esc(b.product)} <span class="muted">${esc(b.sku)}</span></dd>
        <dt>Cliente</dt><dd>${b.customer ? `<span class="tdot t-${esc(b.tier)}" style="display:inline-block;vertical-align:middle"></span> ${esc(b.customer)}` : "—"}</dd>
        <dt>Línea</dt><dd>${line ? `${esc(line.code)} · ${esc(line.name)}` : "—"}</dd>
        <dt>Inicio</dt><dd>${tFull(s)}</dd>
        <dt>Fin</dt><dd>${tFull(e)}</dd>
        <dt>Duración</dt><dd>${dur(e - s)}</dd>
        <dt>Operarios</dt><dd>${b.crew} · ${num(b.man_hours)} horas-hombre</dd>
        <dt>Avance</dt><dd>${num(b.done, 0)} / ${num(b.qty, 0)} ${esc(b.unit)} (${pg}%)<div class="gp-prog"><i style="width:${pg}%"></i></div></dd>
        <dt>Compromiso</dt><dd class="${b.late ? "late" : ""}">${b.due ? tFull(Date.parse(b.due)) : "—"}${b.late ? " · atrasado" : ""}</dd>
        <dt>Prioridad</dt><dd>${b.priority}</dd>
        ${b.changeover ? `<dt>Cambio de producto</dt><dd>${Math.round(b.changeover)} min incluidos (la línea venía de otra familia)</dd>` : ""}
        ${b.split_of || b.has_parts ? `<dt>División</dt><dd>Operación repartida entre varias líneas en paralelo</dd>` : ""}
        ${b.stage_id && !b.stage_first ? `<dt>Con la etapa anterior</dt><dd>${linkText(b)}</dd>` : ""}
      </dl>
      ${b.conflicts.length ? `<div class="callout bad">${b.conflicts.map((c) => esc(CONFLICT[c] || c)).join("<br>")}</div>` : ""}`;
    const editable = canPlan && b.state !== "done";
    $("#gp-form").hidden = !editable;
    $("#gp-err").hidden = true;
    if (editable) {
      const compatible = state.data.all_lines.filter((l) => l.kind === b.kind || !b.kind);
      $("#gp-line").innerHTML = compatible.map((l) => `<option value="${l.id}" ${l.id === b.line ? "selected" : ""}>${esc(l.label)}</option>`).join("");
      $("#gp-start").value = msToWall(s);
      $("#gp-end").value = msToWall(e);
      $("#gp-unsched").hidden = b.state === "running";
      updateDur();
    }
    const compatibleLines = state.data.all_lines.filter((l) => !b.kind || l.kind === b.kind);
    const splitOk = canPlan && b.state !== "done" && b.state !== "running" && !b.split_of && compatibleLines.length > 1;
    const mergeOk = canPlan && b.state !== "done" && (b.split_of || b.has_parts);
    $("#gp-split").hidden = !splitOk;
    $("#gp-split-form").hidden = true;
    $("#gp-merge").hidden = !mergeOk;
    const linkOk = canPlan && b.stage_id && !b.stage_first;
    $("#gp-link").hidden = !linkOk;
    $("#gp-link-form").hidden = true;
    if (linkOk) {
      $("#gp-link-sel").innerHTML = LINKS.map(([v, l]) => `<option value="${v}" ${v === (b.link || "cola") ? "selected" : ""}>${esc(l)}</option>`).join("");
      $("#gp-lag").value = b.link_lag ?? "";
      $("#gp-lag-row").hidden = (b.link || "cola") !== "flujo";
      $("#gp-link-msg").textContent = `Etapa «${b.stage_name}». Aplica a todos sus lotes.`;
    }
    if (splitOk) {
      $("#gp-split-lines").innerHTML = compatibleLines.map((l, i) => `<label class="gp-split-row"><input type="checkbox" value="${l.id}" ${l.id === b.line || i < 2 ? "checked" : ""}>
          <span>${esc(l.label)}</span><input type="number" min="0" step="any" data-qty="${l.id}"></label>`).join("");
      splitEven(b);
    }
    const day = msToWall(s).slice(0, 10);
    $("#gp-links").innerHTML = `<a class="sbtn" href="${esc(b.order_url)}"><svg class="ico sm"><use href="#i-external"/></svg> Abrir pedido</a>
      <a class="sbtn" href="${esc(root.dataset.scheduler)}?site=${state.site}&view=dia&date=${day}"><svg class="ico sm"><use href="#i-calendar"/></svg> Ver en programador</a>`;
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
    tipEl.hidden = true;
  }
  function splitEven(b) {
    const on = $$("#gp-split-lines input[type=checkbox]").filter((c) => c.checked);
    const each = on.length ? Math.floor((b.qty / on.length) * 1000) / 1000 : 0;
    $$("#gp-split-lines [data-qty]").forEach((q) => { q.value = ""; q.disabled = true; });
    on.forEach((c, i) => {
      const q = $(`#gp-split-lines [data-qty="${c.value}"]`);
      q.disabled = false;
      q.value = i === on.length - 1 ? +(b.qty - each * (on.length - 1)).toFixed(3) : each;
    });
    $("#gp-split-msg").textContent = `${on.length} partes de ${num(b.qty, 0)} ${b.unit} en total. Ajusta si quieres.`;
  }
  $("#gp-split")?.addEventListener("click", () => { $("#gp-split-form").hidden = !$("#gp-split-form").hidden; });
  $("#gp-split-lines")?.addEventListener("change", (e) => { if (e.target.type === "checkbox") splitEven(blockById(state.sel)); });
  $("#gp-split-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const b = blockById(state.sel);
    const parts = $$("#gp-split-lines input[type=checkbox]").filter((c) => c.checked)
      .map((c) => ({ line: Number(c.value), qty: Number($(`#gp-split-lines [data-qty="${c.value}"]`).value) }));
    const sum = parts.reduce((a, p) => a + (p.qty || 0), 0);
    if (parts.length < 2 || Math.abs(sum - b.qty) > 0.001) {
      $("#gp-split-msg").textContent = `Elige al menos 2 líneas y reparte exactamente ${num(b.qty, 3)} (llevas ${num(sum, 3)}).`;
      return;
    }
    try {
      await post(root.dataset.split, { operation: b.id, parts });
      toast(`${b.order} · ${b.op} dividida en ${parts.length} líneas.`);
      closePanel();
      await load();
    } catch (err) { $("#gp-split-msg").textContent = err.message; }
  });
  $("#gp-merge")?.addEventListener("click", async () => {
    const b = blockById(state.sel);
    if (!b || !confirm("¿Volver a juntar las partes en una sola operación?")) return;
    try { await post(root.dataset.merge, { operation: b.id }); toast("Partes unidas."); closePanel(); await load(); }
    catch (err) { toast(err.message, true); }
  });

  $("#gp-link")?.addEventListener("click", () => { $("#gp-link-form").hidden = !$("#gp-link-form").hidden; });
  $("#gp-link-sel")?.addEventListener("change", (e) => { $("#gp-lag-row").hidden = e.target.value !== "flujo"; });
  $("#gp-link-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const b = blockById(state.sel);
    if (!b) return;
    try {
      const d = await post(root.dataset.stageLink, { operation: b.id, link: $("#gp-link-sel").value,
        link_lag: $("#gp-lag").value, reschedule: $("#gp-resched").checked });
      toast(`«${b.stage_name}»: relación guardada${d.scheduled ? ` · ${d.scheduled} operaciones reprogramadas` : ""}.`);
      closePanel();
      await load();
    } catch (err) { $("#gp-link-msg").textContent = err.message; }
  });

  function closePanel() {
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
    state.sel = null;
    $$(".gt-bar.hl").forEach((x) => x.classList.remove("hl"));
  }
  function updateDur() {
    const a = Date.parse($("#gp-start").value), b = Date.parse($("#gp-end").value);
    $("#gp-dur").textContent = a && b ? (b > a ? `Duración: ${dur(b - a)}` : "El fin debe quedar después del inicio.") : "";
  }
  $("#gp-close").addEventListener("click", closePanel);
  $("#gp-start").addEventListener("input", updateDur);
  $("#gp-end").addEventListener("input", updateDur);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (state.drag) cancelDrag();
    else if (panel.classList.contains("open")) closePanel();
  });
  $("#gp-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const b = blockById(state.sel);
    if (!b) return;
    const start = $("#gp-start").value, end = $("#gp-end").value;
    if (!start || !end || end <= start) {
      $("#gp-err").textContent = "Revisa las fechas: el fin debe ser posterior al inicio.";
      $("#gp-err").hidden = false;
      return;
    }
    $("#gp-save").disabled = true;
    try {
      await post(root.dataset.edit, { operation: b.id, line: Number($("#gp-line").value), start, end });
      toast("Programación actualizada.");
      closePanel();
      await load();
    } catch (err) {
      $("#gp-err").textContent = err.message;
      $("#gp-err").hidden = false;
    } finally {
      $("#gp-save").disabled = false;
    }
  });
  $("#gp-unsched").addEventListener("click", async () => {
    const b = blockById(state.sel);
    if (!b || !confirm(`¿Devolver ${b.order} · ${b.op} al backlog? Quedará sin fecha.`)) return;
    try {
      await post(root.dataset.unschedule, { operation: b.id });
      toast("Operación devuelta al backlog.");
      closePanel();
      await load();
    } catch (err) {
      toast(err.message, true);
    }
  });

  // ---------- arrastrar: mover y estirar ----------
  function snap(ms) {
    const step = SNAP_MIN[state.view] * 60000;
    return Math.round(ms / step) * step;
  }
  wrap.addEventListener("pointerdown", (ev) => {
    const el = ev.target.closest(".gt-bar.can-drag");
    if (!el || ev.button !== 0 || ev.pointerType === "touch") return;
    const b = blockById(Number(el.dataset.id));
    if (!b) return;
    state.drag = { b, el, mode: ev.target.closest(".rz") ? "resize" : "move", x0: ev.clientX, y0: ev.clientY, moved: false,
      s: Date.parse(b.start), e: Date.parse(b.end), line: b.line, ok: true };
    el.setPointerCapture(ev.pointerId);
  });
  wrap.addEventListener("pointermove", (ev) => {
    const dr = state.drag;
    if (!dr) return;
    const dx = ev.clientX - dr.x0, dy = ev.clientY - dr.y0;
    if (!dr.moved && Math.hypot(dx, dy) < 5) return;
    if (!dr.moved) {
      dr.moved = true;
      dr.el.classList.add("dragging");
      tipEl.hidden = true;
      dr.ghost = document.createElement("div");
      dr.ghost.className = "gt-ghost";
      dr.ghost.innerHTML = "<span></span>";
    }
    const deltaMs = snap((dx / G.pxh) * HOUR);
    const b = dr.b;
    let s = Date.parse(b.start), e = Date.parse(b.end), line = b.line, ok = true;
    if (dr.mode === "resize") {
      e = Math.max(s + SNAP_MIN[state.view] * 60000, e + deltaMs);
    } else {
      s += deltaMs;
      e += deltaMs;
      if (state.group === "linea") {
        const under = document.elementFromPoint(ev.clientX, ev.clientY)?.closest(".gt-row[data-line], .gt-label");
        const key = under?.dataset.key;
        const row = key ? $(`.gt-row[data-key="${key}"]`) : null;
        if (row?.dataset.line) {
          line = Number(row.dataset.line);
          ok = !b.kind || row.dataset.kind === b.kind;
        }
      }
    }
    dr.s = s; dr.e = e; dr.line = line; dr.ok = ok;
    $$(".gt-row.drop-ok, .gt-row.drop-no, .gt-label.drop-ok, .gt-label.drop-no").forEach((x) => x.classList.remove("drop-ok", "drop-no"));
    const rowEl = state.group === "linea" ? $(`.gt-row[data-line="${line}"]`) : dr.el.parentElement;
    if (state.group === "linea" && dr.mode === "move") {
      rowEl?.classList.add(ok ? "drop-ok" : "drop-no");
      $(`.gt-label[data-key="l${line}"]`)?.classList.add(ok ? "drop-ok" : "drop-no");
    }
    if (rowEl && dr.ghost.parentElement !== rowEl) rowEl.appendChild(dr.ghost);
    const left = xOf(Math.max(s, G.t0)), w = Math.max(4, xOf(Math.min(e, G.t1)) - left);
    dr.ghost.style.left = `${left}px`;
    dr.ghost.style.width = `${w}px`;
    dr.ghost.style.top = rowEl === dr.el.parentElement ? dr.el.style.top : `${G.pad}px`;
    dr.ghost.classList.toggle("bad", !ok);
    const lineCode = lineById(line)?.code || "";
    dr.ghost.firstChild.textContent = ok ? `${span(s, e)} · ${lineCode}` : `${lineCode}: tipo de línea no compatible`;
  });
  function cancelDrag() {
    const dr = state.drag;
    if (!dr) return;
    dr.el.classList.remove("dragging");
    dr.ghost?.remove();
    $$(".drop-ok, .drop-no").forEach((x) => x.classList.remove("drop-ok", "drop-no"));
    state.drag = null;
  }
  wrap.addEventListener("pointerup", async () => {
    const dr = state.drag;
    if (!dr) return;
    const moved = dr.moved;
    cancelDrag();
    if (!moved) return;
    state.suppressClick = true;
    setTimeout(() => (state.suppressClick = false), 0);
    const b = dr.b;
    if (!dr.ok) { toast("Esa línea no es del tipo que requiere la operación.", true); return; }
    if (dr.s === Date.parse(b.start) && dr.e === Date.parse(b.end) && dr.line === b.line) return;
    try {
      if (dr.mode === "resize") await post(root.dataset.resize, { operation: b.id, end: msToWall(dr.e) });
      else await post(root.dataset.edit, { operation: b.id, line: dr.line, start: msToWall(dr.s), end: msToWall(dr.e) });
      toast(`${b.order} · ${b.op}: ${span(dr.s, dr.e)}`);
      await load();
    } catch (err) {
      toast(err.message, true);
      render();
    }
  });
  wrap.addEventListener("pointercancel", cancelDrag);
  wrap.addEventListener("click", (ev) => {
    if (state.suppressClick) return;
    const el = ev.target.closest(".gt-bar");
    if (!el) return;
    const b = blockById(Number(el.dataset.id));
    if (b) openPanel(b);
  });
  wrap.addEventListener("keydown", (ev) => {  // desplazamiento con teclado
    if (ev.key === "ArrowRight") wrap.scrollLeft += 80;
    if (ev.key === "ArrowLeft") wrap.scrollLeft -= 80;
  });

  // ---------- barra de herramientas ----------
  $("#gt-view").addEventListener("click", (e) => {
    const b = e.target.closest("[data-view]");
    if (!b || b.dataset.view === state.view) return;
    state.view = b.dataset.view;
    store("gt.view", state.view);
    state.first = true;
    load();
  });
  $("#gt-group").addEventListener("click", (e) => {
    const b = e.target.closest("[data-group]");
    if (!b || b.dataset.group === state.group) return;
    state.group = b.dataset.group;
    store("gt.group", state.group);
    render();
  });
  $("#gt-prev").addEventListener("click", () => { state.date = state.data?.prev || ""; load(); });
  $("#gt-next").addEventListener("click", () => { state.date = state.data?.next || ""; load(); });
  $("#gt-today").addEventListener("click", () => { state.date = state.data?.today || ""; state.scrollToNow = true; load(); });
  const zoomTo = (i) => {
    const cx = wrap.scrollLeft + (wrap.clientWidth - G.lblW) / 2;
    const ratio = ZOOMS[i] / ZOOMS[state.zoom];
    state.zoom = i;
    store("gt.zoom", String(ZOOMS[i]));
    render();
    wrap.scrollLeft = cx * ratio - (wrap.clientWidth - G.lblW) / 2;
  };
  $("#gt-zin").addEventListener("click", () => state.zoom < ZOOMS.length - 1 && zoomTo(state.zoom + 1));
  $("#gt-zout").addEventListener("click", () => state.zoom > 0 && zoomTo(state.zoom - 1));
  let qT;
  $("#gt-q").addEventListener("input", (e) => {
    clearTimeout(qT);
    qT = setTimeout(() => { state.q = e.target.value.trim().toLowerCase(); render(); }, 150);
  });
  $("#gt-site")?.addEventListener("change", (e) => { state.site = e.target.value; state.first = true; closePanel(); load(); });

  const confirmDlg = document.getElementById("gt-confirm");
  $("#gt-auto")?.addEventListener("click", () => confirmDlg.showModal());
  $("#gt-confirm-no").addEventListener("click", () => confirmDlg.close());
  $("#gt-confirm-yes").addEventListener("click", async () => {
    confirmDlg.close();
    try {
      const r = await post(root.dataset.auto, { site: Number(state.site), keep_existing: true });
      let msg = `${r.scheduled} operaciones programadas.`;
      if (r.late.length) msg += ` ${r.late.length} quedan después del compromiso.`;
      if (r.unassigned.length) msg += ` Sin línea para: ${r.unassigned.join(", ")}.`;
      toast(msg, r.late.length > 0);
      await load();
    } catch (err) {
      toast(err.message, true);
    }
  });

  let rT;
  window.addEventListener("resize", () => { clearTimeout(rT); rT = setTimeout(() => state.data && !state.drag && render(), 150); });
  setInterval(() => {
    if (!document.hidden && !state.drag && !panel.classList.contains("open")) load();
  }, 60000);

  load();
})();
