/* Programador visual de producción — sin dependencias. */
(() => {
  "use strict";
  const root = document.getElementById("sch");
  const API = root.dataset;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const csrf = () => document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
  // Sin permiso de planeación: solo consulta (sin arrastrar, estirar, asignar, editar ni desprogramar).
  const canPlan = root.dataset.canPlan !== "0";
  if (!canPlan) root.classList.add("readonly");

  const params = new URLSearchParams(location.search);
  const state = {
    site: params.get("site") || "", view: params.get("view") || "semana", date: params.get("date") || "",
    line: params.get("line") || "", data: null, tab: "backlog", q: "", prio: "all", tier: "all",
    live: true, drag: null, resizing: false, suppressClick: false, scrollPending: true, loading: false,
  };
  let slotMs = [];
  let colw = 116;

  const CONFLICT_LABEL = { solape: "solape", secuencia: "secuencia", mantenimiento: "mant.", operarios: "operarios" };
  const CONFLICT_TEXT = {
    solape: "La línea tiene más operaciones simultáneas que estaciones disponibles.",
    secuencia: "Empieza antes de que termine la operación anterior de la misma orden.",
    mantenimiento: "Se cruza con un mantenimiento programado en la línea.",
    operarios: "Las cuadrillas simultáneas superan los operarios disponibles del turno.",
  };
  const TIER_LABEL = { bronce: "Bronce", plata: "Plata", oro: "Oro", vip: "VIP" };
  const PRIO_LABEL = { 1: "Urgente", 2: "Alta", 3: "Media", 4: "Baja", 5: "Baja" };

  // ---------- formato ----------
  const tz = () => state.data?.tz;
  const fmt = (iso, opts) => (iso ? new Intl.DateTimeFormat("es", { timeZone: tz(), hour12: false, ...opts }).format(new Date(iso)) : "—");
  const fmtTime = (iso) => fmt(iso, { hour: "2-digit", minute: "2-digit" });
  const fmtDate = (iso) => fmt(iso, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  const fmtQty = (n) => new Intl.NumberFormat("es", { maximumFractionDigits: 2 }).format(n);
  const tierCls = (t) => (t ? `t-${t}` : "");
  function toInputValue(date) {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
      timeZone: tz(), year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    }).formatToParts(date).map((x) => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day}T${p.hour === "24" ? "00" : p.hour}:${p.minute}`;
  }

  // ---------- red ----------
  async function load() {
    if (state.loading) return;
    state.loading = true;
    try {
      const qs = new URLSearchParams({ view: state.view });
      if (state.site) qs.set("site", state.site);
      if (state.date) qs.set("date", state.date);
      if (state.line) qs.set("line", state.line);
      const r = await fetch(`${API.api}?${qs}`, { headers: { Accept: "application/json" } });
      const data = await r.json();
      if (!r.ok) { toast(data.error || "Error al cargar el tablero", true); return; }
      state.data = data;
      state.site = String(data.site.id);
      state.date = data.anchor;
      const url = new URLSearchParams({ site: state.site, view: state.view, date: state.date });
      if (state.line) url.set("line", state.line);
      history.replaceState(null, "", `?${url}`);
      render();
    } catch (e) {
      toast(`Sin conexión con el servidor: ${e.message}`, true);
    } finally {
      state.loading = false;
    }
  }

  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() }, body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 409 && data.frozen && !body.force) {
      if (window.confirm(data.error)) return post(url, { ...body, force: true });
      throw new Error("Cambio cancelado: el plan congelado no se tocó.");
    }
    if (!r.ok) throw new Error(data.error || `Error ${r.status}`);
    return data;
  }

  const LINKS = [["cola", "En cola: cuando termine la anterior"], ["flujo", "A la par: en flujo con la anterior"], ["paralelo", "Al mismo tiempo: independiente"]];
  const LINK_MARK = { flujo: ["⤳", "Va a la par con la etapa anterior"], paralelo: ["∥", "Va al mismo tiempo que la etapa anterior"] };
  const linkText = (b) => (b.link === "flujo" ? `A la par con la etapa anterior${b.link_lag ? ` (arranca cuando lleva ${fmtQty(b.link_lag)} ${esc(b.unit)})` : ""}`
    : b.link === "paralelo" ? "Al mismo tiempo que la etapa anterior, sin esperarla" : "En cola: empieza cuando termina la etapa anterior");
  const linkMark = (b) => (LINK_MARK[b.link] ? `<span class="link-mark" title="${LINK_MARK[b.link][1]}">${LINK_MARK[b.link][0]}</span>` : "");

  async function act(url, body, okMsg) {
    try {
      const d = await post(url, body);
      if (okMsg) toast(typeof okMsg === "function" ? okMsg(d) : okMsg);
      await load();
      return d;
    } catch (e) {
      toast(e.message, true);
      await load();
      return null;
    }
  }

  // ---------- render general ----------
  function render() {
    const d = state.data;
    $("#eyebrow").textContent = `${d.site.code} · ${d.site.name} · ${d.backlog.length} sin programar`;
    const pill = $("#running-pill");
    pill.classList.toggle("on", d.running > 0);
    $("#running-text").textContent = d.running > 0
      ? `${d.running} operaci${d.running === 1 ? "ón" : "ones"} en proceso` : "Sin producción en curso";

    const alert = $("#alert");
    alert.hidden = d.overdue.count === 0;
    if (d.overdue.count) {
      const items = d.overdue.items.map((i) => `<a href="${esc(i.url)}">${esc(i.order)}</a>${i.customer ? ` (${esc(i.customer)})` : ""}`).join(", ");
      const extra = d.overdue.count > d.overdue.items.length ? ` +${d.overdue.count - d.overdue.items.length}` : "";
      const tierTxt = d.overdue.high_tier ? `, ${d.overdue.high_tier} de clientes Oro/VIP` : "";
      $("#alert-text").innerHTML = `<b>${d.overdue.count} orden${d.overdue.count === 1 ? "" : "es"} atrasada${d.overdue.count === 1 ? "" : "s"}${tierTxt}</b> — ${items}${extra}`;
    }

    $("#range").textContent = d.range_label;
    $$("#views button").forEach((b) => b.classList.toggle("on", b.dataset.view === d.view));
    $("#site").innerHTML = d.sites.map((s) => `<option value="${s.id}" ${s.id === d.site.id ? "selected" : ""}>${esc(s.label)}</option>`).join("");
    $("#line").innerHTML = `<option value="">Todas las líneas</option>` +
      d.all_lines.map((l) => `<option value="${l.id}" ${l.id === d.line_filter ? "selected" : ""}>${esc(l.label)}</option>`).join("");
    const newOrder = $("#new-order");
    if (newOrder) newOrder.href = `${d.new_order_url}?next=${encodeURIComponent(location.pathname + location.search)}`;
    $("#bl-count").textContent = d.backlog.length;
    renderBacklog();
    renderBoard();
  }

  // ---------- backlog / cambios ----------
  function backlogFiltered() {
    const q = state.q.trim().toLowerCase();
    return state.data.backlog.filter((b) =>
      (state.prio === "all" || (state.prio === "4" ? b.priority >= 4 : b.priority === Number(state.prio))) &&
      (state.tier === "all" || b.tier === state.tier) &&
      (!q || [b.order, b.op, b.sku, b.product, b.customer, b.kind].join(" ").toLowerCase().includes(q)));
  }

  function backlogCard(b) {
    const title = `${b.order} · ${b.op}\n${b.sku} ${b.product}\nRequiere línea: ${b.kind || "cualquiera"}`;
    return `<div class="bl-card" draggable="${canPlan}" data-op="${b.id}" data-kind="${esc(b.kind)}" title="${esc(title)}">
      <div class="r1"><span class="tdot ${tierCls(b.tier)}"></span>${esc(b.order)}<span class="prio">${PRIO_LABEL[b.priority] || ""} · ${b.seq}</span></div>
      <div class="r2">${esc(b.op)} · ${esc(b.product)}</div>
      <div class="r3"><span>${fmtQty(b.qty)} ${esc(b.unit)}</span><span>${b.minutes} min</span>
        ${b.due ? `<span class="${b.late ? "late" : ""}" title="Fecha compromiso">⏱ ${fmtDate(b.due)}</span>` : ""}</div></div>`;
  }

  function renderBacklog() {
    const body = $("#bl-body");
    $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === state.tab));
    $("#filters").hidden = state.tab !== "backlog";
    if (state.tab === "changes") {
      const ch = state.data.changes;
      body.innerHTML = ch.length
        ? `<div class="changes">${ch.map((c) => `<div class="row"><span class="t">${fmtDate(c.at)}</span><span class="a">${esc(c.action)}</span><span>${esc(c.text)}</span><span class="u">${esc(c.user)}</span></div>`).join("")}</div>`
        : `<div class="sch-empty">Aún no hay cambios registrados en esta planta.</div>`;
      return;
    }
    const items = backlogFiltered();
    const tp = state.data.to_plan;
    const note = tp.count ? `<a class="tp-note" href="${esc(tp.url)}">${tp.count} pedido${tp.count === 1 ? "" : "s"} esperando planeación de etapas → Ir a Planeación</a>` : "";
    if (!state.data.backlog.length) body.innerHTML = note + `<div class="sch-empty">Backlog vacío — todas las órdenes están programadas.</div>`;
    else if (!items.length) body.innerHTML = note + `<div class="sch-empty">Ninguna operación del backlog coincide con los filtros.</div>`;
    else body.innerHTML = note + `<div class="bl-list">${items.map(backlogCard).join("")}</div>`;
  }

  // ---------- tablero ----------
  function colRange(s, e) {
    let a = -1, z = -1;
    slotMs.forEach((sl, i) => { if (sl.e > s && sl.s < e) { if (a < 0) a = i; z = i; } });
    if (a < 0) { // cae en un hueco entre turnos: al siguiente slot
      const i = slotMs.findIndex((sl) => sl.s >= s);
      if (i < 0) return null;
      a = z = i;
    }
    return [a, z];
  }

  function layoutLine(lineId) {
    const d = state.data, N = slotMs.length, items = [];
    for (const b of d.blocks) {
      if (b.line !== lineId) continue;
      const s = Date.parse(b.start), e = Date.parse(b.end), r = colRange(s, e);
      if (r) items.push({ t: "b", x: b, a: r[0], z: r[1], cl: s < slotMs[0].s, cr: e > slotMs[N - 1].e });
    }
    for (const m of d.downtimes) {
      if (m.line !== lineId) continue;
      const r = colRange(Date.parse(m.start), Date.parse(m.end));
      if (r) items.push({ t: "m", x: m, a: r[0], z: r[1] });
    }
    items.sort((p, q) => p.a - q.a || (p.t === q.t ? 0 : p.t === "b" ? -1 : 1));
    const lanes = [];
    for (const it of items) {
      let l = lanes.findIndex((end) => end < it.a);
      if (l < 0) { l = lanes.length; lanes.push(it.z); } else lanes[l] = it.z;
      it.lane = l;
    }
    return { items, lanes: Math.max(1, lanes.length) };
  }

  function blockHtml(it, compact) {
    const b = it.x;
    const conf = b.conflicts.length > 0;
    const cls = ["blk", `state-${b.state}`, conf && "conflict", compact && "compact", it.cl && "cont-left", it.cr && "cont-right"].filter(Boolean).join(" ");
    const title = [`${b.order} · ${b.op}`, `${b.sku} ${b.product} × ${fmtQty(b.qty)} ${b.unit}`,
      `Cuadrilla: ${b.crew} operario${b.crew === 1 ? "" : "s"} · ${fmtQty(b.man_hours)} horas-hombre`,
      `${fmtDate(b.start)} → ${fmtDate(b.end)}`, b.customer && `Cliente: ${b.customer} (${TIER_LABEL[b.tier] || "—"})`,
      ...b.conflicts.map((c) => `⚠ ${CONFLICT_TEXT[c]}`)].filter(Boolean).join("\n");
    const editable = canPlan && b.state !== "done";
    const warn = conf ? `<span class="warn">⚠ ${b.conflicts.map((c) => CONFLICT_LABEL[c]).join(", ")}</span>` : "";
    return `<div class="${cls}" draggable="${editable}" data-op="${b.id}" data-kind="${esc(b.kind)}" data-a="${it.a}"
        style="grid-column:${it.a + 1} / ${it.z + 2}; grid-row:${it.lane + 1}" title="${esc(title)}">
      <div class="r1"><span class="tdot ${tierCls(b.tier)}"></span><span class="no">${esc(b.order)}</span>${b.late ? '<span class="late-mark" title="Atrasada">!</span>' : ""}${b.split_of || b.has_parts ? '<span class="part-mark" title="Operación dividida entre líneas">⇉</span>' : ""}${linkMark(b)}${b.changeover ? `<span class="co-mark" title="Incluye ${Math.round(b.changeover)} min de cambio de producto">↻</span>` : ""}</div>
      <div class="r2"><span class="p">${b.crew > 1 ? `${b.crew} op. · ` : ""}${esc(b.op)} · ${esc(b.product)}</span>${warn}</div>
      ${editable && b.state !== "running" ? '<button class="x" title="Devolver al backlog">×</button>' : ""}
      ${editable ? '<span class="rz" title="Arrastra para cambiar el fin"></span>' : ""}
    </div>`;
  }

  // Vista mensual: un resumen por línea y día (nº de operaciones, carga aproximada, conflictos, paros).
  function monthSummaries(line) {
    const d = state.data;
    let h = "";
    slotMs.forEach((sl, i) => {
      const ops = d.blocks.filter((b) => b.line === line.id && Date.parse(b.start) < sl.e && Date.parse(b.end) > sl.s);
      const dts = d.downtimes.filter((m) => m.line === line.id && Date.parse(m.start) < sl.e && Date.parse(m.end) > sl.s);
      if (!ops.length && !dts.length) return;
      const busy = ops.reduce((acc, b) => acc + Math.min(Date.parse(b.end), sl.e) - Math.max(Date.parse(b.start), sl.s), 0);
      const load = Math.round((busy * 100) / ((sl.e - sl.s) * line.capacity));
      const conflict = ops.some((b) => b.conflicts.length);
      const running = ops.some((b) => b.state === "running");
      const title = [`${ops.length} operación(es) · carga aprox. ${load}%`, ...ops.slice(0, 8).map((b) => `${b.order} · ${b.op}`),
        ops.length > 8 ? `… y ${ops.length - 8} más` : "", ...dts.map((m) => `Mantenimiento: ${m.reason}`)].filter(Boolean).join("\n");
      h += `<div class="mday ${conflict ? "conflict" : ""} ${running ? "running" : ""} ${load > 85 ? "hi" : ""}" data-day="${d.days[i].date}"
          style="grid-column:${i + 1}; grid-row:1" title="${esc(title)}">
          ${dts.length ? '<span class="mt"></span>' : ""}<b>${ops.length || "—"}</b><small>op</small>
          <span class="lb"><span style="width:${Math.min(100, load)}%"></span></span></div>`;
    });
    return h;
  }

  function renderBoard() {
    const d = state.data, wrap = $("#board-wrap");
    slotMs = d.slots.map((s) => ({ s: Date.parse(s.start), e: Date.parse(s.end) }));
    const N = slotMs.length;
    const labelW = window.innerWidth < 700 ? 150 : 214;
    colw = Math.max(d.view === "mes" ? 46 : 116, Math.floor((wrap.clientWidth - labelW - 2) / N));
    const compact = colw < 100;
    const dayStarts = new Set(d.days.map((x) => x.col));
    const now = Date.parse(d.now);

    const month = d.view === "mes";
    let h = `<div class="sch-board ${month ? "month" : ""}" style="--cols:${N};--colw:${colw}px">`;
    h += `<div class="sch-corner" style="grid-row: span ${d.view === "mes" ? 1 : 2}">LÍNEA · CARGA</div>`;
    const hol = Object.fromEntries((d.holidays || []).map((x) => [x.date, x.name]));
    h += `<div class="sch-cols hdr hdr-days">${d.days.map((x) => `<div class="sch-day ${x.today ? "today" : ""} ${hol[x.date] ? "holiday" : ""}" style="grid-column:${x.col + 1} / span ${x.span}"${hol[x.date] ? ` title="Festivo · ${esc(hol[x.date])}"` : ""}>
        <span class="dow">${x.dow}</span><span class="num">${d.view === "mes" ? x.day : `${x.day} ${x.month}`}</span></div>`).join("")}</div>`;
    if (d.view !== "mes") {
      h += `<div class="sch-cols hdr hdr-shifts">${d.slots.map((s) => `<div class="sch-shift ${s.today ? "today" : ""} ${s.off ? "off" : ""} ${s.extra ? "extra" : ""}"
          title="${esc(s.label)} ${fmtTime(s.start)}–${fmtTime(s.end)}${s.off ? ` · ${esc(s.off)}` : ""}${s.note ? ` · ${esc(s.note)}` : ""}">
          <i style="background:${esc(s.color || "#999")}"></i><span>${esc(s.label)}${d.view === "dia" ? ` · ${fmtTime(s.start)}–${fmtTime(s.end)}` : ""}</span>
          ${s.off ? `<em class="off-tag">${esc(s.off.split(" · ")[0])}</em>` : s.note ? `<em class="ot-tag">${esc(s.note)}</em>` : ""}</div>`).join("")}</div>`;
    }

    if (d.operators) {
      const total = d.operators.total ? ` · total ${d.operators.total}` : "";
      h += `<div class="sch-linecell sch-oplabel"><div class="code"><i class="op"></i>OPERARIOS</div>
          <div class="name">libres / disponibles${total}</div></div>`;
      h += `<div class="sch-cols sch-oprow">${d.operators.slots.map((o, i) => {
        const cls = `${d.slots[i].today ? "today" : ""} ${dayStarts.has(i) ? "daystart" : ""}`;
        if (!o) return `<div class="opc off ${cls}" title="Fuera de turno"></div>`;
        const pct = o.available ? Math.max(0, Math.min(100, (o.used * 100) / o.available)) : 0;
        const state = o.free < 0 ? "over" : o.free <= Math.max(1, o.available * 0.1) ? "hi" : "";
        const tip = `Disponibles ${o.available} · en uso hasta ${o.used} · libres mínimo ${o.free}`;
        return `<div class="opc ${state} ${cls}" title="${tip}"><span class="ov"><b>${o.free}</b>${compact ? "" : `<small>/${o.available}</small>`}</span>
          <span class="ob"><span style="width:${pct}%"></span></span></div>`;
      }).join("")}</div>`;
    }

    if (!d.lines.length) {
      h += `<div class="sch-linecell"></div><div class="sch-empty" style="padding:24px">Esta planta no tiene líneas activas. Créalas en Administración → Centros de trabajo.</div>`;
    }
    let nowLeft = null;
    const ni = slotMs.findIndex((s) => now >= s.s && now < s.e);
    if (ni >= 0) nowLeft = ni * colw + ((now - slotMs[ni].s) / (slotMs[ni].e - slotMs[ni].s)) * colw;

    for (const line of d.lines) {
      const { items, lanes } = month ? { items: [], lanes: 1 } : layoutLine(line.id);
      const barCls = line.load > 100 ? "over" : line.load > 85 ? "hi" : "";
      h += `<div class="sch-linecell" data-line="${line.id}" data-kind="${esc(line.kind)}">
          <div class="code"><i class="${line.running ? "" : "idle"}" title="${line.running ? "Produciendo" : "Sin operación en proceso"}"></i>${esc(line.code)}</div>
          <div class="name" title="${esc(line.name)} · tipo ${esc(line.kind)} · ${line.capacity} estación(es)">${esc(line.name)}</div>
          <div class="load"><div class="bar ${barCls}"><span style="width:${Math.min(100, line.load)}%"></span></div><span class="pct">${line.load}%</span></div>
        </div>`;
      h += `<div class="sch-track" data-line="${line.id}" data-kind="${esc(line.kind)}" style="grid-template-rows: repeat(${lanes}, ${month ? 64 : 58}px) minmax(6px, 1fr)">`;
      h += d.slots.map((s, i) => `<div class="sch-cell ${s.today ? "today" : ""} ${dayStarts.has(i) ? "daystart" : ""} ${s.off ? "off" : ""} ${s.extra ? "extra" : ""}" data-col="${i}" style="grid-column:${i + 1}"${s.off ? ` title="${esc(s.off)}"` : ""}></div>`).join("");
      if (month) h += monthSummaries(line);
      for (const it of items) {
        if (it.t === "b") h += blockHtml(it, compact);
        else h += `<div class="mpill" data-dt="${it.x.id}" style="grid-column:${it.a + 1} / ${it.z + 2}; grid-row:${it.lane + 1}"
              title="${esc(`${it.x.reason}\n${fmtDate(it.x.start)} → ${fmtDate(it.x.end)}`)}"><span>${esc(it.x.reason)}</span></div>`;
      }
      if (nowLeft !== null) h += `<div class="sch-now" style="left:${nowLeft}px"></div>`;
      h += `</div>`;
    }
    h += `</div>`;
    const keepLeft = wrap.scrollLeft, keepTop = wrap.scrollTop;
    wrap.innerHTML = h;
    if (state.scrollPending) {
      state.scrollPending = false;
      const today = d.days.find((x) => x.today);
      wrap.scrollLeft = today ? Math.max(0, today.col * colw - colw) : 0;
      wrap.scrollTop = 0;
    } else {
      wrap.scrollLeft = keepLeft;
      wrap.scrollTop = keepTop;
    }
  }

  // ---------- arrastrar y soltar ----------
  const opById = (id) => state.data.blocks.find((b) => b.id === id) || state.data.backlog.find((b) => b.id === id);
  const compatible = (el) => !state.drag?.kind || el.dataset.kind === state.drag.kind;
  const colAt = (track, x) => {
    const r = track.getBoundingClientRect();
    return Math.max(0, Math.min(slotMs.length - 1, Math.floor((x - r.left) / colw)));
  };
  const clearDrop = () => $$(".sch-cell.drop").forEach((c) => c.classList.remove("drop"));

  function onDragStart(e) {
    const el = e.target.closest?.(".blk, .bl-card");
    if (!el || el.getAttribute("draggable") !== "true" || state.resizing) return;
    state.drag = { op: Number(el.dataset.op), kind: el.dataset.kind, fromBoard: el.classList.contains("blk") };
    e.dataTransfer.setData("text/plain", el.dataset.op);
    e.dataTransfer.effectAllowed = "move";
    requestAnimationFrame(() => el.classList.add("dragging"));
    $$(".sch-track, .sch-linecell").forEach((t) => t.classList.toggle("incompatible", !compatible(t)));
  }
  function onDragEnd() {
    state.drag = null;
    clearDrop();
    $$(".dragging, .incompatible").forEach((x) => x.classList.remove("dragging", "incompatible"));
    $("#bl-body").classList.remove("drop-hover");
  }
  document.addEventListener("dragstart", onDragStart);
  document.addEventListener("dragend", onDragEnd);

  const wrap = $("#board-wrap");
  wrap.addEventListener("dragover", (e) => {
    const track = e.target.closest(".sch-track");
    if (!track || !state.drag || !compatible(track)) return;
    e.preventDefault();
    const col = colAt(track, e.clientX);
    const cell = track.querySelector(`.sch-cell[data-col="${col}"]`);
    if (!cell.classList.contains("drop")) { clearDrop(); cell.classList.add("drop"); }
  });
  wrap.addEventListener("dragleave", (e) => { if (!wrap.contains(e.relatedTarget)) clearDrop(); });
  wrap.addEventListener("drop", (e) => {
    const track = e.target.closest(".sch-track");
    if (!track || !state.drag) return;
    e.preventDefault();
    const { op } = state.drag;
    placeAt(op, Number(track.dataset.line), colAt(track, e.clientX));
    onDragEnd();
  });

  function placeAt(op, line, col) {
    const s = state.data.slots[col];
    const l = state.data.lines.find((x) => x.id === line);
    const where = `${l ? l.code : ""} · ${fmt(s.start, { weekday: "short", day: "2-digit" })}${state.data.view !== "mes" ? ` ${s.label}` : ""}`;
    return act(API.place, { operation: op, line, slot_start: s.start, slot_end: s.end }, `Programada en ${where}`);
  }

  const blBody = $("#bl-body");
  blBody.addEventListener("dragover", (e) => { if (state.drag?.fromBoard) { e.preventDefault(); blBody.classList.add("drop-hover"); } });
  blBody.addEventListener("dragleave", (e) => { if (!blBody.contains(e.relatedTarget)) blBody.classList.remove("drop-hover"); });
  blBody.addEventListener("drop", (e) => {
    if (!state.drag?.fromBoard) return;
    e.preventDefault();
    const op = state.drag.op;
    onDragEnd();
    act(API.unschedule, { operation: op }, "Devuelta al backlog");
  });

  // Estirar por el borde derecho
  wrap.addEventListener("pointerdown", (e) => {
    const rz = e.target.closest(".rz");
    if (!rz) return;
    e.preventDefault();
    e.stopPropagation();
    const blk = rz.closest(".blk"), track = blk.closest(".sch-track");
    const a = Number(blk.dataset.a);
    let col = null;
    state.resizing = true;
    blk.setAttribute("draggable", "false");
    const move = (ev) => {
      col = Math.max(a, colAt(track, ev.clientX));
      blk.style.gridColumn = `${a + 1} / ${col + 2}`;
    };
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      state.resizing = false;
      state.suppressClick = true;
      setTimeout(() => (state.suppressClick = false), 0);
      if (col === null) { blk.setAttribute("draggable", "true"); return; }
      act(API.resize, { operation: Number(blk.dataset.op), end: state.data.slots[col].end }, "Duración ajustada");
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
  });

  // Clics en el tablero
  wrap.addEventListener("click", (e) => {
    if (state.suppressClick) return;
    const x = canPlan && e.target.closest(".x");
    if (x) { e.stopPropagation(); act(API.unschedule, { operation: Number(x.closest(".blk").dataset.op) }, "Devuelta al backlog"); return; }
    const blk = e.target.closest(".blk");
    if (blk) return openEditor(opById(Number(blk.dataset.op)));
    const mday = e.target.closest(".mday");
    if (mday) return navigate({ view: "dia", date: mday.dataset.day });
    const pill = e.target.closest(".mpill");
    if (pill) return showDowntime(state.data.downtimes.find((m) => m.id === Number(pill.dataset.dt)));
    const cell = canPlan && e.target.closest(".sch-cell");
    if (cell) return assignDialog(Number(cell.closest(".sch-track").dataset.line), Number(cell.dataset.col));
  });

  // ---------- diálogos ----------
  const dlg = $("#dlg");
  dlg.addEventListener("click", (e) => { if (e.target === dlg || e.target.closest("[data-close]")) dlg.close(); });
  function openDialog(title, body, buttons = []) {
    $("#dlg-title").textContent = title;
    $("#dlg-body").innerHTML = body;
    const foot = $("#dlg-foot");
    foot.innerHTML = "";
    for (const b of [...buttons, { label: "Cerrar", cls: "", close: true }]) {
      const el = document.createElement(b.href ? "a" : "button");
      el.className = `sbtn ${b.cls || ""}`;
      el.textContent = b.label;
      if (b.href) el.href = b.href;
      el.addEventListener("click", async () => { if (b.onClick) { const keep = await b.onClick(); if (keep) return; } if (!b.href) dlg.close(); });
      foot.appendChild(el);
    }
    if (!dlg.open) dlg.showModal();
  }

  function lineLabel(id) {
    const l = state.data.lines.find((x) => x.id === id) || state.data.all_lines.find((x) => x.id === id);
    return l ? (l.code ? `${l.code} · ${l.name}` : l.label) : "—";
  }

  // ---------- panel de edición (clic en una tarjeta) ----------
  // Fechas "de pared" (YYYY-MM-DD + HH:MM) en la zona horaria de la planta <-> instantes (ms).
  function tzParts(ms) {
    return Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
      timeZone: tz(), year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  }
  function msToWall(ms) {
    const p = tzParts(ms);
    return { date: `${p.year}-${p.month}-${p.day}`, time: `${p.hour === "24" ? "00" : p.hour}:${p.minute}` };
  }
  function tzOffset(ms) {
    const p = tzParts(ms);
    return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) - Math.floor(ms / 1000) * 1000;
  }
  function wallToMs(date, time) {
    if (!date || !time) return NaN;
    const [Y, M, D] = date.split("-").map(Number), [h, m] = time.split(":").map(Number);
    const guess = Date.UTC(Y, M - 1, D, h, m);
    return guess - tzOffset(guess - tzOffset(guess));
  }
  const addDays = (date, n) => {
    const [Y, M, D] = date.split("-").map(Number);
    return new Date(Date.UTC(Y, M - 1, D + n)).toISOString().slice(0, 10);
  };
  // Un turno que cruza medianoche pertenece al día en que termina (igual que las columnas del tablero).
  const shiftWindow = (sh, date) => (sh.end === "00:00"  // termina a medianoche: es del día en que empieza
    ? [wallToMs(date, sh.start), wallToMs(addDays(date, 1), "00:00")]
    : [wallToMs(sh.end <= sh.start ? addDays(date, -1) : date, sh.start), wallToMs(date, sh.end)]);
  function lineShifts(line) {
    const all = state.data.shifts;
    return line && line.shifts.length ? all.filter((s) => line.shifts.includes(s.id)) : all;
  }
  function shiftAt(ms, shifts) {
    const base = msToWall(ms).date;
    for (const date of [base, addDays(base, 1)]) {
      for (const sh of shifts) {
        const [a, b] = shiftWindow(sh, date);
        if (ms >= a && ms < b) return { sh, date };
      }
    }
    return null;
  }
  function fmtDur(ms) {
    if (!(ms > 0)) return "—";
    const mins = Math.round(ms / 60000), d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60), m = mins % 60;
    return [d && `${d}d`, h && `${h}h`, m && !d && `${m}m`].filter(Boolean).join(" ") || "0m";
  }
  const UNIT_TEXT = { UN: "unidades", KG: "kg", CJ: "cajas" };
  const STATE_TEXT = { planned: "Planificada", released: "Liberada", running: "En proceso", done: "Terminada", backlog: "Sin programar" };

  const edlg = $("#edit-dlg");
  const ed = { op: null, dueTime: "23:59", dueOrig: "" };

  function availability(lineId, ms, atEnd) {
    const d = state.data, line = d.all_lines.find((l) => l.id === lineId);
    if (!line || Number.isNaN(ms)) return ["na", ""];
    const t = atEnd ? ms - 1 : ms;
    const shifts = lineShifts(line);
    if (shifts.length && !shiftAt(t, shifts)) return ["off", "Fuera de turno"];
    if (d.downtimes.some((m) => m.line === lineId && Date.parse(m.start) <= t && Date.parse(m.end) > t)) return ["maint", "Mantenimiento"];
    if (!d.lines.some((l) => l.id === lineId) || t < slotMs[0].s || t >= slotMs[slotMs.length - 1].e) return ["na", "Sin datos en pantalla"];
    const n = d.blocks.filter((b) => b.line === lineId && b.id !== ed.op.id && b.state !== "done" &&
      Date.parse(b.start) <= t && Date.parse(b.end) > t).length;
    if (n >= line.capacity) return ["busy", `Ocupado (${n}/${line.capacity})`];
    return ["free", line.capacity > 1 ? `¡Libre! (${n}/${line.capacity})` : "¡Libre!"];
  }

  const edStart = () => wallToMs($("#ed-sd").value, $("#ed-st").value);
  const edEnd = () => wallToMs($("#ed-ed").value, $("#ed-et").value);
  function setWall(prefix, ms) {
    const w = msToWall(ms);
    $(`#ed-${prefix}d`).value = w.date;
    $(`#ed-${prefix}t`).value = w.time;
  }
  function setAv(id, [cls, text]) {
    const el = $(id);
    el.className = `ed-av ${cls}`;
    el.textContent = text;
  }

  function fillShiftSelects() {
    const line = state.data.all_lines.find((l) => l.id === Number($("#ed-line").value));
    const opts = `<option value="">—</option>` + lineShifts(line).map((s) => `<option value="${s.id}">${esc(s.name)} · ${s.start}–${s.end}</option>`).join("");
    $("#ed-sshift").innerHTML = opts;
    $("#ed-eshift").innerHTML = opts;
  }

  function refreshEditor() {
    const line = Number($("#ed-line").value), s = edStart(), e = edEnd();
    const shifts = lineShifts(state.data.all_lines.find((l) => l.id === line));
    setAv("#ed-av-s", availability(line, s, false));
    setAv("#ed-av-e", availability(line, e, true));
    const ss = Number.isNaN(s) ? null : shiftAt(s, shifts), es = Number.isNaN(e) ? null : shiftAt(e - 1, shifts);
    $("#ed-sshift").value = ss ? String(ss.sh.id) : "";
    $("#ed-eshift").value = es ? String(es.sh.id) : "";
    const dur = e - s;
    $("#ed-dur").textContent = dur <= 0 ? "El fin debe ser posterior al inicio"
      : `Ventana ${fmtDur(dur)} · trabajo estándar ${fmtDur(ed.op.minutes * 60000)}`;
    const dueDate = $("#ed-due").value;
    if (dueDate && !Number.isNaN(e)) {
      const slack = wallToMs(dueDate, ed.dueTime) - e;
      setAv("#ed-av-due", slack >= 0 ? ["free", "A tiempo"] : ["late", "Atrasada"]);
      $("#ed-slack").textContent = slack >= 0 ? `Holgura ${fmtDur(slack)} (esta operación)` : `Esta operación termina ${fmtDur(-slack)} tarde`;
    } else {
      setAv("#ed-av-due", ["na", ""]);
      $("#ed-slack").textContent = dueDate ? "" : "Sin fecha compromiso";
    }
    $("#ed-save").disabled = ed.op.state === "done" || !(dur > 0);
  }

  function openEditor(b) {
    if (!b) return;
    const d = state.data;
    ed.op = b;
    const scheduled = Boolean(b.start);
    const stateKey = scheduled ? b.state : "backlog";
    $("#ed-no").textContent = b.order;
    const badge = $("#ed-badge");
    badge.className = `ed-badge ${stateKey}`;
    badge.textContent = STATE_TEXT[stateKey];
    $("#ed-title").textContent = `${b.product} · ${fmtQty(b.qty)} ${UNIT_TEXT[b.unit] || b.unit}`;
    $("#ed-sub").innerHTML = `<span>${esc(b.op)}</span><span>·</span><span>${esc(b.sku)}</span>` +
      `<span>·</span><span>${b.crew} operario${b.crew === 1 ? "" : "s"} · ${fmtQty(b.man_hours)} HH</span>` +
      (b.customer ? `<span>·</span><span class="tdot ${tierCls(b.tier)}"></span><span>${esc(b.customer)} (${TIER_LABEL[b.tier]})</span>` : "") +
      (b.stage_id && !b.stage_first && b.link && b.link !== "cola" ? `<span>·</span><span>${linkText(b)}</span>` : "");
    const conf = $("#ed-conf");
    conf.hidden = !b.conflicts?.length;
    conf.innerHTML = (b.conflicts || []).map((c) => `⚠ ${CONFLICT_TEXT[c]}`).join("<br>");
    $("#ed-tz").textContent = `Zona horaria ${d.tz}`;
    $("#ed-err").hidden = true;

    const lines = d.all_lines.filter((l) => !b.kind || l.kind === b.kind);
    $("#ed-line").innerHTML = lines.length
      ? lines.map((l) => `<option value="${l.id}">${esc(l.code)} · ${esc(l.name)}</option>`).join("")
      : `<option value="">No hay líneas de tipo «${esc(b.kind)}»</option>`;
    $("#ed-line").value = String(b.line && lines.some((l) => l.id === b.line) ? b.line : lines[0]?.id ?? "");
    fillShiftSelects();

    let s = scheduled ? Date.parse(b.start) : Math.ceil(Date.now() / 3600000) * 3600000;
    let e = scheduled ? Date.parse(b.end) : s + Math.max(1, b.minutes) * 60000;
    setWall("s", s);
    setWall("e", e);

    const due = b.due ? msToWall(Date.parse(b.due)) : null;
    ed.dueTime = due ? due.time : "23:59";
    ed.dueOrig = due ? due.date : "";
    $("#ed-due").value = ed.dueOrig;
    $("#ed-prio").value = String(b.priority);

    const route = d.routes[b.order_id] || [];
    $("#ed-route").innerHTML = route.map((r) => `<button type="button" class="rchip ${r.id === b.id ? "cur" : ""} ${r.status === "terminada" ? "done" : ""}"
        data-op="${r.id}" title="${esc(r.name)}${r.start ? ` · ${fmtDate(r.start)}` : " · sin programar"}">
        ${esc(r.line || "—")} <small>${r.start ? msToWall(Date.parse(r.start)).date : "backlog"}</small></button>`).join("") || `<span class="ed-hint">—</span>`;

    const done = b.state === "done";
    $$("#ed-form input, #ed-form select").forEach((el) => (el.disabled = !canPlan || done || !lines.length));
    $("#ed-save span").textContent = scheduled ? "Guardar" : "Programar";
    $("#ed-save").hidden = !canPlan;
    $("#ed-ro").hidden = canPlan;
    $("#ed-unsched").hidden = !canPlan || !scheduled || b.state === "running" || done;
    const compatible = state.data.all_lines.filter((l) => !b.kind || l.kind === b.kind);
    $("#ed-split").hidden = !canPlan || done || b.state === "running" || compatible.length < 2 || Boolean(b.split_of);
    $("#ed-merge").hidden = !canPlan || !(b.split_of || b.has_parts) || done;
    $("#ed-link").hidden = !canPlan || !b.stage_id || b.stage_first;
    $("#ed-open").href = b.order_url;
    refreshEditor();
    if (!edlg.open) edlg.showModal();
  }

  // Al cambiar el inicio se conserva la duración (se mueve el bloque completo).
  let lastStart = NaN;
  ["#ed-sd", "#ed-st"].forEach((sel) => {
    $(sel).addEventListener("focus", () => (lastStart = edStart()));
    $(sel).addEventListener("change", () => {
      const s = edStart(), e = edEnd();
      if (!Number.isNaN(lastStart) && !Number.isNaN(s) && !Number.isNaN(e)) setWall("e", s + (e - lastStart));
      lastStart = s;
      refreshEditor();
    });
  });
  ["#ed-ed", "#ed-et", "#ed-due"].forEach((sel) => $(sel).addEventListener("change", refreshEditor));
  $("#ed-line").addEventListener("change", () => { fillShiftSelects(); refreshEditor(); });
  $("#ed-sshift").addEventListener("change", (ev) => {
    const sh = state.data.shifts.find((x) => String(x.id) === ev.target.value);
    if (!sh) return;
    const dur = edEnd() - edStart();
    const [a] = shiftWindow(sh, $("#ed-sd").value);
    setWall("s", a);
    if (dur > 0) setWall("e", a + dur);
    refreshEditor();
  });
  $("#ed-eshift").addEventListener("change", (ev) => {
    const sh = state.data.shifts.find((x) => String(x.id) === ev.target.value);
    if (!sh) return;
    const [, b] = shiftWindow(sh, $("#ed-ed").value);
    setWall("e", b);
    refreshEditor();
  });
  $("#ed-route").addEventListener("click", (ev) => {
    const chip = ev.target.closest(".rchip:not(.cur)");
    if (!chip) return;
    const other = opById(Number(chip.dataset.op));
    if (other) openEditor(other);
    else toast("Esa operación está fuera de la ventana visible; navega a su fecha.", true);
  });
  edlg.addEventListener("click", (ev) => { if (ev.target === edlg || ev.target.closest("[data-ed-close]")) edlg.close(); });
  $("#ed-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!canPlan) return;
    const b = ed.op, err = $("#ed-err");
    const body = {
      operation: b.id, line: Number($("#ed-line").value),
      start: `${$("#ed-sd").value}T${$("#ed-st").value}`, end: `${$("#ed-ed").value}T${$("#ed-et").value}`,
      priority: Number($("#ed-prio").value),
    };
    if ($("#ed-due").value && $("#ed-due").value !== ed.dueOrig) body.due_date = $("#ed-due").value;
    $("#ed-save").disabled = true;
    try {
      await post(API.edit, body);
      edlg.close();
      toast(b.start ? `${b.order} · ${b.op} actualizada` : `${b.order} · ${b.op} programada`);
      await load();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
      $("#ed-save").disabled = false;
    }
  });
  $("#ed-link").addEventListener("click", () => {
    const b = ed.op;
    edlg.close();
    openDialog(`Relación de «${b.stage_name}» con la etapa anterior`, `<p class="ed-hint">${esc(b.order)} · aplica a todos los lotes de la etapa.
        Hoy: ${linkText(b)}.</p>
      <div class="link-form">
        ${LINKS.map(([v, l]) => `<label class="link-opt"><input type="radio" name="lk" value="${v}" ${v === (b.link || "cola") ? "checked" : ""}>
          <span><b>${esc(l.split(":")[0])}</b><small>${esc(l.split(":")[1] || "")}</small></span></label>`).join("")}
        <label class="link-lag" id="lk-lag-row" ${(b.link || "cola") === "flujo" ? "" : "hidden"}>Arranca cuando la anterior lleve
          <input type="number" id="lk-lag" min="0" step="any" placeholder="1" value="${b.link_lag ?? ""}"> ${esc(b.unit)}</label>
        <label class="link-re"><input type="checkbox" id="lk-re" checked> Reprogramar la orden con la nueva relación</label>
      </div>`, [{ label: "Guardar relación", cls: "accent", onClick: async () => {
        const link = $('#dlg-body input[name="lk"]:checked').value;
        await act(API.stageLink, { operation: b.id, link, link_lag: $("#lk-lag").value, reschedule: $("#lk-re").checked },
          (d) => `«${b.stage_name}»: relación guardada${d.scheduled ? ` · ${d.scheduled} operaciones reprogramadas` : ""}`);
        return false;
      } }]);
    $$('#dlg-body input[name="lk"]').forEach((r) => r.addEventListener("change", () => { $("#lk-lag-row").hidden = r.value !== "flujo" || !r.checked; }));
  });
  $("#ed-split").addEventListener("click", () => {
    const b = ed.op;
    const lines = state.data.all_lines.filter((l) => !b.kind || l.kind === b.kind);
    const total = b.qty;
    const rows = lines.map((l, i) => `<label class="split-row"><input type="checkbox" data-line="${l.id}" ${l.id === b.line || i < 2 ? "checked" : ""}>
        <span>${esc(l.label)}</span><input type="number" min="0" step="any" data-qty="${l.id}" value=""></label>`).join("");
    edlg.close();
    openDialog(`Dividir ${b.order} · ${b.op}`, `<p class="ed-hint">Reparte ${fmtQty(total)} ${esc(b.unit)} entre líneas del mismo tipo que trabajan en paralelo.
        Cada parte se programa en el primer hueco libre de su línea.</p><div class="split" id="split">${rows}</div>
        <p class="ed-hint" id="split-sum"></p>`, [{ label: "Dividir", cls: "accent", onClick: async () => {
        const parts = $$("#split [data-line]").filter((c) => c.checked).map((c) => ({ line: Number(c.dataset.line), qty: Number($(`#split [data-qty="${c.dataset.line}"]`).value) }));
        const sum = parts.reduce((a, p) => a + (p.qty || 0), 0);
        if (parts.length < 2 || Math.abs(sum - total) > 0.001) { $("#split-sum").textContent = `Elige al menos 2 líneas y reparte exactamente ${fmtQty(total)} (llevas ${fmtQty(sum)}).`; return true; }
        await act(API.split, { operation: b.id, parts }, `${b.order} · ${b.op} dividida en ${parts.length} líneas`);
        return false;
      } }]);
    const even = () => {
      const on = $$("#split [data-line]").filter((c) => c.checked);
      const each = on.length ? Math.floor((total / on.length) * 1000) / 1000 : 0;
      $$("#split [data-qty]").forEach((q) => { q.value = ""; q.disabled = true; });
      on.forEach((c, i) => { const q = $(`#split [data-qty="${c.dataset.line}"]`); q.disabled = false; q.value = i === on.length - 1 ? +(total - each * (on.length - 1)).toFixed(3) : each; });
      $("#split-sum").textContent = `Partes iguales entre ${on.length} líneas. Puedes ajustar las cantidades.`;
    };
    $$("#split [data-line]").forEach((c) => c.addEventListener("change", even));
    even();
  });
  $("#ed-merge").addEventListener("click", async () => {
    const b = ed.op;
    if (!window.confirm(`¿Volver a juntar las partes de ${b.order} · ${b.op.split(" · parte ")[0]} en una sola operación?`)) return;
    edlg.close();
    await act(API.merge, { operation: b.id }, "Partes unidas en una sola operación");
  });
  $("#ed-unsched").addEventListener("click", async () => {
    const b = ed.op;
    edlg.close();
    await act(API.unschedule, { operation: b.id }, `${b.order} · ${b.op} devuelta al backlog`);
  });

  function assignDialog(lineId, col) {
    const line = state.data.lines.find((l) => l.id === lineId);
    const slot = state.data.slots[col];
    const items = state.data.backlog.filter((b) => !b.kind || b.kind === line.kind);
    const title = `Asignar a ${line.code} · ${fmt(slot.start, { weekday: "short", day: "2-digit", month: "short" })}${state.data.view !== "mes" ? ` · ${slot.label}` : ""}`;
    const body = items.length
      ? `<div class="pick">${items.map((b) => `<button data-op="${b.id}"><b>${esc(b.order)}</b> · ${esc(b.op)}<br>
           <small>${esc(b.product)} · ${fmtQty(b.qty)} ${esc(b.unit)} · ${b.minutes} min${b.customer ? ` · ${esc(b.customer)}` : ""}</small></button>`).join("")}</div>`
      : `<p class="sch-empty">No hay operaciones en el backlog que requieran una línea de tipo «${esc(line.kind)}».</p>`;
    openDialog(title, body);
    $$("#dlg-body .pick button").forEach((btn) => btn.addEventListener("click", () => {
      dlg.close();
      placeAt(Number(btn.dataset.op), lineId, col);
    }));
  }

  function showDowntime(m) {
    if (!m) return;
    openDialog(`Mantenimiento · ${lineLabel(m.line)}`, `<dl><dt>Motivo</dt><dd>${esc(m.reason)}</dd>
        <dt>Desde</dt><dd>${fmtDate(m.start)}</dd><dt>Hasta</dt><dd>${fmtDate(m.end)}</dd></dl>`, canPlan ? [
      { label: "Eliminar", cls: "", onClick: () => act(API.downtimeDelete.replace("/0/", `/${m.id}/`), {}, "Mantenimiento eliminado") },
    ] : []);
  }

  function maintenanceDialog() {
    const lines = state.data.all_lines;
    if (!lines.length) return toast("La planta no tiene líneas.", true);
    const start = new Date(Math.ceil(Date.now() / 3600000) * 3600000);
    const end = new Date(start.getTime() + 2 * 3600000);
    openDialog("Programar mantenimiento", `
      <label>Línea<select id="m-line">${lines.map((l) => `<option value="${l.id}">${esc(l.label)}</option>`).join("")}</select></label>
      <label>Motivo<input id="m-reason" placeholder="Limpieza profunda, sanitización, cambio de cuchillas…" maxlength="120"></label>
      <label>Desde<input id="m-start" type="datetime-local" value="${toInputValue(start)}"></label>
      <label>Hasta<input id="m-end" type="datetime-local" value="${toInputValue(end)}"></label>`, [
      {
        label: "Guardar", cls: "dark", onClick: async () => {
          const r = await act(API.downtime, {
            line: Number($("#m-line").value), reason: $("#m-reason").value,
            start: $("#m-start").value, end: $("#m-end").value,
          }, "Mantenimiento programado");
          return !r; // si falló, mantener abierto
        },
      },
    ]);
  }

  blBody.addEventListener("click", (e) => {
    const card = e.target.closest(".bl-card");
    if (card) openEditor(opById(Number(card.dataset.op)));
  });

  // ---------- controles ----------
  function navigate(changes) {
    Object.assign(state, changes);
    state.scrollPending = true;
    load();
  }
  $("#prev").addEventListener("click", () => navigate({ date: state.data.prev }));
  $("#next").addEventListener("click", () => navigate({ date: state.data.next }));
  $("#today").addEventListener("click", () => navigate({ date: state.data.today }));
  $("#views").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) navigate({ view: b.dataset.view }); });
  $("#site").addEventListener("change", (e) => navigate({ site: e.target.value, line: "" }));
  $("#line").addEventListener("change", (e) => navigate({ line: e.target.value }));
  $("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { state.tab = b.dataset.tab; renderBacklog(); } });
  $("#q").addEventListener("input", (e) => { state.q = e.target.value; renderBacklog(); });
  $("#filters").addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    if (b.dataset.prio) { state.prio = b.dataset.prio; $$("[data-prio]").forEach((x) => x.classList.toggle("on", x === b)); }
    if (b.dataset.tier) { state.tier = b.dataset.tier; $$("[data-tier]").forEach((x) => x.classList.toggle("on", x === b)); }
    renderBacklog();
  });
  $("#live").addEventListener("click", (e) => {
    state.live = !state.live;
    e.currentTarget.classList.toggle("on", state.live);
    toast(state.live ? "Actualización en vivo activada" : "Actualización en vivo pausada");
  });
  $("#maint")?.addEventListener("click", maintenanceDialog);
  $("#auto")?.addEventListener("click", () => act(API.auto, { site: Number(state.site), keep_existing: true }, (d) =>
    `Plan automático: ${d.scheduled} operaciones programadas` +
    (d.late.length ? ` · ${d.late.length} atrasadas` : "") +
    (d.unassigned.length ? ` · sin líneas para: ${d.unassigned.join(", ")}` : "")));

  $("#import")?.addEventListener("click", () => $("#import-file").click());
  $("#import-file")?.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await fetch(API.import, { method: "POST", headers: { "X-CSRFToken": csrf() }, body: fd });
      const d = await r.json();
      if (!r.ok) {
        openDialog("No se pudo importar", `<p>${esc(d.error)}</p>${d.details ? `<ul class="err">${d.details.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
          <p class="sch-empty">Columnas: planta, sku, proceso, cantidad, compromiso, prioridad, referencia, cliente.</p>`);
        return;
      }
      toast(`Importadas ${d.created.length} órdenes al backlog`);
      load();
    } catch (err) { toast(err.message, true); }
  });

  // ---------- reloj, aviso, vivo ----------
  let toastTimer;
  function toast(msg, err = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.toggle("err", err);
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), err ? 6000 : 3000);
  }
  function tick() {
    if (!state.data || !$("#clock-date")) return;
    const now = new Date();
    $("#clock-date").textContent = new Intl.DateTimeFormat("es", { timeZone: tz(), weekday: "short", day: "numeric", month: "short", year: "numeric" }).format(now);
    $("#clock-time").textContent = new Intl.DateTimeFormat("es", { timeZone: tz(), hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(now);
  }
  setInterval(tick, 1000);
  setInterval(() => {
    if (state.live && !state.drag && !state.resizing && !dlg.open && !edlg.open && !document.hidden) load();
  }, 30000);
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => state.data && renderBoard(), 150); });

  load().then(tick);
})();
