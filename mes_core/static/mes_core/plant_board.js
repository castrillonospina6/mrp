/* Tablero de planta en vivo: una tarjeta por línea; se actualiza cada 30 s. */
(() => {
  "use strict";
  const root = document.getElementById("pb");
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const num = (n) => Number(n ?? 0).toLocaleString("es", { maximumFractionDigits: 1 });
  const REFRESH_MS = 30000;
  const ORDER = ["rodando", "asignada", "mantenimiento", "por_asignar", "fuera_turno"];
  const LEGEND = { rodando: "Rodando", asignada: "Asignadas", mantenimiento: "Mantenimiento", por_asignar: "Por asignar", fuera_turno: "Fuera de turno" };

  const site = root.dataset.site || "";  // «todas» o el id de la bodega (se elige con los botones de arriba)
  let filter = null;
  let data = null;
  let tz;

  // Reloj en la zona horaria de la planta
  function tick() {
    const d = new Date();
    const opts = tz ? { timeZone: tz } : {};
    $("#pb-time").textContent = new Intl.DateTimeFormat("es", { ...opts, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(d);
    $("#pb-date").textContent = new Intl.DateTimeFormat("es", { ...opts, weekday: "short", day: "2-digit", month: "short", year: "numeric" }).format(d);
  }
  setInterval(tick, 1000);
  tick();

  const kpi = (label, v) => `<div><span>${label}</span><b>${v === null || v === undefined ? "—" : `${v}%`}</b></div>`;
  const progress = (pct, label) => (pct === null || pct === undefined ? "" :
    `<div class="pb-prog" title="${esc(label)}"><i style="width:${Math.min(100, pct)}%"></i></div>`);

  function card(l) {
    const m = l.main;
    let body;
    if (m) {
      body = `<div class="pb-order"><a href="${esc(m.order_url)}">${esc(m.order)}</a> · ${esc(m.product)}</div>
        <div class="pb-sub">${esc(m.op)} · ${m.crew} op. · ${esc(m.start)} → <span class="${m.late ? "late" : ""}">${esc(m.end)}</span>${l.more ? ` · +${l.more} más` : ""}</div>
        ${progress(m.qty_pct, "Avance de cantidad")}
        <div class="pb-sub">${num(m.done)} / ${num(m.qty)} ${esc(m.unit)}${m.qty_pct !== null ? ` · ${m.qty_pct}%` : ""}${m.customer ? ` · <span class="tdot t-${esc(m.tier)}"></span> ${esc(m.customer)}` : ""}</div>`;
    } else if (l.downtime) {
      body = `<div class="pb-order">${esc(l.downtime.reason)}</div><div class="pb-sub">Hasta las ${esc(l.downtime.end)}</div>`;
    } else {
      body = `<div class="pb-order muted">Sin orden asignada</div>`;
      if (l.next) body += `<div class="pb-sub">Siguiente: <a href="${esc(l.next.order_url)}">${esc(l.next.order)}</a> · ${esc(l.next.product)} · ${esc(l.next.start)}</div>`;
      if (l.backlog) body += `<div class="pb-sub"><a href="${esc(root.dataset.scheduler)}">${l.backlog} por programar (${esc(l.kind_label)})</a></div>`;
    }
    if (l.next_downtime && !l.downtime) body += `<div class="pb-sub pb-maint-next">Mantenimiento ${esc(l.next_downtime.start)} · ${esc(l.next_downtime.reason)}</div>`;
    return `<article class="pb-card st-${l.state}">
      <div class="pb-h"><b>${esc(l.code)}</b><span>${esc(l.kind_label)}</span>
        ${l.capacity > 1 ? `<em title="Estaciones ocupadas">${l.busy}/${l.capacity}</em>` : ""}</div>
      <div class="pb-name">${esc(l.name)}</div>
      <div class="pb-state">${esc(l.state_label)}</div>
      <div class="pb-body">${body}</div>
      <div class="pb-kpis">${kpi("Disp.", l.kpi.avail)}${kpi("Rend.", l.kpi.perf)}${kpi("Cal.", l.kpi.qual)}</div>
    </article>`;
  }

  const shiftText = (g) => (g.shift ? `Turno ${g.shift.name} ${g.shift.start}–${g.shift.end}`
    : g.next_shift ? `Fuera de turno · siguiente: ${g.next_shift.name} ${g.next_shift.start}` : "Fuera de turno");

  function render() {
    if (!data) return;
    const sh = data.shift;
    if (data.all) {
      const n = data.groups.length;
      const on = data.groups.filter((g) => g.shift).length;
      $("#pb-shift").textContent = `${data.day_label} · Todas las bodegas`;
      $("#pb-shift-time").textContent = `${n} bodega${n === 1 ? "" : "s"} · ${on} en turno`;
    } else {
      $("#pb-shift").textContent = sh ? `${data.day_label} · Turno ${sh.name}` : `${data.day_label} · Fuera de turno`;
      $("#pb-shift-time").textContent = sh ? `${sh.start}–${sh.end}` : data.next_shift ? `Siguiente: ${data.next_shift.name} ${data.next_shift.start}` : "";
    }
    $("#pb-down").textContent = `${data.downtime_minutes} min`;
    $("#pb-stations").textContent = data.stations;
    $("#pb-ops-wrap").hidden = !data.operators;
    if (data.operators) {
      const o = data.operators;
      $("#pb-ops").textContent = `${o.used} en uso · ${o.free} libres de ${o.available}`;
      $("#pb-ops").className = o.free < 0 ? "neg" : "";
    }
    $("#pb-legend").innerHTML = ORDER.filter((k) => data.counts[k] || k !== "fuera_turno").map((k) =>
      `<button type="button" class="pb-chip st-${k} ${filter === k ? "on" : ""}" data-f="${k}" aria-pressed="${filter === k}">
        <i></i>${LEGEND[k]} <b>${data.counts[k]}</b></button>`).join("")
      + (filter ? `<button type="button" class="pb-chip clear" data-f="">Ver todas</button>` : "");
    const lines = data.lines.filter((l) => !filter || l.state === filter);
    let html = "";
    if (data.all) {
      for (const g of data.groups) {
        const mine = lines.filter((l) => l.site === g.site.code);
        if (!mine.length) continue;
        html += `<h2 class="pb-group"><b>${esc(g.site.name)}</b><span class="pb-group-code">${esc(g.site.code)}</span>
          <span class="pb-group-shift ${g.shift ? "on" : ""}">${esc(shiftText(g))}</span>
          <span class="pb-group-n">${mine.length} línea${mine.length === 1 ? "" : "s"}</span></h2>` + mine.map(card).join("");
      }
    } else {
      html = lines.map(card).join("");
    }
    $("#pb-grid").innerHTML = html
      || `<div class="sch-loading">${data.lines.length ? "Ninguna línea en este estado." : data.all ? "Ninguna bodega tiene líneas activas." : "Esta planta no tiene líneas activas."}</div>`;
  }

  $("#pb-legend").addEventListener("click", (e) => {
    const b = e.target.closest("[data-f]");
    if (!b) return;
    filter = b.dataset.f && filter !== b.dataset.f ? b.dataset.f : null;
    render();
  });

  async function load() {
    const url = new URL(root.dataset.api, location.href);
    if (site) url.searchParams.set("site", site);
    try {
      const r = await fetch(url, { headers: { Accept: "application/json" } });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || `Error ${r.status}`);
      data = d;
      tz = d.tz;
      $("#pb-error").hidden = true;
      $("#pb-live").classList.remove("off");
      render();
      tick();
    } catch (e) {
      $("#pb-error").textContent = `No se pudo actualizar: ${e.message}`;
      $("#pb-error").hidden = false;
      $("#pb-live").classList.add("off");
    }
  }

  $("#pb-full").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else root.requestFullscreen?.().catch(() => {});
  });
  document.addEventListener("fullscreenchange", () => {
    $("#pb-full span").textContent = document.fullscreenElement ? "Salir de pantalla completa" : "Pantalla completa";
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });

  load();
  setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
})();
