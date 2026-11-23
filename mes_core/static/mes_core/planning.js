/* Planeación por etapas: edición de etapas + cálculo en vivo (el servidor hace las cuentas). */
(() => {
  "use strict";
  const cfg = JSON.parse(document.getElementById("plan-config").textContent);
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const csrf = () => document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
  const num = (n, d = 2) => (n === null || n === undefined || Number.isNaN(n) ? "—" : Number(n).toLocaleString("es", { maximumFractionDigits: d }));
  const fmtDate = (iso) => (iso ? new Intl.DateTimeFormat("es", {
    timeZone: cfg.tz, weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(iso)) : "—");
  const fmtHours = (h) => (h === null || h === undefined ? "—" : h >= 24 ? `${num(h, 1)} h (${num(h / 24, 1)} d)` : `${num(h, 1)} h`);

  const UNIT = { UN: ["unidad", "unidades"], KG: ["kg", "kg"], CJ: ["caja", "cajas"] }[cfg.order.unit] || ["unidad", "unidades"];
  const unitLabel = (label) => label.replace("unidades", UNIT[1]).replace("unidad", UNIT[0]);
  const PARAM = { dias: "días", operarios: "operarios" };
  const QPARAM = { parcial: `${UNIT[1]} a procesar ahora`, lotes: `${UNIT[1]} por lote` };
  const locked = cfg.locked;
  const info = cfg.replan_info || [];
  let replan = false;  // re-planear solo lo pendiente (orden que ya empezó)
  let reopen = false;  // «Editar todo»: se rehace la planeación completa aunque ya haya empezado o esté terminada
  const readOnly = () => locked && !replan && !reopen;
  const prog = cfg.progress || {};
  const container = $("#stages");
  const LINK_ICON = { cola: "↓", flujo: "⇉", paralelo: "∥" };
  const LINK_HELP = {
    cola: "Empieza cuando termina la etapa anterior (y las que van a la par con ella).",
    flujo: "Trabaja al mismo tiempo que la anterior: le va llegando producto. Arranca cuando la anterior lleva estas unidades y no termina antes que ella.",
    paralelo: "Arranca junto con la etapa que abre el grupo, sin esperar producto de la anterior (p. ej. otro módulo con material propio).",
  };

  const blank = () => ({
    name: "", line: cfg.lines[0]?.id ?? "", quantity: cfg.order.quantity, std_value: "", std_unit: "min_unidad",
    setup_minutes: 0, mode: "turno", mode_param: "", qty_mode: "todo", qty_param: "",
    supervisor: "", efficiency: "", overtime: [], link: "cola", link_lag: "", report_mode: "", split_mode: "tiempo",
  });
  let stages = cfg.stages.length ? cfg.stages.map((s) => ({ ...blank(), ...s, mode_param: s.mode_param ?? "", qty_param: s.qty_param ?? "",
    supervisor: s.supervisor ?? "", efficiency: s.efficiency ?? "", overtime: s.overtime || [],
    link: s.link || "cola", link_lag: s.link_lag ?? "", report_mode: s.report_mode || "", split_mode: s.split_mode || "tiempo" })) : [blank()];
  const REPORT_HELP = {
    "": "Lo que tenga configurado el proceso o la línea en Control de piso → Forma de reporte (si nada, contador).",
    contador: "Módulo o mesa: el operario toca la pantalla por cada unidad.",
    intervalo: "Línea continua: cada operario ve una cuenta regresiva y al llegar a cero escribe lo que hizo.",
    supernumerario: "El supernumerario escribe cada cierto tiempo el total de la línea y se reparte entre el personal.",
  };
  const lineById = (id) => cfg.lines.find((l) => String(l.id) === String(id));
  const defaultEff = (line) => (line && line.efficiency) || cfg.default_efficiency;
  const otHours = (it) => {
    const [ah, am] = it.start.split(":").map(Number), [bh, bm] = it.end.split(":").map(Number);
    let m = bh * 60 + bm - (ah * 60 + am);
    if (m <= 0) m += 1440;
    return m / 60;
  };
  const DOW = ["dom", "lun", "mar", "mié", "jue", "vie", "sáb"];
  const otLabel = (it) => {
    const d = new Date(`${it.date}T12:00:00Z`);
    const day = `${DOW[d.getUTCDay()]} ${String(d.getUTCDate()).padStart(2, "0")}/${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
    if (it.kind === "extra") return `Turno adicional ${day} ${it.start}–${it.end}`;
    const sh = (it.label || "").split(" del ")[0] || "Turno";
    return `${sh} ${day}: ${it.start}–${it.end} (+${num(otHours(it), 2)} h)`;
  };
  $("#start").value = cfg.start;
  $("#locked").hidden = !locked;
  $("#locked-ro").hidden = cfg.can_replan;
  $("#locked-replan").hidden = !cfg.can_replan;
  $("#replan-toggle").hidden = !cfg.can_replan;
  $("#reopen-toggle").hidden = !cfg.can_reopen;
  $("#reopen-toggle").parentElement.hidden = !cfg.can_replan;  // con una sola opción no hace falta elegir
  const lostText = () => (cfg.cancelled ? "Al guardar se reactiva y vuelve a pasar por insumos y liberación. " : "") + (prog.reports
    ? `Al guardar se borran las operaciones actuales con lo reportado: ${prog.reports} reporte(s) con ${num(prog.good)} ${UNIT[1]} buenas` +
      `${prog.scrap ? ` y ${num(prog.scrap)} de merma` : ""}${prog.crews ? `, y las cuadrillas de ${prog.crews} operación(es)` : ""}. El pedido arranca de cero (queda liberado).`
    : `Al guardar se rehacen todas las operaciones${prog.crews ? ` (las cuadrillas de ${prog.crews} operación(es) se rearman)` : ""}.`);
  // Una orden que ya empezó o terminó se abre EDITABLE («Editar todo»); si se quiere conservar lo hecho, se cambia a
  // «Re-planear solo lo pendiente».
  function setMode(mode) {
    replan = mode === "replan";
    reopen = mode === "reopen";
    $("#replan-toggle")?.classList.toggle("on", replan);
    $("#reopen-toggle")?.classList.toggle("on", reopen);
    $("#locked").classList.toggle("on", replan || reopen);
    $("#reopen-note").hidden = !reopen;
    $("#reopen-note").textContent = reopen ? lostText() : "";
    $("#save").textContent = replan ? "Guardar lo pendiente y programar" : reopen ? "Guardar todo y programar" : $("#save").dataset.label;
    render();
  }
  $("#replan-toggle")?.addEventListener("click", () => setMode("replan"));
  $("#reopen-toggle")?.addEventListener("click", () => setMode("reopen"));
  $("#save").dataset.label = $("#save").textContent;
  if (locked && cfg.can_reopen) { replan = false; reopen = true; }

  const options = (list, value, label = (x) => x[1]) =>
    list.map((x) => `<option value="${esc(x[0])}" ${String(x[0]) === String(value) ? "selected" : ""}>${esc(label(x))}</option>`).join("");

  const timeModes = cfg.modes.filter((m) => m[0] !== "operarios");
  let last = [];  // último resultado de cada etapa (para "Usar libres" y para cambiar de enfoque)
  let lastOps = null;  // resumen de operarios de la última vista previa

  function card(st, i) {
    const lines = cfg.lines.map((l) => [l.id, `${l.label} (${l.kind})`]);
    const byCrew = st.mode === "operarios";
    const free = last[i]?.free_at_start;
    const inf = replan ? info[i] : null;
    const done = inf && !(inf.pending > 0);
    const link = i === 0 ? "" : `<div class="pl-link l-${esc(st.link)}" data-i="${i}">
        <span class="pl-link-ic" aria-hidden="true">${LINK_ICON[st.link] || "↓"}</span>
        <label><span class="pl-lbl">Relación con «${esc(stages[i - 1].name || `etapa ${i}`)}»</span>
          <select data-k="link">${options(cfg.links, st.link)}</select></label>
        <label class="pl-link-lag" ${st.link === "flujo" ? "" : "hidden"}><span class="pl-lbl">Arranca cuando la anterior lleve</span>
          <span class="pl-pair"><input type="number" data-k="link_lag" min="0" step="any" value="${esc(st.link_lag)}" placeholder="1">
          <small>${UNIT[1]}</small></span></label>
        <span class="pl-help">${LINK_HELP[st.link] || ""}</span>
      </div>`;
    return `${link}<article class="pl-stage ${done ? "done" : ""}" data-i="${i}">
      <header>
        <span class="pl-num">${i + 1}</span>
        <input class="pl-name" data-k="name" value="${esc(st.name)}" placeholder="Nombre de la etapa (p. ej. Etiquetado)" maxlength="120">
        <select data-k="line" title="Línea donde se hace la etapa" class="${lineById(st.line) ? "" : "pl-missing"}">${lines.length
          ? (lineById(st.line) ? "" : '<option value="" selected>— Elige la línea —</option>') + options(lines, st.line)
          : '<option value="">Sin líneas activas</option>'}</select>
        <div class="pl-tools">
          <button type="button" data-act="up" title="Subir" ${i === 0 ? "disabled" : ""}>↑</button>
          <button type="button" data-act="down" title="Bajar" ${i === stages.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" data-act="del" title="Eliminar etapa">×</button>
        </div>
      </header>
      <div class="pl-row">
        <label class="pl-field"><span class="pl-lbl">${UNIT[1]}</span>
          <input type="number" data-k="quantity" min="0" step="any" value="${esc(inf ? inf.pending : st.quantity)}">
          <span class="pl-help">${inf ? `Pendiente. Ya hecho o en proceso: ${num(inf.kept_qty)} ${UNIT[1]}${inf.in_progress ? ` (${inf.in_progress} en proceso)` : ""}`
            : `Del pedido: ${num(cfg.order.quantity)} ${UNIT[1]}`}</span></label>
        <label class="pl-field"><span class="pl-lbl">Tiempo estándar</span>
          <span class="pl-pair"><input type="number" data-k="std_value" min="0" step="any" value="${esc(st.std_value)}" placeholder="0,5">
          <select data-k="std_unit">${options(cfg.std_units, st.std_unit, (x) => unitLabel(x[1]))}</select></span></label>
        <label class="pl-field"><span class="pl-lbl">Alistamiento (min)</span>
          <input type="number" data-k="setup_minutes" min="0" step="any" value="${esc(st.setup_minutes)}">
          <span class="pl-help">Por lote</span></label>
      </div>
      <div class="pl-row two">
        <div class="pl-field"><span class="pl-lbl">Esta etapa: ¿qué calculo?</span>
          <div class="pl-pills" role="radiogroup">
            <label class="pl-pill"><input type="radio" name="calc-${i}" data-calc value="tiempo" ${byCrew ? "" : "checked"}> Operarios</label>
            <label class="pl-pill"><input type="radio" name="calc-${i}" data-calc value="operarios" ${byCrew ? "checked" : ""}> Tiempo</label>
          </div>
          <span class="pl-help">${byCrew ? "¿Cuántos operarios pones en esta etapa?" : "¿En cuánto tiempo la quieres hacer?"}</span>
          ${byCrew ? `<span class="pl-pair">
              <input type="number" data-k="mode_param" min="1" step="1" value="${esc(st.mode_param)}" placeholder="Operarios">
              <button type="button" class="sbtn sm" data-act="use-free" ${free === null || free === undefined || free < 1 ? "disabled" : ""}
                title="Usar los operarios libres cuando arranca esta etapa">Usar libres${free !== null && free !== undefined ? ` (${free})` : ""}</button></span>`
            : `<span class="pl-pair"><select data-k="mode">${options(timeModes, st.mode)}</select>
              <input type="number" data-k="mode_param" min="1" step="1" value="${esc(st.mode_param)}" placeholder="${PARAM[st.mode] || ""}" ${PARAM[st.mode] ? "" : "hidden"}></span>`}
        </div>
        <label class="pl-field"><span class="pl-lbl">Cantidad</span>
          <span class="pl-pair"><select data-k="qty_mode">${options(cfg.qty_modes, st.qty_mode)}</select>
          <input type="number" data-k="qty_param" min="0" step="any" value="${esc(st.qty_param)}" placeholder="${QPARAM[st.qty_mode] || ""}" ${QPARAM[st.qty_mode] ? "" : "hidden"}></span></label>
      </div>
      <div class="pl-row two pl-who">
        <label class="pl-field"><span class="pl-lbl">Supervisor responsable</span>
          <select data-k="supervisor" ${cfg.can_assign ? "" : "disabled title=\"Solo quien asigna supervisores puede cambiarlo\""}>
            <option value="">Automático (regla por línea y turno)</option>
            ${cfg.supervisors.map((u) => `<option value="${u.id}" ${String(u.id) === String(st.supervisor) ? "selected" : ""}>${esc(u.label)}</option>`).join("")}
          </select>
          <span class="pl-help" data-sup-hint>${st.supervisor ? "Elegido a mano: la regla de línea y turno no lo cambia." : "Lo pone la regla de la línea y el turno."}</span></label>
        <label class="pl-field"><span class="pl-lbl">Rendimiento de la gente (%)</span>
          <span class="pl-pair"><input type="number" data-k="efficiency" min="10" max="150" step="1" value="${esc(st.efficiency)}"
              placeholder="${defaultEff(lineById(st.line))}">
            ${lineById(st.line)?.measured ? `<button type="button" class="sbtn sm" data-act="use-measured"
              title="Medido en ${lineById(st.line).measured.ops} operación(es) de los últimos ${lineById(st.line).measured.days} días">Usar medido ${lineById(st.line).measured.pct} %</button>` : ""}</span>
          <span class="pl-help">${st.efficiency ? "De esta etapa." : lineById(st.line)?.efficiency ? `Vacío = el de la línea (${lineById(st.line).efficiency} %).` : `Vacío = el de la planta (${cfg.default_efficiency} %).`}
            100 % = el estándar exacto; lo realista suele estar entre 80 y 90 %.</span></label>
      </div>
      <div class="pl-row two">
        <label class="pl-field"><span class="pl-lbl">Reporte en planta</span>
          <select data-k="report_mode"><option value="">Automático (el del proceso o la línea)</option>${options(cfg.report_modes || [], st.report_mode)}</select>
          <span class="pl-help">${esc(REPORT_HELP[st.report_mode] || "")} El supernumerario siempre puede escribir cantidades a mano.</span></label>
        <label class="pl-field"><span class="pl-lbl">Reparto de lo reportado por la línea</span>
          <select data-k="split_mode">${options(cfg.split_modes || [], st.split_mode)}</select>
          <span class="pl-help">${st.split_mode === "iguales" ? "Todos los de la etapa reciben lo mismo." : "A quien tuvo paros, llegó tarde o faltó le toca menos."}
            La carga de la etapa también se reparte: cada persona ve su parte de las ${UNIT[1]}.</span></label>
      </div>
      ${done ? `<div class="pl-donebar">Etapa sin pendientes: ${num(inf.kept_qty)} ${UNIT[1]} hechas o en proceso${inf.kept_end ? `, hasta ${fmtDate(inf.kept_end)}` : ""}. Se conserva tal cual.</div>` : ""}
      <div class="pl-results" data-res><span class="pl-help">Completa el tiempo estándar para calcular.</span></div>
      <div class="pl-ot" data-ot>
        <div class="pl-ot-h"><b>Horas extra de esta etapa</b><span class="pl-help" data-ot-sum></span></div>
        <ul class="pl-ot-list">${st.overtime.map((it, k) => `<li><span>${esc(otLabel(it))}</span>
            <button type="button" data-act="ot-del" data-k="${k}" aria-label="Quitar">×</button></li>`).join("") || '<li class="muted">Sin horas extra.</li>'}</ul>
        <div class="pl-ot-add">
          <select data-ot-point aria-label="Turno a alargar"><option value="">Calcula para ver los turnos que se pueden alargar</option></select>
          <select data-ot-hours aria-label="Horas extra">${[0.25, 0.5, 0.75, 1, 1.5, 2, 2.5, 3, 4].map((h) => `<option value="${h}" ${h === 1 ? "selected" : ""}>+${num(h, 2)} h</option>`).join("")}</select>
          <button type="button" class="sbtn sm" data-act="ot-add">Alargar turno</button>
        </div>
      </div>
    </article>`;
  }

  function render() {
    container.innerHTML = stages.map(card).join("");
    syncApproach();
    const all = "#stages input, #stages select, #stages button, #add-stage, #save, #start, #approach input, #approach button, #respect";
    $$(all).forEach((el) => (el.disabled = readOnly()));
    if (replan) {
      // lo pendiente se re-planea sin agregar, quitar ni reordenar etapas; las unidades son las que faltan
      $$('#add-stage, #stages [data-act="up"], #stages [data-act="down"], #stages [data-act="del"], #stages .pl-name, #stages [data-k="quantity"]')
        .forEach((el) => (el.disabled = true));
      $$("#stages .pl-stage.done").forEach((c) => c.querySelectorAll("input, select, button").forEach((el) => (el.disabled = true)));
    }
    schedulePreview();
  }

  container.addEventListener("input", (e) => {
    const k = e.target.dataset.k;
    if (!k) return;
    const i = Number(e.target.closest("[data-i]").dataset.i);  // la relación va entre tarjetas, fuera de .pl-stage
    stages[i][k] = e.target.value;
    if (k === "mode" || k === "qty_mode") {
      const p = e.target.parentElement.querySelector("input");
      const hint = k === "mode" ? PARAM[e.target.value] : QPARAM[e.target.value];
      p.hidden = !hint;
      p.placeholder = hint || "";
      if (hint) p.focus();
    }
    if (k === "mode_param") syncApproach();
    if (k === "link" || k === "line" || k === "report_mode" || k === "split_mode") { render(); return; }  // la línea cambia el rendimiento sugerido y quita el aviso
    schedulePreview();
  });
  // Cambiar el enfoque de una etapa o de todas
  function setApproach(i, kind) {
    const st = stages[i];
    if (kind === "operarios" && st.mode !== "operarios") {
      st._timeMode = st.mode;
      st._timeParam = st.mode_param;
      st.mode = "operarios";
      st.mode_param = last[i]?.crew ?? "";  // arranca con lo que calculó el otro enfoque
    } else if (kind === "tiempo" && st.mode === "operarios") {
      st.mode = st._timeMode || "turno";
      st.mode_param = st._timeParam ?? "";
    }
  }
  // Enfoque general: aplica a todas las etapas
  $$('#approach input[name="approach"]').forEach((r) => r.addEventListener("change", () => {
    if (!r.checked || readOnly()) return;
    stages.forEach((_, i) => setApproach(i, r.value));
    render();
    if (r.value === "operarios") $("#all-crew").focus();
  }));
  function syncApproach() {
    const crewAll = stages.every((s) => s.mode === "operarios");
    const timeAll = stages.every((s) => s.mode !== "operarios");
    $$('#approach input[name="approach"]').forEach((r) => {
      r.checked = (r.value === "operarios" && crewAll) || (r.value === "tiempo" && timeAll);
    });
    $("#approach-mixed").hidden = crewAll || timeAll;
    $("#allcrew").hidden = !crewAll;
    const vals = [...new Set(stages.map((s) => String(s.mode_param ?? "")))];
    if (crewAll && document.activeElement !== $("#all-crew")) $("#all-crew").value = vals.length === 1 ? vals[0] : "";
  }
  // "Operarios para todas las etapas"
  function setAllCrew(value) {
    stages.forEach((s) => { if (s.mode === "operarios") s.mode_param = value; });
    $$('#stages [data-k="mode_param"]').forEach((el) => (el.value = value));
    schedulePreview();
  }
  $("#all-crew").addEventListener("input", (e) => setAllCrew(e.target.value));
  $("#all-free").addEventListener("click", () => {
    const free = lastOps?.free;
    if (free === null || free === undefined || free < 1) return;
    $("#all-crew").value = free;
    setAllCrew(String(free));
  });

  // Enfoque de una sola etapa
  container.addEventListener("change", (e) => {
    if (!e.target.matches("[data-calc]") || !e.target.checked) return;
    const i = Number(e.target.closest(".pl-stage").dataset.i);
    setApproach(i, e.target.value);
    render();
    if (e.target.value === "operarios") $(`.pl-stage[data-i="${i}"] [data-k="mode_param"]`)?.focus();
  });

  container.addEventListener("change", (e) => {
    if (e.target.dataset.k === "supervisor" || e.target.dataset.k === "line") render();
  });
  container.addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    const i = Number(e.target.closest(".pl-stage").dataset.i);
    if (act === "use-free") {
      stages[i].mode_param = last[i]?.free_at_start ?? "";
      render();
      return;
    }
    if (act === "use-measured") {
      stages[i].efficiency = lineById(stages[i].line)?.measured?.pct ?? "";
      render();
      return;
    }
    if (act === "ot-del") {
      stages[i].overtime.splice(Number(e.target.closest("[data-act]").dataset.k), 1);
      render();
      return;
    }
    if (act === "ot-add") {
      const card = e.target.closest(".pl-stage");
      const pt = (last[i]?.points || [])[Number(card.querySelector("[data-ot-point]").value)];
      if (!pt) return;
      const hours = Math.min(Number(card.querySelector("[data-ot-hours]").value), pt.max_hours);
      const [h, m] = pt.start.split(":").map(Number);
      const endMin = (h * 60 + m + Math.round(hours * 60)) % 1440;
      stages[i].overtime.push({ kind: "ot", date: pt.date, shift: pt.shift, start: pt.start, label: pt.label,
        end: `${String(Math.floor(endMin / 60)).padStart(2, "0")}:${String(endMin % 60).padStart(2, "0")}` });
      render();
      return;
    }
    if (act === "del") {
      if (stages.length === 1) return;
      stages.splice(i, 1);
      last.splice(i, 1);
    } else {
      const j = act === "up" ? i - 1 : i + 1;
      [stages[i], stages[j]] = [stages[j], stages[i]];
      [last[i], last[j]] = [last[j], last[i]];
    }
    render();
  });
  $("#add-stage").addEventListener("click", () => {
    stages.push(blank());
    render();
    $$(".pl-name").at(-1)?.focus();
  });
  $("#start").addEventListener("input", schedulePreview);
  $("#respect").addEventListener("change", schedulePreview);

  const payload = () => ({
    start: $("#start").value,
    respect_operators: $("#respect").checked,
    replan,
    reopen,
    stages: stages.map(({ _timeMode, _timeParam, ...s }) => ({ ...s, line: s.line ? Number(s.line) : null })),
  });

  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() }, body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `Error ${r.status}`);
    return data;
  }

  let timer, seq = 0;
  function schedulePreview() {
    clearTimeout(timer);
    timer = setTimeout(runPreview, 350);
  }
  async function runPreview() {
    const my = ++seq;
    const errors = $("#errors");
    try {
      const data = await post(cfg.urls.preview, payload());
      if (my !== seq) return;
      errors.hidden = true;
      showResults(data);
    } catch (e) {
      if (my !== seq) return;
      errors.innerHTML = esc(e.message).split("; ").join("<br>");
      errors.hidden = false;
      $("#s-verdict").className = "pl-verdict";
      $("#s-verdict").textContent = "";
    }
  }

  const kpi = (label, value, cls = "") => `<div class="pl-kpi ${cls}"><span>${label}</span><b>${value}</b></div>`;

  function shiftPlan(r) {
    if (r.shifts_before_due === null || r.shifts_before_due === undefined) return "";
    if (!(r.shifts > r.shifts_before_due + 0.005)) {
      return `<div class="pl-shiftplan ok">Necesita <b>${num(r.shifts, 2)} turnos</b> y antes del compromiso caben <b>${num(r.shifts_before_due, 2)}</b>: alcanza.</div>`;
    }
    const missing = r.shifts - r.shifts_before_due;
    return `<div class="pl-shiftplan">Necesita <b>${num(r.shifts, 2)} turnos</b> y antes del compromiso caben <b>${num(r.shifts_before_due, 2)}</b>:
      faltan ${num(missing, 2)} turno (≈ ${fmtHours(missing * r.shift_hours)} de horas extra).
      <button type="button" class="sbtn sm" data-suggest>Sugerir horas extra</button></div>`;
  }

  function opLine(r) {
    if (r.free_at_start === null || r.free_at_start === undefined) return "";
    const rem = r.remaining;
    return `<div class="pl-opline">
        <span>Libres al iniciar: <b>${r.free_at_start}</b></span>
        <span>→ con esta etapa quedan <b class="${rem < 0 ? "neg" : ""}">${rem}</b>${r.free_min !== r.free_at_start ? " <small>(en el momento más justo)</small>" : ""}</span>
        <span>· Al terminar (${fmtDate(r.release_at)}) se liberan <b>${r.crew}</b> → quedan <b>${r.free_after}</b> libres</span>
      </div>`;
  }

  function showResults(data) {
    last = data.stages;
    data.stages.forEach((r, i) => {
      const card = $(`.pl-stage[data-i="${i}"]`);
      const el = card && card.querySelector("[data-res]");
      if (!el) return;
      if (r.done) {
        el.innerHTML = "";
        return;
      }
      const byCrew = stages[i]?.mode === "operarios";
      const short = r.operators_available && r.crew > r.operators_available;
      const lots = r.lots > 1 ? `<div class="pl-lots">${r.lots} lotes: ${r.schedule.map((l, k) =>
        `<span title="${fmtDate(l.start)} → ${fmtDate(l.end)}">#${k + 1} · ${num(l.qty)} ${UNIT[1]}</span>`).join("")}</div>` : "";
      el.innerHTML = `<div class="pl-kpis">
          ${kpi(`Min / ${UNIT[0]} reales`, `${num(r.mpu, 4)} <small>estándar ${num(r.std_mpu, 4)} al ${r.efficiency} %</small>`)}
          ${(() => {
            const t = data.summary.takt?.stages?.[i], tk = data.summary.takt?.takt;
            if (!t || t.setup_only) return kpi("Horas-hombre", num(r.man_hours, 1));
            return kpi("Ciclo / takt", `${fmtMin(t.ct)} <small>${tk ? `takt ${fmtMin(tk)}` : "sin takt"}</small>`, tk ? (t.ok ? "good" : "bad") : "");
          })()}
          ${kpi("Horas-hombre", num(r.man_hours, 1))}
          ${kpi(byCrew ? "Operarios asignados" : "Operarios necesarios", `${r.crew}${r.operators_available ? ` <small>de ${r.operators_available}</small>` : ""}`,
            short ? "bad" : byCrew ? "" : "good primary")}
          ${kpi(byCrew ? "Tiempo de proceso" : "Duración", fmtHours(r.duration_hours), byCrew ? "good primary" : "")}
          ${kpi("Turnos", `${num(r.shifts, 2)} <small>de ${num(r.shift_hours, 2)} h</small>`,
            r.shifts_before_due !== null && r.shifts_before_due !== undefined && r.shifts > r.shifts_before_due + 0.005 ? "bad" : "")}
          ${kpi("Días", `${num(r.days, 2)} <small>${r.shifts_per_day} turno${r.shifts_per_day === 1 ? "" : "s"}/día</small>`)}
          ${kpi(byCrew ? "Inicio → termina" : "Programado", `${fmtDate(r.start)} → ${fmtDate(r.end)}`, "wide")}
          ${i > 0 && r.link !== "cola" ? kpi("Con la etapa anterior", r.link === "flujo" ? "A la par (le va llegando producto)" : "Al mismo tiempo, sin esperarla", "wide") : ""}
        </div>${shiftPlan(r)}${opLine(r)}${lots}
        ${r.warnings.length ? `<ul class="pl-warn">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}`;
      const hint = card.querySelector("[data-sup-hint]");
      if (hint && !stages[i]?.supervisor) {
        hint.textContent = r.supervisor_rule?.length ? `Por regla: ${r.supervisor_rule.join(" · ")}` : "Sin regla de supervisor para esta línea: queda sin asignar (elige uno o crea la regla).";
        hint.classList.toggle("warn", !r.supervisor_rule?.some((x) => !x.startsWith("sin regla")));
      }
      const sel = card.querySelector("[data-ot-point]");
      if (sel) {
        sel.innerHTML = (r.points || []).length
          ? r.points.map((p, k) => `<option value="${k}">${esc(p.label)} (hasta +${num(p.max_hours, 1)} h)</option>`).join("")
          : '<option value="">No hay turnos para alargar en esta etapa</option>';
      }
      const sum = card.querySelector("[data-ot-sum]");
      if (sum) sum.textContent = r.overtime_hours ? `${num(r.overtime_hours, 2)} h · ${num(r.overtime_man_hours, 1)} horas-hombre extra (${r.crew} operarios)` : "";
      const useFree = card.querySelector('[data-act="use-free"]');
      if (useFree) {
        const ok = r.free_at_start !== null && r.free_at_start !== undefined && r.free_at_start >= 1;
        useFree.disabled = readOnly() || !ok;
        useFree.textContent = ok ? `Usar libres (${r.free_at_start})` : "Usar libres";
      }
    });
    renderOperators(data.summary.operators, data.summary.max_crew);
    renderTakt(data.summary.takt);
    const s = data.summary;
    $("#s-avail").textContent = fmtHours(s.available_hours);
    $("#s-hh").textContent = `${num(s.man_hours, 1)} HH`;
    $("#s-crew").textContent = s.max_crew;
    $("#s-start").textContent = fmtDate(s.start);
    $("#s-end").textContent = fmtDate(s.end);
    $("#s-due").textContent = fmtDate(s.due);
    const v = $("#s-verdict");
    if (s.slack_hours === null) {
      v.className = "pl-verdict";
      v.textContent = "El pedido no tiene fecha compromiso.";
    } else if (s.slack_hours >= 0) {
      v.className = "pl-verdict ok";
      v.textContent = `A tiempo · holgura de ${fmtHours(s.slack_hours)} antes del compromiso`;
    } else {
      v.className = "pl-verdict bad";
      v.textContent = `No alcanza: termina ${fmtHours(-s.slack_hours)} después del compromiso. Prueba «Sugerir horas extra», sube operarios o adelanta el inicio.`;
    }
    const otTotal = data.stages.reduce((a, r) => a + (r.overtime_hours || 0), 0);
    const otHH = data.stages.reduce((a, r) => a + (r.overtime_man_hours || 0), 0);
    $("#ot-total").textContent = otTotal ? `Este plan usa ${num(otTotal, 2)} h extra (${num(otHH, 1)} horas-hombre).` : "Este plan no usa horas extra.";
    $("#ot-suggest").classList.toggle("accent", s.slack_hours !== null && s.slack_hours < 0);
  }

  const fmtMin = (m) => {
    if (m === null || m === undefined) return "—";
    if (m < 1) return `${num(m * 60, 1)} s`;
    if (m < 60) return `${num(m, 2)} min`;
    return fmtHours(m / 60);
  };
  function renderTakt(t) {
    const none = !t || t.takt === null || t.takt === undefined;
    $("#tk-none").hidden = !none;
    $("#tk-body").hidden = none;
    if (none) {
      $("#tk-none").textContent = cfg.order.due ? "Sin tiempo disponible antes del compromiso: el takt no se puede calcular."
        : "El pedido no tiene fecha compromiso: el takt necesita saber en cuánto tiempo hay que entregar.";
      return;
    }
    const u = UNIT[0];
    $("#tk-takt").textContent = fmtMin(t.takt);
    $("#tk-takt-s").textContent = `por ${u}`;
    $("#tk-ct").textContent = fmtMin(t.process_ct);
    $("#tk-ct").className = t.meets ? "ok" : "neg";
    $("#tk-ct-s").textContent = t.bottleneck !== null ? `cuello: ${t.stages[t.bottleneck].name}` : "";
    const v = $("#tk-verdict");
    v.className = `pl-takt-verdict ${t.meets ? "ok" : "bad"}`;
    v.textContent = t.meets
      ? `El proceso sigue el ritmo: el ciclo más lento (${fmtMin(t.process_ct)}) está por debajo del takt (${fmtMin(t.takt)}).`
      : `El ciclo de «${t.stages[t.bottleneck].name}» (${fmtMin(t.process_ct)}) supera el takt (${fmtMin(t.takt)}): sube operarios en esa etapa o mejora su rendimiento.`;
    $("#tk-demand").textContent = `${num(t.demand)} ${UNIT[1]}${replan ? " pendientes" : ""}`;
    $("#tk-avail").textContent = fmtHours(t.available_min / 60);
    $("#tk-std").textContent = `${fmtMin(t.std_piece)}${t.std_vs_takt !== null ? ` · ${num(t.std_vs_takt, 2)} × takt` : ""}`;
    $("#tk-real").textContent = fmtMin(t.real_piece);
    $("#tk-pph").textContent = `${num(t.pph_needed, 1)} · cuello hace ${num(t.pph_bottleneck, 1)}`;
    $("#tk-ops").textContent = t.min_operators !== null ? `${t.min_operators} · el plan asigna ${t.crew_used} (suma de etapas)` : "—";
    $("#tk-ops").className = t.min_operators !== null && t.crew_used < t.min_operators ? "neg" : "";
    const maxV = Math.max(t.takt, ...t.stages.map((x) => x.ct)) || 1;
    $("#tk-bars").innerHTML = t.stages.map((x, i) => x.setup_only
      ? `<li class="setup"><span class="n">${esc(x.name)}</span><span class="v">${x.done ? "terminada" : "solo alistamiento"}</span></li>`
      : `<li class="${x.ok ? "ok" : "over"} ${i === t.bottleneck ? "bn" : ""}" title="${esc(x.line)} · ${num(x.real_mpu, 3)} min/${u} reales ÷ ${x.crew} operario(s)">
          <span class="n">${esc(x.name)}${i === t.bottleneck ? " · cuello" : ""}</span><span class="v">${fmtMin(x.ct)}</span>
          <span class="bar"><i style="width:${Math.min(100, (x.ct / maxV) * 100)}%"></i><em style="left:${(t.takt / maxV) * 100}%" title="Takt"></em></span></li>`).join("");
    $("#tk-setup").textContent = t.setup_min ? `El alistamiento suma ${fmtHours(t.setup_min / 60)} aparte${t.takt_net ? `; descontándolo, el takt queda en ${fmtMin(t.takt_net)}` : ""}.` : "";
  }

  function renderOperators(o, maxCrew) {
    lastOps = o;
    const allFree = $("#all-free");
    const ok = o && o.free !== null && o.free !== undefined && o.free >= 1;
    allFree.disabled = readOnly() || !ok;
    allFree.textContent = ok ? `Usar libres (${o.free})` : "Usar libres";
    $("#ops").hidden = !o;
    $("#ops-none").hidden = Boolean(o);
    if (!o) return;
    $("#o-free").textContent = o.free ?? "—";
    $("#o-free").className = o.free !== null && o.free < maxCrew ? "neg" : "";
    $("#o-at").textContent = `(${fmtDate(o.at)})`;
    $("#o-avail").textContent = o.available ?? "—";
    $("#o-used").textContent = o.used_by_others ?? "—";
    $("#o-max").textContent = maxCrew;
    $("#o-rel").innerHTML = o.releases.length
      ? o.releases.map((x) => `<li><span class="t">${fmtDate(x.at)}</span><span>+${x.released}</span>
          <span class="f">quedan ${x.free_after ?? "—"} libres</span><small>${x.labels.map(esc).join(", ")}</small></li>`).join("")
      : `<li class="muted">Nadie se libera mientras dura esta planeación.</li>`;
  }

  // Sugerencias de horas extra para cumplir el compromiso
  async function suggest() {
    const box = $("#ot-options");
    box.innerHTML = '<p class="pl-help">Calculando opciones…</p>';
    try {
      const d = await post(cfg.urls.overtime, payload());
      if (d.error) { box.innerHTML = `<p class="pl-help">${esc(d.error)}</p>`; return; }
      if (!d.needed) { box.innerHTML = `<p class="pl-help">No hace falta: el plan termina ${fmtHours(d.slack_hours)} antes del compromiso.</p>`; return; }
      const opts = d.options || [];
      box.innerHTML = `<p class="pl-help">Termina ${fmtHours(d.late_hours)} tarde. Las horas extra rinden al ${d.ot_efficiency} % (se suma ese margen).</p>` +
        (opts.length ? opts.map((o, k) => `<article class="pl-otopt ${o.meets ? "ok" : "no"} ${k === d.best ? "best" : ""}">
          <div class="t">${k === d.best ? '<span class="pill brand">Recomendada</span>' : ""}<span class="pill ${o.meets ? "ok" : "bad"}">${o.meets ? "Cumple" : "No alcanza"}</span></div>
          <b>${esc(o.title)}</b>
          <p>${esc(o.detail)}</p>
          <dl><dt>Horas extra</dt><dd>${num(o.hours, 2)} h · ${num(o.man_hours, 1)} HH (${o.crew} op.)</dd>
            <dt>Termina</dt><dd>${fmtDate(o.end)}</dd>
            <dt>Holgura</dt><dd class="${o.slack_hours < 0 ? "neg" : ""}">${o.slack_hours < 0 ? "−" : ""}${fmtHours(Math.abs(o.slack_hours))}</dd></dl>
          <button type="button" class="sbtn sm ${o.meets ? "accent" : ""}" data-use="${k}">Usar esta opción</button></article>`).join("")
          : `<p class="pl-help">${esc(d.message || "No encontré turnos para alargar ni huecos antes del compromiso. Sube operarios o negocia la fecha.")}</p>`);
      box.querySelectorAll("[data-use]").forEach((b) => b.addEventListener("click", () => {
        const o = opts[Number(b.dataset.use)];
        o.items.forEach(({ stage, ...it }) => stages[stage].overtime.push(it));
        box.innerHTML = `<p class="pl-help">Horas extra agregadas a ${o.lines > 1 ? `${o.lines} líneas del pedido` : `«${esc(o.stage_name)}»`}. Revisa el resultado y guarda.</p>`;
        render();
      }));
    } catch (e) {
      box.innerHTML = `<p class="pl-help">${esc(e.message)}</p>`;
    }
  }
  $("#ot-suggest").addEventListener("click", suggest);
  container.addEventListener("click", (e) => { if (e.target.closest("[data-suggest]")) { suggest(); $("#otbox").scrollIntoView({ behavior: "smooth", block: "nearest" }); } });

  $("#save").addEventListener("click", async () => {
    const btn = $("#save");
    if (reopen && locked && !window.confirm(`¿Editar todo el pedido ${cfg.order.number}?\n\n${lostText()}\n\nNo se puede deshacer.`)) return;
    btn.disabled = true;
    try {
      const d = await post(cfg.urls.save, payload());
      window.location.href = d.redirect;
    } catch (e) {
      $("#errors").innerHTML = esc(e.message).split("; ").join("<br>");
      $("#errors").hidden = false;
      btn.disabled = false;
    }
  });

  if (reopen) setMode("reopen"); else render();
})();
