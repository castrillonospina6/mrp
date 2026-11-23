/* Tablet de piso: operario · tablet de línea · patinador.
   Los datos vienen del MES (/p/piso/api/estado). Cada 2,5 s se pregunta por el «pulso» y solo se recarga cuando cambió
   algo (o cada 20 s para refrescar los indicadores). */
(() => {
'use strict';
const A = document.getElementById('app'), CFG = JSON.parse(document.getElementById('piso-cfg').textContent);
const $ = s => document.querySelector(s);
const E = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
let S = null, skew = 0, pulse = '', lastLoad = 0, offline = false, booted = false;
let tab = 'inicio', sheet = null, SV = {}, loginMode = 'op', loginErr = '', pinBuf = '', DN = null, busy = false;
let me = {};
try { me = JSON.parse(localStorage.getItem('piso.me') || '{}'); } catch (e) { me = {}; }
const save = () => { try { localStorage.setItem('piso.me', JSON.stringify(me)); } catch (e) { /* sin almacenamiento */ } };
const now = () => Date.now() + skew;
const T = s => s ? new Date(s).getTime() : 0;
const T0 = () => { const d = new Date(now()); d.setHours(0, 0, 0, 0); return d.getTime(); };

// ---------------------------------------------------------------- formato
const P = v => v == null ? '–' : Math.round(v) + '%', P1 = v => v == null ? '–' : v.toFixed(1) + '%';
const cl = v => v == null ? '' : v >= 85 ? 'ok' : v < 70 ? 'rd' : '';
const nf = n => Math.round(n || 0).toLocaleString('es-CO');
const q = n => n == null ? '–' : (+n).toLocaleString('es-CO', {maximumFractionDigits: 2});
const fmt = s => { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
const mmss = s => { s = Math.max(0, Math.round(s || 0)); return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); };
const hms = s => { s = Math.max(0, Math.floor(s)); return [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60].map(v => String(v).padStart(2, '0')).join(':'); };
const hT = t => t ? new Date(t).toLocaleTimeString('es-CO', {hour: '2-digit', minute: '2-digit', hour12: false}) : '–';
const dT = t => t ? new Date(t).toLocaleDateString('es-CO', {weekday: 'short', day: '2-digit', month: 'short'}) : '';
const mf = s => s == null ? '–' : fmt(s);
const init = n => String(n || '?').split(' ').filter(Boolean).map(w => w[0]).join('').slice(0, 2).toUpperCase();
const PAL = ['#8b74ff', '#ff8a5b', '#2fb5a8', '#e8b43a', '#5b8def', '#d6678a'];
const avc = (n, i = 0, sz, ring) => `<i class=av style="background:${PAL[i % 6]};${sz ? `width:${sz};height:${sz};font-size:${parseInt(sz) > 36 ? 13 : 10}px;` : ''}${ring ? `box-shadow:0 0 0 3px ${ring};` : ''}">${E(init(n))}</i>`;
const ring = (v, mx, c, inner, id = '') => { const C = 2 * Math.PI * 52, p = Math.min(1, mx > 0 ? v / mx : 0);
  return `<div class=rg><svg viewBox="0 0 120 120"><circle cx=60 cy=60 r=52 class=rt></circle><circle ${id ? `id=${id}` : ''} cx=60 cy=60 r=52 class=rb stroke="${c}" stroke-dasharray="${C * p} ${C}" transform="rotate(-90 60 60)"></circle></svg><div class=ri>${inner}</div></div>`; };
const IC = {
  inicio: '<path d="M3 11l9-8 9 8v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  material: '<path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z"/><path d="M3 7.5 12 12l9-4.5M12 12v9M7.5 5.2l9 4.6"/>',
  historial: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4M8 14h3"/>',
  cola: '<path d="M9 6h12M9 12h12M9 18h12"/><circle cx="4.5" cy="6" r="1.2"/><circle cx="4.5" cy="12" r="1.2"/><circle cx="4.5" cy="18" r="1.2"/>',
  ficha: '<path d="M6 3h8l5 5v13H6z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
  ajustes: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  play: '<path d="M7 4.5l12 7.5-12 7.5z"/>', refresh: '<path d="M20 12a8 8 0 1 1-2.6-5.9M20 4v4.5h-4.5"/>',
  down: '<path d="M10 15v4a2 2 0 0 0 2 2l3-7V3H6.5A2 2 0 0 0 4.5 4.7l-1 7A2 2 0 0 0 5.5 14zM18 3h3v11h-3"/>',
  flag: '<path d="M5 21V4M5 4h12l-2.5 4L17 12H5"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.7 7.1L4 20.5l1.3-4.6A8 8 0 1 1 21 12z"/><path d="M8.5 11.5h.01M12 11.5h.01M15.5 11.5h.01"/>',
  warn: '<path d="M12 3.5L2.5 20h19z"/><path d="M12 10v4.5M12 17.2v.01"/>', ok: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l3 3 5-6"/>',
  sad: '<circle cx="12" cy="12" r="9"/><path d="M9 10h.01M15 10h.01M8.5 16.3c1.2-1.6 5.8-1.6 7 0"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.7-1.5"/>', lockc: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  lineas: '<path d="M3 7h18M3 12h18M3 17h18"/><circle cx="7" cy="7" r="1.6"/><circle cx="14" cy="12" r="1.6"/><circle cx="10" cy="17" r="1.6"/>',
  extras: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2M9 2h6M19 5l1.5-1.5"/>',
  personal: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c.6-3.6 3-5.5 6-5.5s5.4 1.9 6 5.5"/><circle cx="17" cy="9" r="2.4"/><path d="M16 14.6c2.6-.2 4.4 1.4 5 4.4"/>',
};
const svg = k => `<svg viewBox="0 0 24 24">${IC[k]}</svg>`;
const ICN = (c, t, k) => `<i class="${c}" title="${t}">${svg(k)}</i>`;
const toast = (m, k) => { const h = $('#ts'); if (!h) return; const d = document.createElement('div'); d.className = 'tst ' + (k || ''); d.textContent = m; h.appendChild(d); setTimeout(() => d.remove(), 3500); };

// ---------------------------------------------------------------- servidor
const csrf = () => { const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; };
async function api(path, body) {
  const opt = body === undefined ? {credentials: 'same-origin'} :
    {method: 'POST', credentials: 'same-origin', headers: {'content-type': 'application/json', 'X-CSRFToken': csrf()}, body: JSON.stringify(body)};
  let r;
  try { r = await fetch(CFG.base + 'api/' + path, opt); } catch (e) { offline = true; paintOffline(); throw new Error('Sin conexión con el servidor.'); }
  let j = {};
  try { j = await r.json(); } catch (e) { /* respuesta vacía */ }
  if (r.status === 401 && j.e === 'login') {
    if (j.closed) sessionClosed();
    if (S) alarmOff();
    S = null; render(); const e = new Error('login'); e.login = true; throw e;
  }
  if (r.status === 403 && !j.e) throw new Error('La sesión venció: recarga la página.');
  if (r.status === 409 && j.checkin && S) { S.checkin = {done: false, info: (S.checkin || {}).info || {}}; render(); setTimeout(load, 0); }
  if (r.status === 409 && j.overtime && S) setTimeout(load, 0);   // llegó una convocatoria de horas extra
  if (!r.ok) { const e = new Error(j.e || 'No se pudo completar.'); e.done = j.done; throw e; }
  return j;
}
async function settleMarks() {   // antes de traer el estado del servidor, se guardan los toques pendientes
  if (MQ.items.p.length + MQ.items.x.length + MQ.items.w.length) await flushMarks();
  for (let i = 0; MQ.sending && i < 100; i++) await new Promise(r => setTimeout(r, 80));
}
// Una sola carga a la vez: si piden otra mientras una va en camino, al terminar se hace UNA más (con lo último) y
// quien esperaba recibe esa. Antes se podían encimar varias cargas cuando el servidor tardaba (y eso daba más lag).
let loadP = null, loadAgain = false;
function load() {
  if (loadP) { loadAgain = true; return loadP; }
  loadP = (async () => {
    try { do { loadAgain = false; await loadOnce(); } while (loadAgain); } finally { loadP = null; }
  })();
  return loadP;
}
function applyState(j) {   // pinta un estado del servidor (de la consulta, de la página o de la respuesta al entrar)
  skew = T(j.now) - Date.now(); S = j; lastLoad = Date.now(); offline = false; booted = true;
  if (j.v) pulse = j.v;   // ya tiene lo último: el pulso no la hace volver a cargar lo mismo
  paintOffline(); paintBg();
}
function sessionClosed() {   // cierre diario de sesiones: la terminal queda lista para quien la use al otro día
  loginErr = 'Las sesiones se cierran todos los días a las 10:00 p. m. Entra de nuevo con tu documento y PIN.';
  delete me.lastDoc; save(); pinBuf = ''; tab = 'inicio'; sheet = null;
}
async function loadOnce() {
  await settleMarks();
  try {
    applyState(await api('estado?snd=' + (soundOn() ? 1 : 0)));   // le cuenta al MES si este equipo puede sonar solo
  } catch (e) {
    booted = true;
    if (e.login) render();
    else if (!S) A.innerHTML = '<p class=k style="padding:20px">No hay conexión con el MES. Reintentando…</p>';
  }
}
// ---------------------------------------------------------------- marcar al instante (sin esperar al servidor)
// Cada toque sube el conteo en pantalla de inmediato. Los toques se guardan en segundo plano: los que llegan
// seguidos se mandan juntos en un solo envío, con la hora exacta de cada uno (el tiempo de ciclo sigue siendo real).
const MQ = {items: {p: [], x: [], w: []}, op: null, ean: '', sending: false, timer: 0, lastTap: 0, reconcile: 0};
// ---------------------------------------------------------------- referencias (EAN) del pedido
// Si el pedido tiene EAN cargados, cada unidad se reporta a una referencia y no se puede pasar de lo que falta de ella
// (como la app anterior). La referencia elegida se recuerda por operación en este equipo.
const refOf = t => t && t.ref && t.ref.mode === 'on' ? t.ref : null;
const normEan = v => { let c = String(v || '').replace(/\D/g, ''); if (c.length === 14 && c[0] === '0') c = c.slice(1); if (c.length === 12) c = '0' + c; return c; };
// El supernumerario decide el orden de trabajo y qué referencias están abiertas: a una cerrada no se le envían datos.
const refShut = i => i && i.on === false;
const refFull = i => i && i.left != null && i.left <= 0;
function curRef(t) {
  const r = refOf(t); if (!r) return null;
  let it = r.items.find(i => i.ean === ((me.ref || {})[t.id]));
  if (it && refShut(it)) it = null;   // la cerraron: hay que elegir otra
  if (!it) { const open = r.items.filter(i => !refShut(i) && !refFull(i)); if (open.length === 1) it = open[0]; }
  return it || null;
}
const refState = i => refShut(i) ? '🔒 Cerrada por el supernumerario' : refFull(i) ? '✔ Completa' : 'Faltan ' + (i.left == null ? 'sin límite' : q(i.left) + ' de ' + q(i.qty));
const refTxt = it => `${it.desc ? E(it.desc) + ' · ' : ''}faltan <b>${it.left == null ? 'sin límite' : q(it.left)}</b>${it.qty == null ? '' : ' de ' + q(it.qty)}`;
const refPending = t => t && t.ref && t.ref.mode === 'pending' ? `<div class=c style="padding:10px 14px"><small>⚠ Este pedido aún no tiene sus referencias (EAN) cargadas: se reporta sin referencia.</small></div>` : '';
function refBar(t) {   // pantallas de contador: la referencia que se está marcando
  const r = refOf(t); if (!r) return refPending(t);
  const it = curRef(t);
  const nOpen = r.items.filter(i => !refShut(i) && !refFull(i)).length, nShut = r.items.filter(refShut).length;
  return `<div class="c${it ? '' : ' al-ac'}" data-a=sheet data-v=ref style="cursor:pointer;padding:12px 16px"><div class=r2 style="flex-wrap:nowrap;align-items:center"><span style="min-width:0"><small>Referencia (EAN) que estoy marcando</small><b style="display:block;font-size:20px;letter-spacing:.03em">${it ? E(it.ean) : 'Elige o escanea la referencia'}</b><small>${it ? refTxt(it) : r.items.length + ' referencias en este pedido · ' + nOpen + ' abierta' + (nOpen === 1 ? '' : 's') + (nShut ? ' · ' + nShut + ' cerrada' + (nShut === 1 ? '' : 's') : '')}</small></span><button class=pr style="flex:none">${it ? 'Cambiar' : 'Elegir'}</button></div></div>`;
}
function refPicker(t, key = 'rref') {   // formularios por tramo: lista + escáner
  const r = refOf(t); if (!r) return refPending(t);
  const sel = (SV.rv || {})[key] || (curRef(t) || {}).ean || '';
  return `<label>Referencia (EAN)<select id=${key}><option value="">— Elige la referencia —</option>${r.items.map(i => `<option value="${i.ean}"${i.ean === sel && !refShut(i) ? ' selected' : ''}${refFull(i) || refShut(i) ? ' disabled' : ''}>${E(i.ean)}${i.desc ? ' · ' + E(i.desc) : ''} · ${refShut(i) ? 'cerrada' : 'faltan ' + (i.left == null ? 'sin límite' : q(i.left))}</option>`).join('')}</select></label>
   <label>…o escanéala<input id=rscan data-for=${key} inputmode=numeric autocomplete=off placeholder="Escanea el código de barras"></label>`;
}
function refSheet() {
  const t = S.task, r = refOf(t); if (!r) return '<b>Este pedido no tiene referencias.</b>';
  const cur = (curRef(t) || {}).ean;
  return `<b>Referencia (EAN)</b><small>Escanea el código o tócalo en la lista. Lo que marques irá a esa referencia.</small><input id=rscan data-for=pick inputmode=numeric autocomplete=off placeholder="Escanea aquí…" style="font-size:20px;text-align:center">` +
    r.items.map(i => `<button data-a=refpick data-v="${i.ean}"${refFull(i) || refShut(i) ? ' disabled' : ''} class="${i.ean === cur ? 'pr' : ''}" style="text-align:left"><b>${i.pos ? i.pos + '. ' : ''}${E(i.ean)}</b>${i.desc ? ' · ' + E(i.desc) : ''}<small style="display:block">${refState(i)}</small></button>`).join('');
}
function pickRef(ean) {
  const t = S.task, r = refOf(t); if (!r) return;
  const it = r.items.find(i => i.ean === normEan(ean));
  if (!it) { toast('Ese EAN no es de este pedido.'); return false; }
  if (it.left != null && it.left <= 0) { toast('Esa referencia ya está completa.'); return false; }
  if (refShut(it)) { toast('Esa referencia está cerrada: el supernumerario debe abrirla.'); return false; }
  if (MQ.items.p.length + MQ.items.x.length && MQ.ean !== it.ean) flushMarks();   // lo marcado antes va a su referencia
  me.ref = me.ref || {}; me.ref[t.id] = it.ean; save();
  document.activeElement && document.activeElement.blur();   // con el cursor en el escáner la pantalla no se repinta
  sheet = null; render(); toast('Referencia ' + it.ean, 'ok'); return true;
}
// ---------------------------------------------------------------- estándar: cuántas unidades deberían salir por hora
// Sale del tiempo estándar de la operación (minutos por unidad, por persona). Marca el ritmo que hay que sostener.
const perHour = (mpu, n = 1) => (+mpu > 0 ? 60 * n / +mpu : 0);
const rateTxt = v => v >= 100 ? q(Math.round(v)) : (Math.round(v * 10) / 10).toLocaleString('es');
function stdCard(t, n, doneHour, who) {   // n: personas; doneHour: lo hecho en la última hora (o null si no se sabe)
  const r = perHour(t.mpu, n); if (!r) return '';
  const pace = doneHour != null ? doneHour / r * 100 : null, each = 3600 / r;
  return `<div class="c std"><div class=r2><b style="font-size:17px">Estándar: ${rateTxt(r)} ${E(t.unit)}/hora</b><small>${who}</small></div><div class=om>
   <div><b>${rateTxt(r)}</b><small>Por hora</small></div><div><b>${rateTxt(r / 4)}</b><small>Cada 15 min</small></div><div><b>${mmss(each)}</b><small>1 unidad cada</small></div>
   ${pace != null ? `<div><b class="${cl(pace)}">${q(doneHour)} · ${Math.round(pace)}%</b><small>Última hora</small></div>` : ''}</div></div>`;
}
function tramoGoal(o, n) {   // supernumerario: lo que según el estándar debería salir en el tramo que va a reportar
  const r = perHour(o.mpu, n); if (!r || !o.period) return null;
  return Math.max(0, (now() - T(o.period.start)) / 36e5 * r);
}
const busyMarks = () => MQ.sending || MQ.items.p.length + MQ.items.x.length + MQ.items.w.length > 0 || Date.now() - MQ.lastTap < 2500;
function localMark(k, n) {
  const t = S.task, m = S.kind === 'linea' ? S.kpis : S.mine, at = new Date(now()).toISOString();
  if (k === 'p') {
    if (m.last_mark) m.cycle_s = (now() - T(m.last_mark)) / 1e3 / n;
    m.good_hour += n; m.good_today += n; m.good_month += n; m.last_mark = at;
    if (S.kind === 'linea') { m.line_task_good = (m.line_task_good || 0) + n; SV.takt = {t: now(), k: SV.takt ? SV.takt.k : 0}; }
    else {
      m.task_good += n;
      const pts = n * (t ? t.mpu : 0) * (S.pts_per_min || 10);
      m.points_hour += pts; m.points_day += pts;
    }
    if (t) { t.done += n; t.pending = Math.max(0, t.pending - n); }
    const it = curRef(t); if (it && it.left != null) { it.left = Math.max(0, it.left - n); it.done += n; }
  } else if (k === 'x') { m.scrap_today += n; if (t) t.scrap += n; }
  else m.rework_today += n;
}
function queueMark(k, n = 1) {
  if (!S.task) return toast(S.kind === 'linea' ? 'Esta línea aún no tiene una operación liberada.' : 'Aún no tienes una tarea asignada.');
  if (k === 'p' && (S.stop && !S.stop.is_break || (S.kind === 'linea' && S.line_stop))) return toast('Hay un paro abierto: reanuda para marcar.');
  if (k === 'p' && S.task.pending <= 0) return toast(S.kind === 'linea' ? 'La línea ya completó el 100% de esta operación.' : 'Ya completaste el 100% de esta tarea. No puedes marcar más unidades.', 'ok');
  if (k === 'p' && n > S.task.pending) n = S.task.pending;
  const it = curRef(S.task);
  if (refOf(S.task) && k === 'p') {
    if (!it) { sheet = 'ref'; render(); return toast('Primero elige o escanea la referencia (EAN).'); }
    if (it.left != null && it.left <= 0) { sheet = 'ref'; render(); return toast('La referencia ' + it.ean + ' ya está completa: elige otra.', 'ok'); }
    if (refShut(it)) { sheet = 'ref'; render(); return toast('La referencia ' + it.ean + ' está cerrada: elige otra.'); }
    if (it.left != null && n > it.left) n = it.left;
  }
  const ean = it ? it.ean : '';
  if (MQ.op !== null && (MQ.op !== S.task.id || MQ.ean !== ean)) flushMarks();   // cambió la tarea o la referencia
  MQ.op = S.task.id; MQ.ean = ean; MQ.lastTap = Date.now();
  const t = now();
  for (let i = 0; i < n; i++) MQ.items[k].push(t);
  localMark(k, n);
  SV.tap = Date.now();
  fb(k === 'p' && S.task.pending <= 0);
  paint();
  clearTimeout(MQ.timer);
  // si con este toque se completa la meta, se manda ya; si no, se esperan otros toques seguidos (máx. 0,4 s)
  MQ.timer = setTimeout(flushMarks, k === 'p' && S.task.pending <= 0 ? 0 : 400);
}
async function flushMarks() {
  clearTimeout(MQ.timer);
  if (MQ.sending) { MQ.timer = setTimeout(flushMarks, 150); return; }
  const op = MQ.op, ean = MQ.ean, batches = Object.entries(MQ.items).filter(([, v]) => v.length);
  if (!batches.length) return;
  MQ.items = {p: [], x: [], w: []}; MQ.sending = true;
  let failed = false;
  try {
    for (const [k, times] of batches) {
      const qty = times.length;
      try {
        const r = await api('marcar', {k, q: qty, op, t: times, e: ean});
        if (r.finished) showDone(r.task);
      } catch (e) {
        if (e.login) return;
        failed = true;
        toast(e.message, e.done ? 'ok' : '');
      }
    }
  } finally {
    MQ.sending = false;
  }
  const more = MQ.items.p.length + MQ.items.x.length + MQ.items.w.length;
  if (more) { MQ.timer = setTimeout(flushMarks, 0); return; }
  // Cuando se deja de marcar se trae el estado real (eficiencia, puntos…); si algo falló, de inmediato
  clearTimeout(MQ.reconcile);
  MQ.reconcile = setTimeout(() => { if (!busyMarks() || failed) load(); }, failed ? 0 : 1800);
}
// Pintar en el siguiente cuadro (varios toques seguidos = un solo repintado)
let rafPending = false;
function paint() { if (rafPending) return; rafPending = true; requestAnimationFrame(() => { rafPending = false; render(); }); }
window.addEventListener('pagehide', () => { if (MQ.items.p.length + MQ.items.x.length + MQ.items.w.length) flushMarks(); });
// Repintado en segundo plano: nunca mientras el dedo está sobre la pantalla (si la pantalla se redibuja en ese
// instante, el botón que se estaba tocando se reemplaza y el toque se pierde). Espera a que suelte.
let PDOWN = 0;
document.addEventListener('pointerdown', () => { PDOWN = Date.now(); }, {capture: true, passive: true});
['pointerup', 'pointercancel'].forEach(ev => document.addEventListener(ev, () => setTimeout(() => { PDOWN = 0; }, 60), {capture: true, passive: true}));
function paintBg() {
  clearTimeout(paintBg.t);
  if (PDOWN && Date.now() - PDOWN < 1500) { paintBg.t = setTimeout(paintBg, 100); return; }
  render();
}

let polling = false;
async function poll() {
  if (document.hidden || polling) return;   // una consulta a la vez (no se encimen si el servidor tarda)
  if (!S) { if (offline) { polling = true; try { await load(); } finally { polling = false; } } return; }
  if (busyMarks()) return;                          // mientras se marca, la pantalla manda (se sincroniza al terminar)
  polling = true;
  try {
    const r = await fetch(CFG.pulse + '?k=' + encodeURIComponent((S.pulse || []).join(',')), {credentials: 'same-origin'});
    const j = await r.json(); offline = false; paintOffline();
    // las pantallas con mucha gente (línea, supernumerario) recargan como mucho cada 2 s; la del operario cada 1 s
    const gap = S.kind === 'operario' || S.kind === 'patinador' ? 1000 : 2000;
    if ((j.v !== pulse && Date.now() - lastLoad >= gap) || Date.now() - lastLoad > 20000) await load();
  } catch (e) { offline = true; paintOffline(); } finally { polling = false; }
}
function paintOffline() {
  let b = $('#off');
  if (offline && !b) { b = document.createElement('div'); b.id = 'off'; b.className = 'offline'; b.textContent = 'Sin conexión con el MES · reintentando…'; document.body.appendChild(b); }
  if (!offline && b) b.remove();
}

// ---------------------------------------------------------------- ajustes personales (en este dispositivo)
const ukey = () => S ? S.kind + ':' + S.who.id : '';
const CF = () => ((me.cfgs || {})[ukey()]) || {};
const ACC = {morado: '#5b3fd1', azul: '#2f7cf6', verde: '#12a150', naranja: '#e8710a', rosa: '#d6336c'};
let WL = null;
function applyCfg() {
  const c = CF(), R = document.documentElement, a = ACC[c.acc];
  if (c.theme === 'dark' || c.theme === 'light') R.dataset.theme = c.theme; else delete R.dataset.theme;
  let s = $('#acs'); if (!s) { s = document.createElement('style'); s.id = 'acs'; document.head.appendChild(s); }
  s.textContent = a ? `:root{--ac:${a};--a2:color-mix(in srgb,${a} 55%,#fff);--rc:color-mix(in srgb,${a} 80%,#fff)}:root[data-theme=light]{--a2:${a}}` : '';
  A.style.zoom = c.sz || '';
  const w = !!(c.awake && navigator.wakeLock);
  if (w && !WL) navigator.wakeLock.request('screen').then(l => { WL = l; l.addEventListener('release', () => { WL = null; }); }).catch(() => {});
  else if (!w && WL) { WL.release(); WL = null; }
}
const setCF = (k, v) => { me.cfgs = me.cfgs || {}; const c = me.cfgs[ukey()] = me.cfgs[ukey()] || {}; if (v === '' || v == null || v === false) delete c[k]; else c[k] = v; save(); applyCfg(); };
const dnm = () => CF().nick || (S ? S.who.name : '');
function fb(ok) {
  const c = CF();
  if (c.vib && navigator.vibrate) navigator.vibrate(ok ? [40, 60, 40, 60, 120] : 40);
  if (c.snd && soundOn()) try {   // con el audio dormido no se encola nada (sonaría tarde)
    const x = AC, t = x.currentTime;
    (ok ? [660, 880, 1175] : [880]).forEach((f, i) => { const o = x.createOscillator(), g = x.createGain(), s = t + i * .14; o.frequency.value = f; o.connect(g); g.connect(x.destination); g.gain.setValueAtTime(.15, s); g.gain.exponentialRampToValueAtTime(.001, s + .12); o.start(s); o.stop(s + .13); });
  } catch (e) { /* sin audio */ }
}

// ---------------------------------------------------------------- piezas comunes
// [clave, nombre, nombre corto para celular]
const TABS = list => `<div class=otb style="grid-template-columns:repeat(${list.length},minmax(0,1fr))">${list.map(([k, n, s]) => `<button class="${tab === k ? 'on' : ''}" data-a=tab data-v=${k} title="${n}">${svg(k)}<span>${s ? `<span class=tl>${n}</span><span class=ts>${s}</span>` : n}</span></button>`).join('')}</div>`;
const OPT = [['inicio', 'Inicio'], ['historial', 'Historial'], ['cola', 'Cola'], ['ficha', 'Ficha técnica', 'Ficha'], ['ajustes', 'Ajustes']];
const MOT = [['inicio', 'Inicio'], ['historial', 'Historial'], ['cola', 'Cola'], ['ajustes', 'Ajustes']];
const SUP = [['inicio', 'Reportar'], ['material', 'Material'], ['personal', 'Personal'], ['historial', 'Reportes'], ['lineas', 'Líneas'], ['ajustes', 'Ajustes']];
const head = () => `<nav class=top><span class=br>${S.kind === 'linea' ? 'Línea ' + E(S.who.code) : E(dnm())}<small>${S.kind === 'linea' ? E(S.who.name) + ' · ' + E(S.who.site) : S.kind === 'patinador' ? 'Patinador' + ((S.assigned || []).length ? ' · ' + E([...new Set(S.assigned.map(a => a.line).filter(Boolean))].join(', ')) : S.who.position ? ' · ' + E(S.who.position) : '') : S.kind === 'supernumerario' ? 'Supernumerario' + (S.who.supervisor ? ' · supervisor ' + E(S.who.supervisor) : '') : E(S.who.shift || S.who.position || 'Operario')}</small></span><b class=n2 id=clk style="font-size:20px"></b></nav>`;
const MB = (stopOpen, disabled) => `<div class=mbar><button data-a=reload title="Actualizar">${svg('refresh')}</button><button data-a=sheet data-v=rej title="Rechazo, reproceso o varias unidades">${svg('down')}</button><button class=pr data-a=mark${stopOpen || disabled ? ` disabled title="${E(stopOpen ? 'Hay un paro abierto' : disabled)}"` : ' title="Marcar una unidad"'}>＋</button><button data-a=flag class="${stopOpen ? 'al-rd' : ''}" title="${stopOpen ? 'Reanudar' : 'Paro'}">${svg(stopOpen ? 'play' : 'flag')}</button><button data-a=sheet data-v=msg title="Mensajes">${svg('chat')}</button></div>`;
const taskLine = t => `${E(t.order)} · ${E(t.product)}${t.customer ? ' · ' + E(t.customer) : ''}`;
const stopCard = () => {
  const st = S.stop, ls = S.line_stop;
  if (st && !st.is_break) return `<div class="c al-rd"><div class=k style="color:#fff">PARO EN CURSO · ${E(st.cause)}</div><div class=tmr data-lt="${st.start}">${fmt((now() - T(st.start)) / 1e3)}</div><small style="display:block;text-align:center">minutos : segundos · toca Reanudar para cerrar el paro</small></div>`;
  if (ls) return `<div class="c al-ac"><b>Paro de la línea: ${E(ls.cause)}</b> · <b data-lt="${ls.start}">${fmt((now() - T(ls.start)) / 1e3)}</b></div>`;
  return '';
};
const pendingMsg = () => (S.messages || []).find(m => S.kind !== 'linea' || !(me.seen || []).includes(m.id));
const msgOverlay = () => {
  const m = pendingMsg();
  return m ? `<div class="sh${m.alarm !== false ? ' alarm' : ''}"><div class=c><div class=nt style="font-size:clamp(18px,4.5vw,24px)">🔔 <b style="color:#5b3fd1">Mensaje de ${E(m.from)}</b> <small style="color:#555">${hT(m.at)}</small><br>${E(m.text)}</div><button class=pr data-a=accept data-v="${m.id}" style="min-height:64px;font-size:22px">Aceptar</button></div></div>` : '';
};
const opCard = (tag, t, sub, extra = '') => `<div class=c${extra}><div class=r2><div style="min-width:0"><span class=chip>${tag}</span><div style="font-size:18px;font-weight:700;margin-top:6px">${E(t.name)}</div><small>${taskLine(t)}</small></div><b class=n2>${q(t.pending)} <small>${E(t.unit)}</small></b></div>${sub}</div>`;
const progress = t => `<div class=pb><i style="width:${t.qty > 0 ? Math.min(100, t.done / t.qty * 100) : 0}%"></i></div><small style="display:block;margin-top:6px">${q(t.done)} de ${q(t.qty)} ${E(t.unit)} · faltan ${q(t.pending)}${t.start ? ' · programada ' + dT(t.start) + ' ' + hT(t.start) : ''}</small>`;

// ---------------------------------------------------------------- hojas (menús emergentes)
function sheetHtml() {
  if (!sheet) return '';
  let body = '';
  if (sheet === 'rej') body = `<b>¿Qué vas a marcar?</b><button data-a=mark data-k=x>Rechazo</button><button data-a=mark data-k=w>Reproceso</button><button data-a=sheet data-v=qty>Varias unidades buenas…</button>`;
  else if (sheet === 'eans') body = eanSheet();
  else if (sheet === 'qty') body = `<b>¿Cuántas unidades buenas?</b><small>${S.task ? 'Faltan ' + q(S.task.pending) + ' ' + E(S.task.unit) : ''}</small><input id=qn type=number inputmode=decimal min=1 step=any placeholder="Cantidad" style="font-size:22px;text-align:center"><button class=pr data-a=markqty>Marcar</button>`;
  else if (sheet === 'smsg') {
    const QK = ['Reporten la producción, por favor', 'Regresen a sus puestos', 'Revisen la calidad del producto', 'Cambio de referencia en 10 minutos', 'Acérquese al supervisor'];
    body = `<b>Mensaje para: ${E((SV.msg || {}).label || '')}</b><textarea id=smt rows=3 maxlength=500 placeholder="Escribe el mensaje…" style="font-size:18px;padding:10px">${rv('smt')}</textarea><div class=sel>${QK.map(t => `<button type=button class=sm data-a=quick>${E(t)}</button>`).join('')}</div>
      <label class=swt><span>🔔 Con alarma (zumbido)<small style="display:block">Suena y vibra en su pantalla hasta que lo acepte. Sin alarma, le aparece en silencio.</small></span><input type=checkbox id=sma${SV.noAlarm ? '' : ' checked'}></label>
      <div style="display:grid;grid-template-columns:1fr auto;gap:8px"><button class=pr data-a=smsgsend style="min-height:52px;font-size:18px">📣 Enviar</button><button data-a=buzz style="min-height:52px" title="Sin escribir nada: le suena la alarma con «¡Atención! Mira la pantalla»">🔔 Solo zumbido</button></div>`;
  }
  else if (sheet === 'stop') body = `<b>${(SV.emp || SV.wc) && SV.empName ? E(SV.empName) : S.kind === 'linea' ? 'Paro de toda la línea' : 'Motivo del paro'}</b><small>Elige el motivo</small>` + S.causes.map(c => `<button data-a=stop data-v="${E(c)}">${E(c)}</button>`).join('') +
    (S.kind === 'operario' ? S.breaks.filter(b => !b.taken).map(b => `<button class=pr data-a=brk data-v="${b.kind}">🍴 Salir a ${b.kind.toLowerCase()}</button>`).join('') : '');
  else if (sheet === 'who') {
    const crew = S.crew || [];
    body = '<b>¿Quién se para o regresa?</b>' + crew.map(o => `<button data-a=crewstop data-v="${o.id}"${o.stop ? ' class=al-ac' : ''}>${E(o.name)}${o.stop ? ' · ' + E(o.stop.cause) + (o.stop.is_break ? ' (pausa)' : ' · reanudar') : ''}</button>`).join('') +
      (S.line_stop ? `<button class=al-rd data-a=close data-v="${S.line_stop.id}">Reanudar la línea (${E(S.line_stop.cause)})</button>` : '<button data-a=linestop>⏸ Paro de toda la línea</button>') +
      (crew.length ? '' : '<small>Esta línea aún no tiene cuadrilla asignada en el MES.</small>');
  } else if (sheet === 'msg') {
    const L = me.msgs || [];
    body = '<b>Mensajes de hoy</b>' + (L.filter(m => T(m.at) >= T0()).map(m => `<div class=nt><b style="color:#5b3fd1">${E(m.from)}</b> <small style="color:#555">${hT(m.at)}</small><br>${E(m.text)}</div>`).join('') || '<small>Sin mensajes hoy.</small>');
    if (S.kind === 'linea') body = '<b>Mensajes a la línea (hoy)</b>' + ((S.messages || []).map(m => `<div class=nt><b style="color:#5b3fd1">${E(m.from)}</b> <small style="color:#555">${hT(m.at)}</small><br>${E(m.text)}</div>`).join('') || '<small>Sin mensajes hoy.</small>');
  } else if (sheet === 'pmov') {
    const M = S.kind === 'supernumerario' ? (S.material || {}) : S, ln = S.kind === 'supernumerario' ? (M.lines || []).find(l => String(l.id) === String(SV.pwc)) : null;
    const dm = ln ? ln.default_mover : S.default_mover, movers = M.movers || [], dname = (movers.find(m => m.id === dm) || {}).name;
    body = `<b>Pedir material${ln ? ' · ' + E(ln.code) : ''}</b><select id=pmo><option value="">¿De dónde? (opcional)</option>${(M.locations || []).map(l => `<option value="${l.id}">${E(l.name)}</option>`).join('')}</select><input id=pme type=number min=1 max=99 placeholder="Estibas (1)"><input id=pmn maxlength=200 placeholder="¿Qué necesitas? (material, referencia…)">
      <label style="display:grid;gap:4px;font-size:13px;color:var(--mu)">¿A qué patinador?<select id=pmm>${dm ? `<option value="">Al de la línea: ${E(dname || '')}</option>` : ''}<option value="0"${dm ? '' : ' selected'}>Abierto: el primer patinador libre</option>${movers.filter(m => m.id !== dm).map(m => `<option value="${m.id}">${E(m.name)}${m.lines.length ? ' · ' + E(m.lines.join(', ')) : ''}</option>`).join('')}</select></label>
      <select id=pmp><option value=0>Normal</option><option value=1>⚡ Urgente: la línea está parada</option></select><button class=pr data-a=pmsend>Enviar pedido</button>`;
  } else if (sheet === 'pinc') body = '<b>Incidencia en el traslado</b>' + S.incidents.map(i => `<button data-a=minc data-v="${E(i)}">${E(i)}</button>`).join('') + (S.current && S.current.incident ? '<button data-a=minc data-v="">Quitar incidencia</button>' : '');
  else if (sheet === 'unpair') body = `<b>¿Desvincular esta tablet de la línea ${E(S.who.code)}?</b><small>Para volver a usarla en la línea necesitarás su código (lo tiene el jefe de planta en «Tablets y QR»).</small><button class=al-rd data-a=logout>Sí, desvincular</button>`;
  else if (sheet === 'rep') body = repSheet();
  else if (sheet === 'ref') body = refSheet();
  return `<div class=sh data-a=sheet data-v=""><div class="c${sheet === 'rep' || sheet === 'eans' ? ' wide' : ''}" data-a=nop>${body}<button data-a=sheet data-v="">Cerrar</button></div></div>`;
}

// ---------------------------------------------------------------- reporte por tramos (líneas continuas)
// Contador = un toque por unidad · Intervalo = cada operario escribe lo suyo al llegar el corte · Supernumerario = el
// asistente del supervisor (o la tablet de la línea) escribe el total de la línea y se reparte entre el personal.
const MODE_TXT = {contador: 'Contador por toque', intervalo: 'Cada operario reporta', supernumerario: 'Reporta el supernumerario'};
const TAGS = {mes: 'Asignada en el MES', tablet: 'Elegida por mí', supervisor: 'De mi supervisor'};
const rv = k => E((SV.rv || {})[k] ?? '');   // lo escrito en un formulario sobrevive a las actualizaciones de la pantalla
const RB = stopOpen => `<div class=mbar><button data-a=reload title="Actualizar">${svg('refresh')}</button><button data-a=flag class="${stopOpen ? 'al-rd' : ''}" title="${stopOpen ? 'Reanudar' : 'Paro'}">${svg(stopOpen ? 'play' : 'flag')}</button><button data-a=sheet data-v=msg title="Mensajes">${svg('chat')}</button></div>`;
const cdown = (p, label, alert = true) => ring(0, 1, 'var(--a2)', `<b class="n2 cd" data-due="${E(p.due)}" data-st="${E(p.start)}"${alert ? '' : ' data-noalert=1'}>--:--</b><small>${label}</small><hr><small>Corte a las <b>${hT(p.due)}</b><br>cada ${p.interval} min</small>`);
function splitQ(total, list) {   // mismo reparto que el servidor: proporcional y sin perder unidades
  const out = {}; list.forEach(x => { out[x.id] = 0; });
  if (!(total > 0) || !list.length) return out;
  let pos = list.filter(x => x.w > 0); if (!pos.length) pos = list.map(x => ({id: x.id, w: 1}));
  const sw = pos.reduce((a, x) => a + x.w, 0), unit = Number.isInteger(total) ? 1 : 0.001;
  const raw = pos.map(x => { const r = total * x.w / sw; return {id: x.id, w: x.w, r, b: Math.floor(r / unit + 1e-9) * unit}; });
  const rest = Math.round((total - raw.reduce((a, x) => a + x.b, 0)) / unit);
  raw.slice().sort((a, b) => (b.r - b.b) - (a.r - a.b) || b.w - a.w).slice(0, Math.max(0, rest)).forEach(x => { x.b += unit; });
  raw.forEach(x => { out[x.id] = Math.round(x.b * 1000) / 1000; });
  return out;
}
const taskHead = t => `<h2 style="font-size:clamp(22px,5vw,30px)">${E(t.name)}</h2><b style="display:block;margin-top:8px">Pedido ${E(t.order)}</b><small style="font-size:14px">${E(t.product)}${t.customer ? ' · ' + E(t.customer) : ''}</small>
   <div class=pb style="max-width:300px"><i style="width:${t.qty > 0 ? Math.min(100, t.done / t.qty * 100) : 0}%"></i></div><small>Meta de la operación · ${q(t.done)} / ${q(t.qty)} ${E(t.unit)}</small>`;
const shareTxt = (R, t, mine) => R && R.share != null ? `<small style="display:block;margin-top:6px">Tu parte de la carga: <b>${q(R.share)}</b> de ${q(t.qty)} ${E(t.unit)} (${R.people} persona${R.people === 1 ? '' : 's'}) · llevas <b>${q(mine)}</b></small>` : '';
const opKpis = m => `<div class=g4><div class=kt><small>Eficiencia hora</small><b class="${cl(m.eff_hour)}">${P1(m.eff_hour)}</b></div><div class=kt><small>Eficiencia diaria</small><b class="${cl(m.eff_day)}">${P1(m.eff_day)}</b></div>
   <div class=kt><small>Puntos individuales</small><p class=p3><span class=bl>H</span><span class=bl>+${nf(m.points_hour)}</span></p><p class=p3><span class=gr>D</span><span class=gr>+${nf(m.points_day)}</span></p><p class=p3><span title="Acumulado del mes">A</span><span>$${nf(m.money_month)}</span></p></div>
   <div class=kt><small>Mis unidades</small><p class=p3><span>Hoy</span><b>${q(m.good_today)}</b></p><p class=p3><span>Mes</span><b>${q(m.good_month)}</b></p></div></div>`;
function repList(list, title = 'Reportes de hoy') {
  list = list || [];
  if (!list.length) return `<div class=c><b>${title}</b><small style="display:block;margin-top:6px">Aún no hay reportes hoy.</small></div>`;
  return `<div class=c><b>${title}</b>${list.map(r => `<div class=r3 style="font-size:13px;align-items:center"><span><b style="font-size:14px">${hT(r.start)}–${hT(r.end)}</b> · ${E(r.op)}${r.line ? ' · ' + E(r.line) : ''}<small style="display:block">${E(r.kind_label)}${r.people > 1 ? ' · ' + r.people + ' personas' : ''}${r.scrap ? ' · ' + q(r.scrap) + ' rechazos' : ''}</small></span><span style="display:flex;gap:8px;align-items:center;flex:none"><b>${q(r.good)}</b>${r.undo ? `<button class=sm data-a=undo data-v="${r.id}" title="Se puede anular durante 30 minutos">Anular</button>` : ''}</span></div>`).join('')}</div>`;
}
// Operario de una línea que reporta por tramos: cuenta regresiva y formulario
function opInterval() {
  const t = S.task, m = S.mine, R = S.report, p = R.period, st = S.stop && !S.stop.is_break ? S.stop : null;
  const goal = R.share != null ? R.share : t.qty;
  return `${stopCard()}
  <div class=c>${taskHead(t)}${shareTxt(R, t, m.task_good)}
   <hr style="border:0;border-top:1px solid var(--bd);margin:14px 0 0">
   <div class=rr style="padding-bottom:10px"><div class=rw>${cdown(p, 'Próximo reporte')}</div>
    <div class=rw>${ring(m.task_good, goal, 'var(--rc)', `<b class="n a2">${q(m.task_good)}</b><small>Llevo en esta tarea</small><hr><div class=r4><span><b>${q(Math.max(0, goal - m.task_good))}</b><small>Para mi parte</small></span><span><b>${q(m.good_today)}</b><small>Hoy</small></span></div>`)}</div></div></div>
  <div class=c><b style="font-size:18px">Reportar lo que hice</b><small style="display:block;margin-top:4px">Desde las <b>${hT(p.start)}</b> hasta ahora · faltan ${q(t.pending)} ${E(t.unit)} de la operación</small>
   ${refOf(t) ? `<div class=rf>${refPicker(t)}</div>` : refPending(t)}
   <div class=rf><label>Buenas<input id=rg type=number inputmode=decimal min=0 step=any placeholder=0 value="${rv('rg')}"></label><label>Rechazos<input id=rx type=number inputmode=decimal min=0 step=any placeholder=0 value="${rv('rx')}"></label>${refOf(t) ? `<label>Códigos sobrantes<input id=rl type=number inputmode=decimal min=0 step=any placeholder=0 value="${rv('rl')}"></label>` : ''}</div>
   <button class=pr style="width:100%;min-height:56px;font-size:18px" data-a=repown>Enviar reporte</button></div>
  ${stdCard(t, 1, null, 'por persona')}${opKpis(m)}${repList(R.reports, 'Mis reportes de hoy')}${RB(!!st)}`;
}
// Operario de una línea que reporta el supernumerario: solo ve su parte, lo que le han reportado y registra paros
function opSuper() {
  const t = S.task, m = S.mine, R = S.report, st = S.stop && !S.stop.is_break ? S.stop : null;
  const goal = R.share != null ? R.share : t.qty;
  return `${stopCard()}
  <div class=c>${taskHead(t)}${shareTxt(R, t, m.task_good)}
   <hr style="border:0;border-top:1px solid var(--bd);margin:14px 0 0">
   <div class=rr style="padding-bottom:10px"><div class=rw>${ring(m.task_good, goal, 'var(--rc)', `<b class="n a2">${q(m.task_good)}</b><small>Me han reportado</small><hr><div class=r4><span><b>${q(Math.max(0, goal - m.task_good))}</b><small>Para mi parte</small></span><span><b>${q(m.good_today)}</b><small>Hoy</small></span></div>`)}</div>
    <div class=rw>${R.period ? cdown(R.period, 'Próximo reporte del supernumerario', false) : ''}</div></div>
   <small style="display:block">En esta línea el supernumerario reporta cada ${R.interval} min lo producido y se reparte ${R.split === 'iguales' ? 'en partes iguales' : 'según el tiempo que trabajó cada uno (sin paros ni pausas)'}. Tú solo registra tus paros.</small></div>
  ${stdCard(t, 1, null, 'por persona')}${opKpis(m)}${RB(!!st)}`;
}
// Lo que no está en el programador (p. ej. el saldo de un pedido) se ve, pero bloqueado: no se reporta ni suena su corte.
const isLocked = o => o && o.scheduled === false;
const LOCK_TXT = 'Aún no está en el programador: se podrá reportar cuando planeación la programe.';
function lockedCard(tag, o) {
  return `<div class="c locked"><div class=r2 style="flex-wrap:nowrap;align-items:flex-start"><div style="min-width:0"><span class="chip lk">🔒 Sin programar</span>${tag ? `<span class=chip>${E(tag)}</span>` : ''}<div style="font-size:18px;font-weight:700;margin-top:6px">${E(o.name)}</div><small>${taskLine(o)}</small></div><b class=n2>${q(o.pending)} <small>${E(o.unit)}</small></b></div>
   <small style="display:block;margin-top:8px">${LOCK_TXT}</small>
   <button disabled style="width:100%;margin-top:12px;min-height:48px">🔒 No se puede reportar</button></div>`;
}
function waitSched() {   // hay operaciones asignadas, pero ninguna está en el programador todavía
  const lk = (S.queue || []).filter(isLocked);
  return lk.length ? `<div class=c><b>Sin operación programada</b><small style="display:block;margin-top:6px">Hay ${lk.length} operación${lk.length === 1 ? '' : 'es'} en espera de programación. Se verán y se podrán reportar cuando planeación las meta al programador.</small></div>${lk.map(x => lockedCard('', x)).join('')}` : '';
}
function opReportCard(ln, o, alert) {
  if (isLocked(o)) return lockedCard(MODE_TXT[o.mode] || o.mode, o);
  const p = o.period, crew = o.crew_list || [];
  return `<div class=c><div class=r2 style="flex-wrap:nowrap;align-items:flex-start"><div style="min-width:0"><span class=chip>${E(MODE_TXT[o.mode] || o.mode)}</span><div style="font-size:18px;font-weight:700;margin-top:6px">${E(o.name)}</div><small>${taskLine(o)}</small></div>
     <div style="text-align:right;flex:none"><b class="n2 cd" data-due="${E(p.due)}" data-st="${E(p.start)}"${alert ? '' : ' data-noalert=1'}>--:--</b><small style="display:block">corte ${hT(p.due)} · cada ${p.interval} min</small></div></div>
   ${progress(o)}
   ${(() => { const n = crew.length || o.crew || 1, r = perHour(o.mpu, n), g = tramoGoal(o, n); return r ? `<div class=stdl><span>Estándar <b>${rateTxt(r)} ${E(o.unit)}/h</b> · ${n} persona${n === 1 ? '' : 's'} × ${rateTxt(perHour(o.mpu))}</span><span>En este tramo deberían ir <b>≈ ${q(Math.round(g))}</b></span></div>` : ''; })()}
   ${refOf(o) ? (() => { const it = o.ref.items, op_ = it.filter(i => !refShut(i) && !refFull(i)).length, sh = it.filter(refShut).length, fu = it.filter(refFull).length; return `<button class=refctl data-a=eans data-v="${o.id}"><span>Referencias (EAN): <b>${op_} abierta${op_ === 1 ? '' : 's'}</b>${sh ? ' · ' + sh + ' cerrada' + (sh === 1 ? '' : 's') : ''}${fu ? ' · ' + fu + ' completa' + (fu === 1 ? '' : 's') : ''} de ${it.length}</span><span>Ordenar · abrir · cerrar ›</span></button>`; })() : ''}
   <small style="display:block;margin-top:6px">Reportado hoy: <b>${q(o.reported_today)}</b>${p.last ? ' · último reporte ' + hT(p.last) : ' · aún sin reportes hoy'} · ${crew.length} persona${crew.length === 1 ? '' : 's'}${o.share != null ? ' · parte de cada uno: <b>' + q(o.share) + '</b>' : ''}</small>
   <div class=crw>${crew.map((c, i) => `<span class=cp>${avc(c.name, i, '28px', c.stop ? 'var(--rd)' : '')}<span>${E(c.name.split(' ')[0])}<small>${q(c.units)}${o.share != null ? ' / ' + q(c.share) : ''}${c.stop ? ' · ' + E(c.stop.cause) : ''}</small></span></span>`).join('') || '<small>Sin cuadrilla asignada: el reporte queda para la línea.</small>'}</div>
   <button class=pr style="width:100%;margin-top:12px;min-height:54px;font-size:18px" data-a=rep data-v="${o.id}">Reportar ${hT(p.start)} → ahora</button></div>`;
}
const linePanel = (ln, alert) => `<div class=lnh><b style="font-size:20px">${E(ln.code)} · ${E(ln.name)}</b>${(ln.tags || []).map(t => `<span class=chip>${E(TAGS[t] || t)}</span>`).join('')}${ln.stop ? `<span class="chip rd">Paro: ${E(ln.stop.cause)}</span>` : ''}</div>` +
  (ln.ops.length ? ln.ops.map(o => opReportCard(ln, o, alert)).join('') : '<div class=c><small>Sin operaciones liberadas en esta línea.</small></div>');
function findRep() {
  const r = SV.rep; if (!r) return null;
  for (const ln of (S.kind === 'supernumerario' ? S.lines : S.panel ? [S.panel] : []) || []) {
    const o = ln.ops.find(x => String(x.id) === String(r.op)); if (o) return {ln, o};
  }
  return null;
}
function repSheet() {
  const f = findRep(); if (!f) return '<b>Esa operación ya no está abierta.</b>';
  const {ln, o} = f, crew = o.crew_list || [], how = crew.length ? SV.rep.how : 'total';
  const gq = tramoGoal(o, crew.length || o.crew || 1);
  let body = `<b style="font-size:18px">Reportar · ${E(o.name)}</b><small>${E(ln.code)} · pedido ${E(o.order)} · tramo ${hT(o.period.start)} → ahora · faltan ${q(o.pending)} ${E(o.unit)}${gq != null ? ` · según el estándar deberían ser <b>≈ ${q(Math.round(gq))}</b>` : ''}</small>`;
  if (refOf(o)) body += `<div class=rf>${refPicker(o)}<label>Códigos sobrantes<input id=rl type=number inputmode=decimal min=0 step=any placeholder=0 value="${rv('rl')}"></label></div>`;
  else body += refPending(o);
  if (crew.length) body += `<div class=seg><button type=button class="${how === 'total' ? 'on' : ''}" data-a=rephow data-v=total>Total de la línea</button><button type=button class="${how === 'persona' ? 'on' : ''}" data-a=rephow data-v=persona>Por persona</button></div>`;
  if (how === 'persona') body += `<div class=pp>${crew.map(c => `<div class=ppr><span>${E(c.name)}<small>${c.stop ? E(c.stop.cause) + ' · ' : ''}${Math.round(c.w)} min trabajados</small></span><input type=number inputmode=decimal min=0 step=any placeholder="Buenas" data-pg="${c.id}" value="${rv('g' + c.id)}"><input type=number inputmode=decimal min=0 step=any placeholder="Rech." data-px="${c.id}" value="${rv('x' + c.id)}"></div>`).join('')}</div>`;
  else body += `<div class=rf><label>Buenas de la línea<input id=rg type=number inputmode=decimal min=0 step=any placeholder=0 value="${rv('rg')}"></label><label>Rechazos<input id=rx type=number inputmode=decimal min=0 step=any placeholder=0 value="${rv('rx')}"></label></div>` +
    (crew.length ? `<small>${o.split === 'iguales' ? 'Se reparte en partes iguales' : 'Se reparte según el tiempo trabajado en el tramo (sin paros ni pausas)'}:</small><div class=pp>${crew.map(c => `<div class=ppr><span>${E(c.name)}<small>${Math.round(c.w)} min trabajados${c.stop ? ' · ' + E(c.stop.cause) : ''}</small></span><b data-sh="${c.id}">–</b></div>`).join('')}</div>` : '<small>Sin cuadrilla asignada: queda para la línea.</small>');
  return body + `<button class=pr data-a=repsend style="min-height:54px;font-size:18px">Enviar reporte</button>`;
}
function eanSheet() {   // supernumerario: orden de trabajo y referencias abiertas / cerradas de una operación
  const f = findRep() || (() => { for (const ln of S.lines || []) { const o = ln.ops.find(x => String(x.id) === String(SV.eop)); if (o) return {ln, o}; } return null; })();
  if (!f || !refOf(f.o)) return '<b>Esa operación ya no está abierta.</b>';
  const {ln, o} = f, it = o.ref.items;
  const row = (i, k) => `<div class="eanr${refShut(i) ? ' shut' : ''}${refFull(i) ? ' full' : ''}"><span class=en><b>${i.pos}. ${E(i.ean)}</b>${i.desc ? `<small>${E(i.desc)}</small>` : ''}<small>${q(i.done)}${i.qty != null ? ' / ' + q(i.qty) : ''} · ${refShut(i) ? '🔒 Cerrada' : refFull(i) ? '✔ Completa' : i.done > 0 ? 'En proceso' : 'Abierta'}</small></span>
    <span class=eb><button data-a=eanc data-k=subir data-v="${i.ean}"${k === 0 ? ' disabled' : ''} title="Subir">↑</button><button data-a=eanc data-k=bajar data-v="${i.ean}"${k === it.length - 1 ? ' disabled' : ''} title="Bajar">↓</button>
    ${refFull(i) ? '' : refShut(i) ? `<button class=pr data-a=eanc data-k=abrir data-v="${i.ean}">Abrir</button>` : `<button data-a=eanc data-k=cerrar data-v="${i.ean}">Cerrar</button>`}
    ${refFull(i) ? '' : `<button data-a=eanc data-k=solo data-v="${i.ean}" title="Abre esta y cierra las demás">Solo esta</button>`}</span></div>`;
  return `<b style="font-size:18px">Referencias · ${E(o.name)}</b><small>${E(ln.code)} · pedido ${E(o.order)}. El orden es el de trabajo. A una referencia cerrada nadie le puede enviar datos hasta que la abras; al llegar al 100 % se cierra sola.</small>
   <div style="display:flex;gap:8px;flex-wrap:wrap"><button data-a=eanc data-k=todas data-v="">Abrir todas</button></div>
   <div class=eanl>${it.map(row).join('')}</div>`;
}
function repPreview() {
  const f = findRep(); if (!f) return;
  const v = +(($('#rg') || {}).value || 0), crew = f.o.crew_list || [];
  const sh = splitQ(v, crew.map(c => ({id: c.id, w: f.o.split === 'iguales' ? 1 : c.w})));
  crew.forEach(c => { const el = $(`[data-sh="${c.id}"]`); if (el) el.textContent = v > 0 ? q(sh[c.id]) : '–'; });
}
function supHome() {
  const L = S.lines || [];
  if (!L.length) return `<div class=c><b>Aún no tienes líneas</b><small style="display:block;margin-top:6px">Elige en «Líneas» por cuáles reportas, o pídele a tu supervisor que te las asigne en el MES (Control de piso → Forma de reporte).</small><button class=pr data-a=tab data-v=lineas style="margin-top:12px;width:100%">Elegir líneas</button></div>`;
  return `<h2>Mis líneas</h2><small>Reporta lo producido en cada tramo. El total se reparte entre el personal de la etapa; también puedes escribirlo por persona.</small>${L.map(ln => linePanel(ln, true)).join('')}`;
}
const supHist = () => `<h2>Mis reportes</h2>${repList(S.reports, 'Hoy')}`;
function supLines() {
  const L = S.all_lines || [];
  return `<h2>Líneas</h2><small>Marca las líneas por las que reportas. Las que te asignó el supervisor en el MES y las de tu supervisor aparecen solas.</small><div class=c>${L.map(l => {
    const mine = l.tags.includes('tablet'), fixed = l.tags.filter(t => t !== 'tablet');
    return `<div class=r3 style="align-items:center"><span><b>${E(l.code)}</b> · ${E(l.name)}<small style="display:block">${E(l.site)}${fixed.length ? ' · ' + fixed.map(t => E(TAGS[t] || t)).join(' · ') : ''}</small></span><button class="${mine ? 'pr' : ''}" data-a=supline data-v="${l.id}" data-on="${mine ? 0 : 1}">${mine ? '✔ Mía' : 'Agregar'}</button></div>`;
  }).join('') || '<small>No hay líneas activas.</small>'}</div>`;
}
// ---------------------------------------------------------------- alarma fuerte (mensajes y cortes)
// Un navegador común no deja sonar una página hasta que alguien la toca. Sí deja sonar sola a la app INSTALADA en el
// equipo (Chrome), a un navegador de quiosco con «reproducción automática» y a Chrome con la política de autoplay. Así
// que, sin pedirle nada al operario:
//  · el audio se intenta despertar solo todo el tiempo (al abrir, cada 5 s, al volver a la pantalla, antes de sonar);
//  · cualquier toque que de todos modos hace (entrar con su PIN, marcar, aceptar) lo deja despierto en esa página;
//  · nunca se deja un sonido en cola (con el audio dormido no suena tarde, p. ej. al dar «Aceptar»);
//  · con los «avisos del sistema» activados en el equipo, el mensaje suena también con el tono del teléfono;
//  · cada equipo informa al MES si puede sonar solo: el supervisor lo ve en Piso en vivo y arregla ESE equipo una vez.
let AC = null, SIR = null, KEEP = null;
function audio() {
  if (!AC) try { AC = new (window.AudioContext || window.webkitAudioContext)(); AC.onstatechange = keepAlive; } catch (e) { AC = null; }
  return AC;
}
const soundOn = () => !!AC && AC.state === 'running';
function wake() { const x = audio(); if (x && x.state !== 'running') x.resume().then(keepAlive, () => {}); }
function keepAlive() {   // tono inaudible que mantiene abierta la salida de audio: la sirena arranca sin retraso
  if (!soundOn() || KEEP) return;
  try { const o = AC.createOscillator(), g = AC.createGain(); g.gain.value = 0.0001; o.frequency.value = 40; o.connect(g); g.connect(AC.destination); o.start(); KEEP = o; } catch (e) { /* sin audio */ }
}
function unlockAudio() {   // dentro de un toque cualquiera: despierta el audio (iPhone/iPad exigen sonar algo en el toque)
  const x = audio(); if (!x) return;
  try {
    if (x.state !== 'running') x.resume().then(keepAlive, () => {});
    const s = x.createBufferSource(); s.buffer = x.createBuffer(1, 1, 22050); s.connect(x.destination); s.start(0);
  } catch (e) { /* sin audio */ }
}
['pointerdown', 'touchend', 'click', 'keydown'].forEach(ev => document.addEventListener(ev, unlockAudio, {passive: true, capture: true}));
document.addEventListener('visibilitychange', () => { if (!document.hidden) wake(); });
wake(); setInterval(wake, 5000);
// avisos del sistema (service worker): suenan con el tono de notificaciones del equipo aunque el navegador no deje sonar
let SWR = null, INST = null;
if ('serviceWorker' in navigator && window.isSecureContext && CFG.sw) navigator.serviceWorker.register(CFG.sw, {scope: CFG.base}).then(r => { SWR = r; }).catch(() => {});
window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); INST = e; if (S && tab === 'ajustes') render(); });
const installed = () => matchMedia('(display-mode: standalone)').matches || matchMedia('(display-mode: fullscreen)').matches || !!navigator.standalone;
const notifOk = () => !!SWR && 'Notification' in window && Notification.permission === 'granted';
function sysNotify(tag, title, body) {
  if (!notifOk() || (soundOn() && !document.hidden)) return;   // con la sirena sonando en pantalla no hace falta
  try { SWR.showNotification(title, {body, tag, renotify: true, requireInteraction: true, silent: false, vibrate: [600, 200, 600, 200, 900], icon: CFG.icon, badge: CFG.icon}); } catch (e) { /* sin avisos */ }
}
function sysClear(tag) { try { if (SWR && SWR.getNotifications) SWR.getNotifications({tag}).then(ns => ns.forEach(n => n.close())).catch(() => {}); } catch (e) { /* sin avisos */ } }
function siren(n = 3) {   // sirena de dos tonos con onda cuadrada (se oye por encima del ruido de la planta)
  if (!soundOn()) return false;   // nunca en cola: o suena ya, o no suena
  const x = AC;
  try {
    const t0 = x.currentTime + .01, comp = x.createDynamicsCompressor(), g = x.createGain();
    g.gain.value = 1; g.connect(comp); comp.connect(x.destination);
    SIR = comp;
    for (let i = 0; i < n * 2; i++) {
      const o = x.createOscillator(), s = t0 + i * .26; o.type = 'square';
      o.frequency.setValueAtTime(i % 2 ? 1400 : 950, s); o.connect(g); o.start(s); o.stop(s + .24);
    }
    // zumbido: tono grave y áspero debajo de la sirena (se siente aunque haya ruido de máquinas)
    const z = x.createOscillator(), zg = x.createGain(), lfo = x.createOscillator(), lg = x.createGain(), end = t0 + n * .52;
    z.type = 'sawtooth'; z.frequency.value = 150; lfo.frequency.value = 22; lg.gain.value = .5;
    lfo.connect(lg); lg.connect(zg.gain); zg.gain.value = .5; z.connect(zg); zg.connect(comp);
    z.start(t0); lfo.start(t0); z.stop(end); lfo.stop(end);
  } catch (e) { /* sin audio */ }
  return true;
}
function hush() { if (SIR) { try { SIR.disconnect(); } catch (e) { /* ya desconectado */ } SIR = null; } if (navigator.vibrate) navigator.vibrate(0); }
let ALM = null;
function blast() {   // un golpe de alarma: sirena (o, si el audio sigue dormido, lo despierta y suena enseguida), vibra y parpadea
  if (!siren(3)) {   // si el equipo lo permite, despierta en milisegundos; si tarda es que esperaba un toque: no suena tarde
    const x = audio(), t0 = Date.now();
    if (x) x.resume().then(() => { keepAlive(); if (ALM && Date.now() - t0 < 500) siren(3); }, () => {});
  }
  if (navigator.vibrate) navigator.vibrate([600, 200, 600, 200, 900]);
  document.body.classList.remove('flash'); void document.body.offsetWidth; document.body.classList.add('flash');
}
function alarmOn(key, note) {   // suena y vibra cada 4 s hasta que se acepte (después del primer minuto, cada 20 s)
  if (ALM && ALM.key === key) return;
  alarmOff();
  const ring = () => { if (ALM && Date.now() - ALM.t > 60000 && (ALM.n++ % 5)) return; blast(); };
  ALM = {key, t: Date.now(), n: 0, ring, iv: setInterval(ring, 4000)};
  ring();   // suena en el mismo instante en que llega el mensaje
  if (note) sysNotify(key, note.title, note.body);
}
function alarmOff() {   // «Aceptar»: se calla al instante (corta también la sirena que estaba sonando)
  if (ALM) { sysClear(ALM.key); clearInterval(ALM.iv); ALM = null; }
  hush(); document.body.classList.remove('flash');
}
function dueAlert() {   // llegó el corte: sirena tres veces
  if (Date.now() - (SV.alAt || 0) < 10000) return;   // varias operaciones con el mismo corte = un solo aviso
  SV.alAt = Date.now();
  [0, 5000, 10000].forEach(d => setTimeout(() => { if (ALM) return; blast(); }, d));
  sysNotify('corte', '⏰ Hora de reportar', 'Llegó el corte: reporta lo producido en el tramo.');
  toast('⏰ Hora de reportar lo producido');
}

// ---------------------------------------------------------------- supernumerario: su personal
const STATE_TXT = {paro: 'En paro', sin_marcar: 'Atrasado en reportes', ok: 'Trabajando', sin_iniciar: 'Sin iniciar', disponible: 'Sin tarea hoy'};
const ATT_CLS = {presente: 'ok', tarde: 'yl', ausente: 'rd', permiso: 'rd', incapacidad: 'rd', vacaciones: 'rd'};
function supPeople() {
  const B = S.people; if (!B) return '';
  const k = B.kpis, A2 = B.attendance, cards = B.cards || [];
  const kp = `<div class=g4>${ko('Personas', k.people)}${ko('Trabajando', k.working)}${ko('En paro ahora', k.open_stops, k.open_stops ? 'rd' : '')}${ko('Eficiencia hoy', P(k.eff), cl(k.eff))}${ko('Buenas hoy', q(k.good))}${ko('Rechazos hoy', q(k.scrap), k.scrap ? 'rd' : '')}${ko('Min. de paro hoy', Math.round(k.stop_min || 0))}${ko('Asistencia sin confirmar', A2.pending, A2.pending ? 'rd' : 'ok')}</div>`;
  const att = `<div class=c><div class=r2><b>Asistencia de hoy</b><span>${A2.counts.map(([n, c]) => `<span class="chip ${n === 'Sin confirmar' ? 'rd' : ''}">${E(n)}: ${c}</span>`).join('')}</span></div><small style="display:block;margin-top:6px">La confirma el supervisor en el MES («Mi turno»). Aquí se ve al instante.</small></div>`;
  const lines = (B.lines || []).map(l => `<div class=qi><span><b>${E(l.code)}</b> · ${E(l.name)}${l.stop ? ` · <b class=rd>PARO: ${E(l.stop.cause)} <span data-lt="${l.stop.start}">${fmt((now() - T(l.stop.start)) / 1e3)}</span></b>` : ''}<small style="display:block">${l.task ? E(l.task.name) + ' · ' + q(l.task.done) + ' / ' + q(l.task.qty) : 'Sin operación'} · ${q(l.good)} buenas hoy</small></span>
     <button class="${l.stop ? 'al-rd' : ''}" data-a=plstop data-v="${l.id}">${l.stop ? '▶ Reanudar línea' : '⏸ Paro de la línea'}</button><button data-a=lmsg data-v="${l.id}">📣 Mensaje</button></div>`).join('');
  const people = cards.map((c, i) => {
    const a = c.att, st = c.stop;
    return `<div class=c${st ? ' style="border-color:var(--rd)"' : ''}><div class=r2 style="flex-wrap:nowrap;align-items:flex-start"><div class=who style="min-width:0">${avc(c.name, i, '42px', st ? 'var(--rd)' : '')}<span style="min-width:0"><b style="font-size:16px">${E(c.name)}</b><small style="display:block">${c.task ? E(c.task.name) + ' · ' + E(c.task.line) : 'Sin tarea asignada hoy'}</small></span></div>
       <span style="text-align:right;flex:none"><span class="chip ${a ? ATT_CLS[a.status] || '' : 'rd'}">${a ? E(a.label) : 'Asistencia sin confirmar'}</span><small style="display:block">${a && a.by ? 'por ' + E(a.by) + ' · ' + hT(a.at) : ''}</small></span></div>
     <div class=om><div><b class="${cl(c.eff_day)}">${P(c.eff_day)}</b><small>Efic. día</small></div><div><b class="${cl(c.eff_hour)}">${P(c.eff_hour)}</b><small>Efic. hora</small></div><div><b>${q(c.good)}</b><small>Buenas hoy</small></div><div><b class="${c.scrap ? 'rd' : ''}">${q(c.scrap)}</b><small>Rechazos</small></div><div><b>${hT(c.last_mark)}</b><small>Último reporte</small></div><div><b class="${c.state === 'paro' || c.state === 'sin_marcar' ? 'rd' : c.state === 'ok' ? 'ok' : ''}" style="font-size:14px">${STATE_TXT[c.state] || ''}</b><small>Estado</small></div></div>
     ${st ? `<div class="c al-rd" style="margin:12px 0 0;padding:10px 14px"><b>${st.is_break ? 'Pausa' : 'Paro'}: ${E(st.cause)}</b> · <b data-lt="${st.start}">${fmt((now() - T(st.start)) / 1e3)}</b></div>` : ''}
     <div class=pa><button class="${st ? 'al-rd' : ''}" data-a=pstop data-v="${c.id}">${st ? '▶ Reanudar' : '⏸ Poner paro'}</button><button data-a=pmsg data-v="${c.id}">📣 Mensaje</button></div></div>`;
  }).join('') || '<div class=c><small>Aún no hay personal en tus líneas.</small></div>';
  const sent = (B.sent || []).length ? `<div class=c><b>Mensajes que envié hoy</b>${B.sent.map(m => `<div class=r3 style="font-size:13px"><span>${E(m.text)}<small style="display:block">para ${E(m.audience)}</small></span><small>${hT(m.at)}</small></div>`).join('')}</div>` : '';
  return `<div class=r2 style="margin-bottom:10px"><h2 style="margin:0">Mi personal</h2><button class=pr data-a=amsg>📣 Mensaje a todos</button></div>${kp}${att}${lines ? `<div class=c><b>Líneas</b>${lines}</div>` : ''}${people}${sent}`;
}

// ---------------------------------------------------------------- pausa (desayuno / almuerzo) con QR
function breakOverlay() {
  const st = S.stop;
  if (S.kind !== 'operario' || !st || !st.is_break) return '';
  const end = T(st.planned_end), rem = (end - now()) / 1e3, kd = st.cause.toLowerCase();
  if (rem > 0) return `<div class=bk><div class=c><div class=k>🍴 HORA DE ${kd.toUpperCase()}</div><div class=tmr id=bkt data-end="${end}">${fmt(Math.ceil(rem))}</div><small>Disfruta tu ${kd}. Vuelve a las ${hT(end)} y escanea el QR de la línea para retomar.</small></div></div>`;
  return `<div class=bk><div class=c><b style="font-size:24px">Se acabó el tiempo de ${kd}</b><small>Escanea el QR pegado en la línea para retomar la operación.</small><input id=bkc placeholder="Esperando el QR…" autocomplete=off autocapitalize=characters maxlength=40><p id=bke>${E(SV.bkErr || '')}</p><div class=k>Tiempo extra <b data-lt="${end ? new Date(end).toISOString() : st.start}">0:00</b></div></div></div>`;
}

// ---------------------------------------------------------------- operario
function opHome() {
  const t = S.task, m = S.mine, st = S.stop && !S.stop.is_break ? S.stop : null;
  if (!t) return `${stopCard()}${waitSched() || `<div class=c><b>Sin tarea asignada</b><small style="display:block;margin-top:6px">Tu supervisor te asignará una operación desde el MES (en «Mi turno», al armar la cuadrilla). Esta pantalla se actualiza sola.</small></div>`}${doneToday()}${MB(!!st, 'Sin tarea programada')}`;
  if (S.report && S.report.mode === 'intervalo' && S.report.period) return opInterval();
  if (S.report && S.report.mode === 'supernumerario') return opSuper();
  const goal = t.mpu > 0 ? Math.floor(60 / t.mpu) : 0, expS = t.mpu * 60, te = m.cycle_s ? expS / m.cycle_s * 100 : null;
  const nq = S.queue.length ? `<div class=c><div class=r2><b>Sigue en tu cola</b><span>${E(S.queue[0].name)} · ${q(S.queue[0].pending)} ${E(S.queue[0].unit)}</span></div>${S.queue.length > 1 ? `<small>+${S.queue.length - 1} más en espera</small>` : ''}</div>` : '';
  return `${stopCard()}${refBar(t)}
  <div class=c><div class=th><div style="min-width:0"><h2 style="font-size:clamp(22px,5vw,30px)">${E(t.name)}</h2><b style="display:block;margin-top:8px">Pedido ${E(t.order)}</b><small style="font-size:14px">${E(t.product)}${t.customer ? ' · ' + E(t.customer) : ''}</small>
   <div class=pb style="max-width:300px"><i style="width:${t.qty > 0 ? Math.min(100, t.done / t.qty * 100) : 0}%"></i></div><small>Meta de la operación · ${q(t.done)} / ${q(t.qty)} ${E(t.unit)} · tú: ${q(m.task_good)}${S.report && S.report.share != null && S.report.people > 1 ? ' de tu parte de ' + q(S.report.share) : ''}</small></div>
   <div class=who>${avc(dnm(), 0, '36px')}<span><b style="font-size:15px">${E(dnm())}</b><small>${E(t.line)}</small></span></div></div>
   <hr style="border:0;border-top:1px solid var(--bd);margin:14px 0 0">
   <div class=rr style="padding-bottom:10px"><div class=rw${st ? '' : ' data-a=mark'} style="cursor:${st ? 'not-allowed' : 'pointer'}">${ring(m.good_hour, goal, 'var(--rc)', `<b class="n a2">${q(m.good_hour)}</b><small>Última hora</small><hr><div class=r4><span><b>${Math.max(0, goal - m.good_hour)}</b><small>Para la meta</small></span><span><b>${(+t.mpu).toFixed(2)}</b><small>Minutos</small></span></div>`)}<span class=pill>${st ? 'En paro' : 'Toca para marcar'}</span></div>
   ${ring(m.scrap_today + m.rework_today, Math.max(m.good_today, 1), 'var(--rd)', `<b class=n>${q(m.scrap_today)}</b><small>Rechazos hoy</small><hr><b class=n2>${q(m.rework_today)}</b><small>Reprocesos</small>`)}</div></div>
  ${stdCard(t, 1, m.good_hour, 'por persona')}
  <div class=g4>
   <div class=kt><small>Eficiencia hora</small><b class="${cl(m.eff_hour)}">${P1(m.eff_hour)}</b></div><div class=kt><small>Eficiencia diaria</small><b class="${cl(m.eff_day)}">${P1(m.eff_day)}</b></div>
   <div class="kt w2"><b style="font-size:16px">Avance del pedido (toda la cuadrilla)</b><div class=ki><span><b>${q(t.done)}</b>Hechas</span><span><b>${q(t.pending)}</b>Faltan</span><span><b class=rd>${q(t.scrap)}</b>Rechazos</span></div></div>
   <div class=kt><small>Puntos individuales</small><p class=p3><span class=bl>H</span><span class=bl>+${nf(m.points_hour)}</span></p><p class=p3><span class=gr>D</span><span class=gr>+${nf(m.points_day)}</span></p><p class=p3><span title="Acumulado del mes">A</span><span>$${nf(m.money_month)}</span></p></div>
   <div class=kt><small>Eficiencia mensual</small><b class="${cl(m.eff_month)}">${P1(m.eff_month)}</b></div>
   <div class="kt w2"><b style="font-size:16px">Mis unidades</b><div class=ki><span><b>${q(m.task_good)}</b>Esta tarea</span><span><b>${q(m.good_today)}</b>Hoy</span><span><b>${q(m.good_month)}</b>Mes</span></div></div></div>
  <div class=c><div class=r2><div class=who>${avc(dnm())}<b style="font-size:18px">${E(t.name)}</b></div><span class=ic>${ICN(st || S.line_stop ? 'yl' : '', 'Paro abierto', 'warn')}${ICN(!st && m.eff_hour >= 100 ? 'ok' : '', 'Eficiencia hora ≥ 100%', 'ok')}${ICN(m.scrap_today ? 'rd' : '', 'Hay rechazos hoy', 'sad')}${ICN('w', 'Tarea abierta', 'lock')}</span></div>
   <div class=om><div><b>${mmss(expS)}</b><small>Tiempo esperado</small></div><div><b>${mmss(m.cycle_s)}</b><small>Tiempo tardado</small></div><div><b class="${cl(te)}">${te ? te.toFixed(1) + '%' : '–'}</b><small>Eficiencia</small></div><div><b>${hT(m.last_mark)}</b><small>Marcación</small></div></div></div>
  ${nq}${MB(!!st)}`;
}
const doneToday = () => S.done_today && S.done_today.length ? `<div class=c><div class=r2><b>Tareas completadas hoy</b><b class=ok>${S.done_today.length} ✔</b></div>${S.done_today.map(z => `<div class=r3 style="font-size:13px"><span>✔ ${E(z.name)} · pedido ${E(z.order)}<small style="display:block">${hT(z.end)} · ${q(z.qty)}/${q(z.goal)} · tú ${q(z.mine)}</small></span></div>`).join('')}</div>` : '';
// «Mis registros» (como Control tiempos de la app anterior): lo reportado por el operario en una fecha o lo último
const ymd = t => new Date(t).toLocaleDateString('en-CA');
async function loadMyRec(hist) {
  const d = ($('#mrd') || {}).value || (SV.myrec || {}).day || ymd(now());
  SV.myrec = {...(SV.myrec || {}), loading: true, day: d}; render();
  try { const j = await api('mis-registros?' + (hist ? 'h=1' : 'fecha=' + encodeURIComponent(d))); SV.myrec = {day: hist ? d : j.day, hist, rows: j.rows || []}; }
  catch (e) { if (!e.login) { toast(e.message); SV.myrec = {...SV.myrec, loading: false}; } }
  render();
}
function myRecords() {
  const R = SV.myrec || {}, rows = R.rows || [];
  const tot = rows.reduce((a, r) => a + (+r.u || 0), 0), hrs = rows.reduce((a, r) => a + (+r.h || 0), 0);
  return `<div class=c><div class=r2><b>Mis registros</b><span style="display:flex;gap:6px;align-items:center"><input id=mrd type=date value="${E(R.day || ymd(now()))}" max="${ymd(now())}" style="min-height:40px"><button class=sm data-a=myrec>Ver</button><button class=sm data-a=myrec data-v=h>Últimos 100</button></span></div>
   ${R.loading ? '<small>Cargando…</small>' : !R.rows ? '<small>Toca «Ver» para consultar.</small>' : !rows.length ? `<small style="display:block;margin-top:6px">${R.hist ? 'Aún no tienes registros.' : 'Sin registros ese día.'}</small>` :
   `<small style="display:block;margin:6px 0">${rows.length} registro${rows.length === 1 ? '' : 's'}${R.hist ? ' (los más recientes)' : ''} · <b>${q(tot)}</b> unidades · <b>${hrs.toFixed(2)}</b> h</small>` +
   rows.map(r => `<div class=r3 style="font-size:13px;align-items:center"><span><b>${R.hist ? E(r.fecha) + ' · ' : ''}${hT(r.ini)}${r.ini !== r.fin ? '–' + hT(r.fin) : ''}</b> · pedido ${E(r.pedido)}<small style="display:block">${E(r.proceso)}${r.ean ? ' · EAN ' + E(r.ean) : ''}${r.sup ? ' · reportó ' + E(r.sup) : ''}${+r.x ? ' · ' + q(r.x) + ' rechazos' : ''}</small></span><span style="text-align:right;flex:none"><b>${q(r.u)}</b><small style="display:block">${r.h != null ? r.h + ' h' : ''}${r.e != null ? ' · <b class="' + cl(r.e) + '">' + r.e + '%</b>' : ''}</small></span></div>`).join('')}</div>`;
}
function opHist() {
  const m = S.mine, td = S.stops_today || [], op = td.find(z => !z.end);
  return `<h2>Mi historial</h2><div class=g4><div class=kt><small>Unidades hoy</small><b>${q(m.good_today)}</b></div><div class=kt><small>Eficiencia hoy</small><b class="${cl(m.eff_day)}">${P(m.eff_day)}</b></div><div class=kt><small>Unidades del mes</small><b>${q(m.good_month)}</b></div><div class=kt><small>Eficiencia del mes</small><b class="${cl(m.eff_month)}">${P(m.eff_month)}</b></div><div class=kt style="grid-column:1/-1"><small>Puntos del mes</small><b>${nf(m.points_month)} <span class=gr style="font-size:18px">≈ $${nf(m.money_month)}</span></b></div></div>
  <div class=c><div class=r2><div><b>Tiempo de paro hoy</b><div class=k>${td.length} paro${td.length === 1 ? '' : 's'} · min:seg</div></div><b class=n2 ${op ? `data-lt="${op.start}" data-base="${Math.round(m.stop_min_today * 60 - (now() - T(op.start)) / 1e3)}"` : ''}>${fmt(m.stop_min_today * 60)}</b></div>${td.filter(z => z.end).slice(0, 8).map(z => `<div class=r3 style="font-size:13px"><span>${hT(z.start)} · ${E(z.cause)}</span><b>${fmt((T(z.end) - T(z.start)) / 1e3)}</b></div>`).join('')}</div>${myRecords()}${doneToday()}`;
}
function opQueue() {
  const t = S.task;
  return `<h2>Mi línea de trabajo</h2><small>${(t ? 1 : 0) + S.queue.length} tarea(s) pendientes. El orden lo define tu supervisor en el MES.</small>
  ${t ? opCard('▶ En curso', t, progress(t), ' style="border-color:var(--ac)"') : `<div class=c><b>Sin tarea en curso</b><small style="display:block;margin-top:6px">Estás disponible: tu supervisor te asignará la siguiente tarea.</small></div>`}
  ${S.queue.map((x, i) => isLocked(x) ? lockedCard('', x) : opCard('Siguiente ' + (i + 1), x, `<small style="display:block;margin-top:8px">${mmss(x.mpu * 60)} por unidad · línea ${E(x.line)}${x.start ? ' · ' + dT(x.start) + ' ' + hT(x.start) + ' → ' + hT(x.end) : ''}</small>`)).join('')}${doneToday()}`;
}
function ficha() {
  const t = S.task, soon = (a, b) => `<div class="c soon"><span class=chip>Próximamente</span><b style="display:block">${a}</b><small>${b}</small></div>`;
  const people = S.kind === 'linea' ? (S.kpis.people || 1) : 1;
  return `<h2>Ficha técnica</h2>${t ? `<div class=c><span class=chip>Operación actual</span><div style="font-size:20px;font-weight:700;margin:6px 0">${E(t.name)}</div><small>Pedido ${E(t.order)} · ${E(t.product)}${t.customer ? ' · ' + E(t.customer) : ''} · ${E(t.line)}</small>
   <div class=om><div><b>${(+t.mpu).toFixed(2)}</b><small>Min estándar / unidad</small></div><div><b>${mmss(t.mpu * 60 / people)}</b><small>${people > 1 ? 'Takt de la línea' : 'Por unidad'}</small></div><div><b>${t.mpu > 0 ? Math.floor(60 * people / t.mpu) : '–'}</b><small>Unid./hora al 100%</small></div><div><b>${q(t.qty)}</b><small>Programadas (${E(t.unit)})</small></div><div><b>${t.crew}</b><small>Personas planeadas</small></div><div><b>${t.due ? dT(t.due) : '–'}</b><small>Compromiso</small></div></div></div>`
   : '<div class=c><b>Sin tarea asignada</b><small style="display:block;margin-top:6px">Cuando tengas una operación verás aquí su ficha.</small></div>'}
  ${soon('Paso a paso', 'Video y fotos de cómo hacer la operación, con los ajustes recomendados.')}${soon('¿Lo estoy haciendo bien?', 'Puntos de calidad para comparar con el estándar.')}${soon('Errores comunes', 'Defectos frecuentes y cómo evitarlos.')}`;
}
// Configuración del equipo (la hace el supervisor UNA vez por terminal; vale para todos los que la usen)
function deviceCard() {
  const ok = (v, a, b) => `<b class="${v ? 'ok' : 'rd'}">${v ? '✔ ' + a : '✖ ' + b}</b>`;
  const secure = window.isSecureContext, inst = installed();
  const nperm = 'Notification' in window ? Notification.permission : 'no';
  const row = (t, v, extra = '') => `<div class=r3 style="align-items:center"><span>${t}${extra ? `<small style="display:block">${extra}</small>` : ''}</span>${v}</div>`;
  return `<div class=c><b>Este equipo: alarmas automáticas</b><small style="display:block;margin-top:6px">Lo configura el supervisor una sola vez por equipo. Después las alarmas suenan solas, sin que el operario toque nada.</small>
   ${row('Sonido de alarmas', ok(soundOn(), 'Suena solo', 'Bloqueado por el navegador'), soundOn() ? '' : 'Se arregla instalando la app (abajo) o con un navegador de quiosco. Mientras tanto suena desde el primer toque que haga el operario.')}
   ${row('App instalada en el equipo', inst ? ok(true, 'Sí') : INST ? '<button class=pr data-a=install>📲 Instalar</button>' : ok(false, '', 'No'),
     inst ? 'Instalada: Chrome deja sonar las alarmas sin toques.' : !secure ? 'Para instalarla, abre la app por la dirección <b>https</b> del MES.' : INST ? '' : 'Menú del navegador (⋮) › «Instalar app» o «Agregar a la pantalla principal». Luego ábrela siempre desde ese ícono.')}
   ${row('Avisos del sistema (tono del teléfono)', !secure || !('Notification' in window) ? '<small>No disponible</small>' : nperm === 'granted' ? ok(true, 'Activados') : nperm === 'denied' ? ok(false, '', 'Bloqueados') : '<button class=pr data-a=notif>🔔 Activar</button>',
     !secure ? 'Requiere abrir la app por https.' : nperm === 'denied' ? 'Actívalos en los permisos del sitio en el navegador.' : 'Suenan aunque el navegador tenga el sonido dormido o la app esté en segundo plano.')}
   <small style="display:block;margin-top:10px">Sube el volumen del equipo al máximo y activa «Mantener pantalla encendida».</small>
   <button data-a=testalarm style="margin-top:10px;width:100%">🔊 Probar alarma</button></div>`;
}
function settings() {
  const c = CF(), ch = (k, v, t) => `<button class="${(c[k] || '') === v ? 'pr' : ''}" data-a=cfs data-k=${k} data-v="${v}">${t}</button>`;
  const sw = (k, t, d) => `<label class=swt><span>${t}<small style="display:block">${d}</small></span><input type=checkbox data-c=cf data-k=${k}${c[k] ? ' checked' : ''}></label>`;
  const person = S.kind !== 'linea';
  return `<h2>Ajustes</h2><div class=c><b>${person ? 'Perfil' : 'Tablet de la línea'}</b><div class=who style="margin:10px 0">${avc(dnm())}<span><b style="font-size:16px">${E(dnm())}</b><small>${person ? 'Documento ••' + E(S.who.doc || '') + (S.who.shift ? ' · ' + E(S.who.shift) : '') : E(S.who.code) + ' · ' + E(S.who.site)}</small></span></div>
   ${person ? `<label class=fl>Nombre en mi pantalla<input data-c=cf data-k=nick maxlength=24 value="${E(c.nick || '')}" placeholder="${E(S.who.name)}"></label><small>Solo cambia cómo se ve en este dispositivo.</small>` : ''}</div>
  <div class=c><b>Apariencia</b><div class=ap><small>Tema</small><div class=sel>${ch('theme', '', 'Automático')}${ch('theme', 'dark', '🌙 Oscuro')}${ch('theme', 'light', '☀️ Claro')}</div></div>
   <div class=ap><small>Color</small><div class=sel style="flex-wrap:wrap">${Object.entries(ACC).map(([k, v]) => `<button class="sw2${c.acc === k ? ' on' : ''}" style="background:${v};flex:none" data-a=cfs data-k=acc data-v=${k} title="${k}"></button>`).join('')}<button class="${c.acc ? '' : 'pr'}" data-a=cfs data-k=acc data-v="">Original</button></div></div>
   <div class=ap><small>Tamaño de la pantalla</small><div class=sel>${ch('sz', '', 'Normal')}${ch('sz', '1.15', 'Grande')}${ch('sz', '1.3', 'Muy grande')}</div></div></div>
  ${deviceCard()}
  <div class=c><b>Al trabajar</b>${sw('snd', 'Sonido al marcar', 'Un pitido en cada unidad y un tono al completar la tarea.')}${sw('vib', 'Vibración', 'Vibra al marcar (depende del dispositivo).')}${sw('awake', 'Mantener pantalla encendida', 'Evita que la tablet se apague mientras trabajas.')}</div>
  ${person ? `<div class=c><b>Cambiar mi PIN</b><form data-f=pin class=fm><input name=pin type=password inputmode=numeric placeholder="PIN actual" required autocomplete=current-password><input name=new type=password inputmode=numeric minlength=4 maxlength=8 placeholder="PIN nuevo (4 a 8 números)" required autocomplete=new-password><input name=new2 type=password inputmode=numeric minlength=4 maxlength=8 placeholder="Repite el PIN nuevo" required autocomplete=new-password><button class=pr>Guardar PIN</button></form></div>` : ''}
  <div class=c style="display:grid;gap:8px"><button data-a=cfr>Restablecer mis ajustes de pantalla</button>${person ? '<button class=al-rd data-a=logout>Salir (cerrar sesión)</button>' : '<button class=al-rd data-a=sheet data-v=unpair>Desvincular esta tablet</button>'}</div>`;
}

// ---------------------------------------------------------------- tablet de línea
// Desde cuándo corre el cronómetro: la última unidad marcada hoy. Si aún no hay ninguna, un punto fijo guardado en el
// dispositivo (por línea y operación), para que cambiar de pestaña, recargar o actualizar no lo reinicie.
function taktFrom(k, t) {
  if (k.last_mark) return T(k.last_mark);
  const key = S.who.id + ':' + t.id, day = T0();
  if (!me.tk || me.tk.key !== key || me.tk.at < day) { me.tk = {key, at: now()}; save(); }
  return me.tk.at;
}
function lineHome() {
  const t = S.task, k = S.kpis, crew = S.crew || [], n = Math.max(1, k.people || 1), ls = S.line_stop;
  const top = `<div class=mh><div class=c><div class=who style="gap:10px"><i class=av style="width:52px;height:52px;font-size:17px;background:#c9bfff;color:#241d4a">${E(init(S.who.name))}</i><span style="min-width:0"><b style="font-size:17px;display:block">${E(S.who.name)}</b><small>${crew.length} persona${crew.length === 1 ? '' : 's'} en la cuadrilla</small></span></div><div style="display:flex;gap:2px;align-items:center;margin-top:10px">${crew.slice(0, 4).map((o, i) => avc(o.name, i, '26px')).join('')}${crew.length > 4 ? `<i class=av style="width:26px;height:26px;font-size:10px;background:#4a4a52">+${crew.length - 4}</i>` : ''}<button class=pr data-a=sheet data-v=who title="Registrar paro de un operario" style="width:30px;height:30px;padding:0;border-radius:8px;margin-left:6px;min-height:0">＋</button></div></div>
   <div class=c><small>Eficiencia hora</small><b class="n2 ${cl(k.eff_hour)}" style="display:block;margin-top:12px">${P(k.eff_hour)}</b></div><div class=c><small>Eficiencia diaria</small><b class="n2 ${cl(k.eff_day)}" style="display:block;margin-top:12px">${P(k.eff_day)}</b></div></div>`;
  const lsCard = ls ? `<div class="c al-rd"><div class=k style="color:#fff">PARO DE LA LÍNEA · ${E(ls.cause)}</div><div class=tmr data-lt="${ls.start}">${fmt((now() - T(ls.start)) / 1e3)}</div><small style="display:block;text-align:center">Toca Reanudar para cerrar el paro</small></div>` : '';
  if (!t && waitSched()) return `${top}${lsCard}${waitSched()}${requests()}${MB(!!ls, 'Sin operación programada')}`;
  if (!t) return `${top}${lsCard}<div class=c><b>Sin operación liberada</b><small style="display:block;margin-top:6px">Cuando planeación libere a planta un pedido para esta línea aparecerá aquí. Mientras tanto no se puede marcar. Esta pantalla se actualiza sola.</small></div>${requests()}${MB(!!ls, 'Sin operación liberada')}`;
  // línea continua: en vez del contador, se reporta el total de cada tramo (se reparte entre el personal)
  if (S.panel) return `${top}${lsCard}${linePanel(S.panel, true)}${repList(S.panel.reports)}${requests()}${RB(!!ls)}`;
  const goal = t.mpu > 0 ? Math.floor(60 * n / t.mpu) : 0, tk = t.mpu * 60 / n, te = k.cycle_s ? tk / k.cycle_s * 100 : null;
  const openOf = id => (crew.find(o => o.id === id) || {}).stop, srt = crew.slice().sort((a, b) => !!b.stop - !!a.stop), vis = srt.slice(0, 5), rest = crew.length - vis.length;
  const tile = o => { const z = o.stop, i = crew.indexOf(o); return `<div data-a=crewstop data-v="${o.id}"><span style="position:relative;display:inline-block">${avc(o.name, i, '44px', z ? 'var(--rd)' : 'var(--a2)')}<span class=dot style="background:${z ? 'var(--rd)' : 'var(--a2)'}"></span></span><small>${E(o.name.split(' ')[0])}</small>${z ? `<small class=rd data-lt="${z.start}">${fmt((now() - T(z.start)) / 1e3)}</small>` : ''}</div>`; };
  SV.takt = {t: taktFrom(k, t), k: tk};
  return `${top}${lsCard}${refBar(t)}
  <div class=c><div class=r2 style="flex-wrap:nowrap"><div style="min-width:0"><b style="font-size:26px">${E(t.name)}</b><div style="margin-top:6px">Pedido ${E(t.order)}</div><small>${E(t.product)}${t.customer ? ' · ' + E(t.customer) : ''}</small><div class=pb style="max-width:300px"><i style="width:${t.qty > 0 ? Math.min(100, t.done / t.qty * 100) : 0}%"></i></div><small>Meta de la operación · ${q(t.done)} / ${q(t.qty)} ${E(t.unit)}${S.queue.length ? ' · +' + S.queue.length + ' en cola' : ''}</small></div></div>
   <hr style="border:0;border-top:1px solid var(--bd);margin:14px 0 0">
   <div class=rr><div class=rw${ls ? '' : ' data-a=mark'} style="cursor:${ls ? 'not-allowed' : 'pointer'}">${ring(k.good_hour, goal, 'var(--rc)', `<b class="n a2">${q(k.good_hour)}</b><small>Última hora</small><hr><div class=r4><span><b>${Math.max(0, goal - k.good_hour)}</b><small>Para la meta</small></span><span><b>${(+t.mpu).toFixed(2)}</b><small>Minutos</small></span></div>`)}<span class=pill>${ls ? 'En paro' : 'Toca para marcar'}</span></div>
   ${ring(0, 1, '#d9d8e3', `<b class=n2 id=tm data-h=1>00:00:00</b><small>Takt ${fmt(tk)} esperado</small>`, 'tg')}</div></div>
  ${stdCard(t, n, k.good_hour, n === 1 ? '1 persona' : 'la línea · ' + n + ' personas')}
  <div class=c><div class=r2 style="flex-wrap:nowrap"><div style="min-width:0;flex:1"><b style="font-size:17px">Tiempos improductivos abiertos</b><div class=mt>${vis.map(tile).join('')}${rest > 0 ? `<div data-a=sheet data-v=who><i class=av style="width:44px;height:44px;background:#4a4a52;font-size:13px">+${rest}</i><small>Más</small></div>` : ''}</div>${crew.length ? '' : '<small>La cuadrilla la arma el supervisor en el MES.</small>'}</div><div style="text-align:right;flex:none"><b class=n2>${Math.round(k.stop_min_today)}</b><small style="display:block">Minutos<br>improductivos</small></div></div></div>
  <div class=c><div class=r2><div class=who>${avc(S.who.name)}<b style="font-size:17px">${E(t.name)}</b></div><span class=ic>${ICN(crew.some(o => o.stop) || ls ? 'yl' : '', 'Hay paros abiertos', 'warn')}${ICN(k.eff_hour >= 100 ? 'ok' : '', 'Eficiencia hora ≥ 100%', 'ok')}${ICN(k.scrap_today ? 'rd' : '', 'Hay rechazos hoy', 'sad')}${ICN('w', 'Operación abierta', 'lock')}</span></div>
   <div class=om><div><b>${fmt(tk)}</b><small>Tiempo esperado</small></div><div><b>${mf(k.cycle_s)}</b><small>Tiempo tardado</small></div><div><b class="${cl(te)}">${te ? te.toFixed(1) + '%' : '–'}</b><small>Eficiencia</small></div><div><b>${hT(k.last_mark)}</b><small>Marcación</small></div></div></div>
  ${requests()}${MB(!!ls)}`;
}
const PST = {p: 'Pendiente', a: 'Aceptada', c: 'En camino', e: 'Entregada'};
const reqRow = x => `<div class=r3 style="font-size:13px"><span><span class=chip>${PST[x.status]}</span>${x.urgent ? '<span class=chip style="color:var(--yl)">⚡</span>' : ''} ${E(x.origin)}<small style="display:block">${x.pallets} estiba${x.pallets === 1 ? '' : 's'}${x.note ? ' · ' + E(x.note) : ''} · ${x.mover ? '🛒 ' + E(x.mover) : 'abierto, sin patinador'}${x.by ? ' · pidió ' + E(x.by) : ''}</small></span><b ${x.status === 'e' ? '' : `data-lt="${x.created_at}"`}>${fmt(((T(x.delivered_at) || now()) - T(x.created_at)) / 1e3)}</b></div>`;
const requests = () => `<div class=c><div class=r2><b>Material</b><button class=pr data-a=sheet data-v=pmov>📦 Pedir material</button></div>${(S.requests || []).map(reqRow).join('') || '<small style="display:block;margin-top:8px">Sin pedidos de material hoy.</small>'}</div>`;
function supMaterial() {   // supernumerario: pedir material para sus líneas y seguir los pedidos
  const L = (S.material || {}).lines || [];
  if (!L.length) return `<div class=c><b>Aún no tienes líneas</b><small style="display:block;margin-top:6px">Elige en «Líneas» por cuáles reportas para pedir su material.</small></div>`;
  return `<h2>Material</h2><small>Pide estibas o insumos para tus líneas. Le llegan al patinador que elijas; por defecto, al patinador que el supervisor asignó a la operación de la línea.</small>` +
    L.map(l => { const dm = ((S.material.movers || []).find(m => m.id === l.default_mover) || {}).name; return `<div class=c><div class=r2><span><b style="font-size:18px">${E(l.code)}</b> · ${E(l.name)}<small style="display:block">${dm ? 'Patinador de la línea: <b>' + E(dm) + '</b>' : 'Sin patinador asignado: el pedido queda abierto o elige uno'}</small></span><button class=pr data-a=pmat data-v="${l.id}">📦 Pedir material</button></div>${l.requests.map(reqRow).join('') || '<small style="display:block;margin-top:8px">Sin pedidos de material hoy.</small>'}</div>`; }).join('');
}
function lineHist() {
  const k = S.kpis, crew = S.crew || [], A2 = S.stops_today || [];
  const by = {}; A2.forEach(x => { if (x.employee_id) { const z = by[x.employee_id] = by[x.employee_id] || {n: x.employee, c: 0, m: 0}; z.c++; z.m += ((T(x.end) || now()) - T(x.start)) / 6e4; } });
  return `<h2>Historial de la línea</h2><div class=g4><div class=kt><small>Unidades hoy</small><b>${q(k.good_today)}</b></div><div class=kt><small>Eficiencia hoy</small><b class="${cl(k.eff_day)}">${P(k.eff_day)}</b></div><div class=kt><small>Unidades del mes</small><b>${q(k.good_month)}</b></div><div class=kt><small>Paros hoy</small><b>${Math.round(k.stop_min_today)} <span style="font-size:14px">min</span></b></div></div>
  <div class=c><b>Improductivo de hoy por operario</b>${Object.values(by).map((z, i) => `<div class=r3 style="font-size:13px"><span class=who>${avc(z.n, i)}<span>${E(z.n)}</span></span><span><span class=chip>${z.c} paro${z.c === 1 ? '' : 's'}</span><b>${Math.round(z.m)} min</b></span></div>`).join('') || '<small style="display:block;margin-top:8px">Sin paros hoy.</small>'}</div>
  <div class=c><b>Paros registrados hoy</b>${A2.map(x => `<div class=r3 style="font-size:13px"><span><b>${E(x.employee || 'Toda la línea')}</b> · ${E(x.cause)}<small style="display:block">${hT(x.start)}${x.end ? ' – ' + hT(x.end) : ' · abierto'}</small></span><b ${x.end ? '' : `data-lt="${x.start}"`}>${fmt(((T(x.end) || now()) - T(x.start)) / 1e3)}</b></div>`).join('') || '<small style="display:block;margin-top:8px">Aún no hay paros registrados.</small>'}</div>
  <div class=c><b>Cuadrilla</b>${crew.map((o, i) => `<div class=r3 style="font-size:13px"><span class=who>${avc(o.name, i)}<span>${E(o.name)}${o.stop ? ' · <span class=rd>' + E(o.stop.cause) + '</span>' : ''}</span></span><b>${q(o.units)} u</b></div>`).join('') || '<small style="display:block;margin-top:8px">Sin cuadrilla asignada.</small>'}</div>`;
}
function lineQueue() {
  const t = S.task;
  return `<h2>Operaciones de la línea</h2><small>Pedidos liberados a planta para esta línea, en el orden del programa.</small>${t ? opCard('▶ En curso', t, progress(t), ' style="border-color:var(--ac)"') : '<div class=c><b>Sin operación en curso</b></div>'}${S.queue.map((x, i) => isLocked(x) ? lockedCard('', x) : opCard('Siguiente ' + (i + 1), x, progress(x))).join('')}`;
}

// ---------------------------------------------------------------- patinador
const route = x => `<b>${E(x.origin)}</b> <span class=a2>→</span> <b>${E(x.destination)}</b>`;
const what = x => `${x.order ? 'Pedido ' + E(x.order) + ' · ' : ''}${x.pallets} estiba${x.pallets === 1 ? '' : 's'}`;
const ko = (t, v, c) => `<div class=kt><small>${t}</small><b class="${c || ''}">${v}</b></div>`;
const steps = x => `<div class=stp>${['p', 'a', 'c', 'e'].map((k, i) => { const t = [x.created_at, x.accepted_at, x.loaded_at, x.delivered_at][i]; return `<span class="${t ? 'on' : ''}${x.status === k ? ' cu' : ''}"><i></i><small>${{p: 'Pendiente', a: 'Aceptada', c: 'Cargada', e: 'Entregada'}[k]}${t ? ' · ' + hT(t) : ''}</small></span>`; }).join('')}</div>`;
function movHome() {
  const cur = S.current, h = S.today, st = S.stop, late = cur && cur.required_at && now() > T(cur.required_at);
  const lb = {p: 'Aceptar traslado', a: 'Ya recogí la estiba', c: 'Ya entregué la estiba'}, tot = h.pallets + S.pending_pallets;
  SV.takt = cur && cur.status !== 'p' ? {t: T(cur.status === 'c' ? cur.loaded_at : cur.accepted_at), k: cur.median_s || 0} : null;
  const pa = st ? `<div class="c al-rd"><div class=k style="color:#fff">PARO EN CURSO · ${E(st.cause)}</div><div class=tmr data-lt="${st.start}">${fmt((now() - T(st.start)) / 1e3)}</div><small style="display:block;text-align:center">Toca Reanudar para continuar</small></div>` : '';
  const card = cur ? `<div class=c${cur.urgent ? ' style="border-color:var(--yl)"' : ''}><div class=r2><span><span class=chip>${PST[cur.status] === 'En camino' ? 'Cargada' : PST[cur.status]}</span>${cur.urgent ? '<span class=chip style="color:var(--yl)">⚡ Urgente</span>' : ''}${cur.required_at ? `<span class="chip${late ? ' rd' : ''}">Para las ${hT(cur.required_at)}</span>` : ''}</span><b class=n2>${cur.pallets} <small>estiba${cur.pallets === 1 ? '' : 's'}</small></b></div><div style="font-size:clamp(20px,4.5vw,28px);font-weight:700;margin:10px 0">${E(cur.origin)} <span class=a2>→</span> ${E(cur.destination)}</div><small>${cur.order ? 'Pedido ' + E(cur.order) : 'Material sin pedido'}${cur.from_line ? ' · pedido por ' + E(cur.line || 'la línea') + (cur.by ? ' (' + E(cur.by) + ')' : '') : ''}</small>${cur.note ? `<div class=nt style="margin-top:10px">${E(cur.note)}</div>` : ''}${cur.incident ? `<div style="margin-top:8px"><span class="chip rd">⚠ ${E(cur.incident)}</span></div>` : ''}${steps(cur)}</div>`
    : `<div class=c><b>Sin traslados asignados</b><small style="display:block;margin-top:6px">${S.pool.length ? 'Hay traslados abiertos en planta: toma uno.' : 'Estás disponible. Esta pantalla se actualiza sola cuando te asignen uno.'}</small>${S.pool.slice(0, 3).map(x => `<div class=qi><span>${route(x)}<small style="display:block">${what(x)}${x.urgent ? ' · ⚡ urgente' : ''}</small></span><button class=pr data-a=madv data-v="${x.id}">Tomar</button></div>`).join('')}</div>`;
  const rr = `<div class=rr><div class=rw>${ring(h.pallets, tot || 1, 'var(--rc)', `<b class="n a2">${h.pallets}</b><small>Estibas hoy</small><hr><div class=r4><span><b>${S.pending_pallets}</b><small>Por mover</small></span><span><b>${h.trips}</b><small>Viajes</small></span></div>`)}</div><div class=rw>${ring(0, 1, '#d9d8e3', `<b class=n2 id=tm>–</b><small>${cur && cur.median_s ? 'Mediana ruta ' + fmt(cur.median_s) : 'Aprendiendo la ruta'}</small>`, 'tg')}</div></div>`;
  const kp = `<div class=g4>${ko('Viajes hoy', h.trips)}${ko('Estibas hoy', h.pallets)}${ko('Viajes / hora', h.per_hour == null ? '–' : h.per_hour.toFixed(1))}${ko('Ciclo promedio', mf(h.cycle_s))}${ko('A tiempo', P(h.on_time), cl(h.on_time))}${ko('Utilización', P(h.utilization))}${ko('Vs. su ruta', P(h.vs_route), cl(h.vs_route))}<div class=kt><small>Puntos</small><p class=p3><span class=gr>D</span><span class=gr>+${nf(h.points)}</span></p><p class=p3><span class=bl>M</span><span class=bl>+${nf(S.month.points)}</span></p></div></div>`;
  const om = `<div class=c><div class=r2><b>Tiempos del ciclo (hoy)</b><span class=chip>${h.trips} viaje${h.trips === 1 ? '' : 's'}</span></div><div class=om><div><b>${mf(h.response_s)}</b><small>Respuesta</small></div><div><b>${mf(h.pickup_s)}</b><small>Recogida</small></div><div><b>${mf(h.transport_s)}</b><small>Transporte</small></div><div><b>${mf(h.cycle_s)}</b><small>Ciclo total</small></div></div></div>`;
  const nx = S.queue.length ? `<div class=c><div class=r2><b>Sigue</b><span>${route(S.queue[0])}</span></div>${S.queue.length > 1 ? `<small>+${S.queue.length - 1} más en tu cola</small>` : ''}</div>` : '';
  const bar = `<div class=pbar><button data-a=reload title="Actualizar">${svg('refresh')}</button><button data-a=sheet data-v=pinc title="Incidencia"${cur ? '' : ' disabled'}>${svg('down')}</button><button class=pr data-a=madv data-v="${cur ? cur.id : ''}"${cur && !st ? '' : ' disabled'}>${cur ? lb[cur.status] : 'Sin traslado'}</button><button data-a=flag class="${st ? 'al-rd' : ''}" title="${st ? 'Reanudar' : 'Paro'}">${svg(st ? 'play' : 'flag')}</button><button data-a=sheet data-v=msg title="Mensajes">${svg('chat')}</button></div>`;
  const asg = (S.assigned || []).length ? `<div class=c style="padding:10px 14px"><small>Patinador asignado por el supervisor a: ${S.assigned.map(a => `<b>${E(a.line || '—')}</b> · ${E(a.name)} (${E(a.order)})`).join(' · ')}. Al terminar la operación vuelves a tu pantalla normal.</small></div>` : '';
  return `${pa}${asg}${card}${rr}${kp}${om}${nx}${bar}`;
}
function movHist() {
  const h = S.today, m = S.month, td = S.stops_today || [], D = {};
  (S.delivered || []).forEach(x => { const k = new Date(x.delivered_at).toDateString(); (D[k] = D[k] || []).push(x); });
  return `<h2>Mi historial</h2><div class=g4>${ko('Viajes hoy', h.trips)}${ko('Ciclo hoy', mf(h.cycle_s))}${ko('Viajes del mes', m.trips)}${ko('Ciclo del mes', mf(m.cycle_s))}<div class=kt style="grid-column:1/-1"><small>Puntos del mes</small><b>${nf(m.points)}</b></div></div>
  <div class=c><b>Paros de hoy</b>${td.map(z => `<div class=r3 style="font-size:13px"><span>${hT(z.start)} · ${E(z.cause)}</span><b ${z.end ? '' : `data-lt="${z.start}"`}>${fmt(((T(z.end) || now()) - T(z.start)) / 1e3)}</b></div>`).join('') || '<small style="display:block;margin-top:8px">Sin paros hoy.</small>'}</div>
  <div class=c><div class=r2><b>Traslados entregados</b><span class=chip>${m.trips} este mes</span></div>${Object.entries(D).map(([k, a]) => `<div style="margin-top:14px"><small style="text-transform:capitalize">${k === new Date(now()).toDateString() ? 'Hoy' : dT(a[0].delivered_at)} · ${a.reduce((s, z) => s + z.pallets, 0)} estibas</small>${a.map(x => `<div class=r3 style="font-size:13px"><span>✔ ${route(x)}<small style="display:block">${hT(x.accepted_at)}–${hT(x.delivered_at)} · ${what(x)}${x.required_at ? (T(x.delivered_at) <= T(x.required_at) ? ' · a tiempo' : ' · tarde') : ''}${x.incident ? ' · ⚠ ' + E(x.incident) : ''}</small></span><b>${fmt((T(x.delivered_at) - T(x.loaded_at)) / 1e3)}</b></div>`).join('')}</div>`).join('') || '<small style="display:block;margin-top:8px">Aún no has entregado traslados este mes.</small>'}</div>`;
}
function movQueue() {
  const row = (x, take) => `<div class=c><div class=r2><div><span class=chip>${take ? 'Abierto' : PST[x.status]}</span>${x.urgent ? '<span class=chip style="color:var(--yl)">⚡ Urgente</span>' : ''}${x.required_at ? `<span class=chip>Para las ${hT(x.required_at)}</span>` : ''}<div style="font-size:17px;margin-top:6px">${route(x)}</div><small>${what(x)}${x.note ? ' · ' + E(x.note) : ''}</small></div>${take ? `<button class=pr data-a=madv data-v="${x.id}">Tomar</button>` : ''}</div></div>`;
  const mine = [S.current, ...S.queue].filter(Boolean);
  return `<h2>Mi cola de traslados</h2><small>${mine.length} asignado(s)</small>${mine.map(x => row(x)).join('') || '<div class=c><small>Sin traslados asignados.</small></div>'}<h2 style="margin-top:8px">Abiertos en planta</h2><small>Cualquier patinador puede tomarlos.</small>${S.pool.map(x => row(x, true)).join('') || '<div class=c><small>No hay traslados abiertos.</small></div>'}`;
}

// ---------------------------------------------------------------- acceso
function loginView() {
  const dots = `<div class=pin>${[0, 1, 2, 3, 4, 5].map(i => `<i class="${i < pinBuf.length ? 'on' : ''}"></i>`).join('')}</div>`;
  const pad = `<div class=pad>${[1, 2, 3, 4, 5, 6, 7, 8, 9].map(n => `<button type=button data-a=pk data-v=${n}>${n}</button>`).join('')}<button type=button data-a=pk data-v=C>⌫</button><button type=button data-a=pk data-v=0>0</button><button type=submit class=pr>Entrar</button></div>`;
  return `<div class="lg"><div class=c style="display:grid;gap:12px"><div><h1 class=pt>Control de piso</h1><small>${loginMode === 'op' ? 'Entra con tu documento y tu PIN.' : 'Vincula esta tablet a una línea con el código que tiene el jefe de planta.'}</small></div>
   <div class=seg><button class="${loginMode === 'op' ? 'on' : ''}" data-a=lm data-v=op>Operario</button><button class="${loginMode === 'line' ? 'on' : ''}" data-a=lm data-v=line>Tablet de línea</button></div>
   ${loginMode === 'op' ? `<form data-f=login style="display:grid;gap:10px"><input id=doc name=doc inputmode=numeric autocomplete=username placeholder="Número de documento" value="${E(me.lastDoc || '')}" required style="font-size:20px;padding:12px">${dots}${pad}</form>`
    : `<form data-f=pair style="display:grid;gap:10px"><input name=code autocapitalize=characters autocomplete=off placeholder="Código de la línea (8 letras)" required style="font-size:20px;padding:12px;letter-spacing:.12em;text-transform:uppercase"><button class=pr>Vincular tablet</button></form>`}
   ${loginErr ? `<p class=rd style="margin:0">${E(loginErr)}</p>` : ''}</div>
   <small style="text-align:center">Los supervisores trabajan desde el MES: ahí asignan las tareas que llegan a esta pantalla.</small></div>`;
}

// ---------------------------------------------------------------- pintar
function render() {
  const ae = document.activeElement;
  if (!S) { applyCfg(); A.innerHTML = booted ? loginView() : '<p class=k style="padding:20px">Cargando…</p>'; return; }
  if (ae && ['INPUT', 'TEXTAREA', 'SELECT'].includes(ae.tagName) && A.contains(ae) && ae.id !== 'bkc') return;
  applyCfg();
  if (needCheckin()) {   // lo primero del día: a dónde va + confirmar asistencia (nada más se habilita antes)
    if (ALM && ALM.key[0] === 'm') alarmOff();
    sheet = null; A.innerHTML = `${head()}<div class=ph>${checkinView()}</div>`; tick(); return;
  }
  const otc = otCall();
  if (otc) {   // convocatoria de horas extra: hasta que no responda no se habilita nada (o la está cambiando)
    if (ALM && ALM.key[0] === 'm') alarmOff();
    if (!SV.otSeen || SV.otSeen !== otc.id) { SV.otSeen = otc.id; fb(); }
    sheet = null; A.innerHTML = `${head()}<div class=ph>${otView(otc)}</div>`; tick(); return;
  }
  (S.messages || []).forEach(m => { me.msgs = me.msgs || []; if (!me.msgs.some(x => x.id === m.id)) { me.msgs.unshift(m); me.msgs = me.msgs.slice(0, 20); save(); } });
  let body;
  if (S.kind === 'linea') {
    if (!OPT.some(x => x[0] === tab)) tab = 'inicio';
    body = TABS(OPT) + ({inicio: lineHome, historial: lineHist, cola: lineQueue, ficha, ajustes: settings}[tab])();
  } else if (S.kind === 'patinador') {
    const L = withOT(MOT); if (!L.some(x => x[0] === tab)) tab = 'inicio';
    body = TABS(L) + ({inicio: movHome, historial: movHist, cola: movQueue, extras: otTab, ajustes: settings}[tab])();
  } else if (S.kind === 'supernumerario') {
    const L = withOT(SUP); if (!L.some(x => x[0] === tab)) tab = 'inicio';
    body = TABS(L) + ({inicio: supHome, material: supMaterial, personal: supPeople, historial: supHist, lineas: supLines, extras: otTab, ajustes: settings}[tab])();
  } else {
    const L = withOT(OPT); if (!L.some(x => x[0] === tab)) tab = 'inicio';
    body = TABS(L) + ({inicio: opHome, historial: opHist, cola: opQueue, ficha, extras: otTab, ajustes: settings}[tab])();
  }
  if (tab !== 'inicio') SV.takt = null;
  const bk = breakOverlay(), focused = ae && ae.id === 'bkc' ? ae.value : null;
  A.innerHTML = `${head()}<div class=ph>${body}</div>${sheetHtml()}${bk || msgOverlay()}`;
  if (focused !== null) { const i = $('#bkc'); if (i) { i.value = focused; i.focus(); } }
  if (Date.now() - (SV.tap || 0) < 200) { const g = $('.rr .rw .rg'); if (g) g.classList.add('tapped'); }   // respuesta visual al toque
  if (sheet === 'rep') repPreview();
  // mensaje sin aceptar: alarma fuerte hasta que lo acepten (si hay pausa en pantalla, espera a que termine)
  const pm = bk ? null : pendingMsg();
  if (pm && pm.alarm !== false) alarmOn('m' + pm.id, {title: '🔔 Mensaje de ' + pm.from, body: pm.text});
  else {
    if (ALM && ALM.key[0] === 'm') alarmOff();
    if (pm && SV.quiet !== pm.id) { SV.quiet = pm.id; fb(); }   // sin alarma: solo el aviso normal (si está activado)
  }
  tick();
}
// ---------------------------------------------------------------- horas extra: convocatoria de planeación
// Mientras haya una sin responder, la tablet solo muestra esta pantalla: SÍ (días + hasta qué hora) o NO (motivo).
const withOT = L => S && S.overtime && (S.overtime.calls || []).length ? [...L.slice(0, -1), ['extras', 'Extras'], L[L.length - 1]] : L;
function otCall() {
  const o = S && S.overtime; if (!o) return null;
  if (SV.otEdit) { const c = (o.calls || []).find(x => x.id === SV.otEdit && x.can_change); if (c) return c; SV.otEdit = null; }
  return o.pending || null;
}
function otState(c) {
  if (!SV.ot || SV.ot.id !== c.id) {
    const picks = {}; c.days.forEach(d => { if (d.mine) picks[d.id] = d.mine; });
    SV.ot = {id: c.id, step: 'ask', picks, prev: {}, reason: c.answer === 'no' ? c.reason || '' : ''};
  }
  return SV.ot;
}
function otView(c) {
  const st = otState(c), back = SV.otEdit ? '<button data-a=otcancel>Volver sin cambiar</button>' : '';
  const head = `<span class="chip lk">🕒 Horas extra</span><h2 style="font-size:clamp(22px,6vw,30px);margin:8px 0 4px">${E(c.title)}</h2>${c.note ? `<div class=otn>${E(c.note)}</div>` : ''}`;
  if (st.step === 'no') {
    return `<div class="c otc">${head}<b style="display:block;margin-top:10px;font-size:18px">¿Por qué no puedes?</b>
      <div class=otr>${((S.overtime || {}).reasons || []).map(r => `<button class="${st.reason === r ? 'pr' : ''}" data-a=otreason data-v="${E(r)}">${E(r)}</button>`).join('')}</div>
      <textarea id=otr rows=3 maxlength=255 placeholder="Escribe el motivo…">${E(st.reason)}</textarea>
      <div class=otb2><button data-a=otstep data-v=ask>Atrás</button><button class=pr data-a=otsend data-v=no>Enviar respuesta</button></div></div>`;
  }
  if (st.step === 'yes') {
    const rows = c.days.map(d => {
      const on = st.picks[d.id] != null;
      return `<div class="otd${on ? ' on' : ''}"><button class=otck data-a=otday data-v="${d.id}" aria-pressed="${on}" aria-label="${E(d.label)}">${on ? '✔' : ''}</button>
        <span class=otdl data-a=otday data-v="${d.id}"><b>${E(d.label)}</b><small>${E(d.kind)} · de ${d.start} a ${d.end}${d.needed ? ' · se necesitan ' + d.needed : ''} · van ${d.count}</small></span>
        ${on ? `<label class=otu>Hasta<input type=time data-otu="${d.id}" value="${E(st.picks[d.id])}" min="${d.start}" max="${d.end}" step=900></label>` : ''}</div>`;
    }).join('');
    return `<div class="c otc">${head}<b style="display:block;margin-top:10px;font-size:18px">Elige los días que puedes y hasta qué hora</b>
      <small>Toca cada día y, con el reloj, la hora hasta la que te puedes quedar.</small><div class=otl>${rows}</div>
      <div class=otb2><button data-a=otstep data-v=ask>Atrás</button><button class=pr data-a=otsend data-v=yes>Confirmar</button></div></div>`;
  }
  return `<div class="c otc">${head}<b style="display:block;margin-top:10px;font-size:18px">¿Puedes hacer horas extra estos días?</b>
    <div class=otds>${c.days.map(d => `<span class=chip>${E(d.label)} · ${d.start}–${d.end}</span>`).join('')}</div>
    ${c.respond_by ? `<small style="display:block;margin-top:8px">Responde antes del ${dT(c.respond_by)} a las ${hT(c.respond_by)}.</small>` : ''}
    <div class=otyn><button class=otyes data-a=otstep data-v=yes>✔ SÍ puedo</button><button class=otno data-a=otstep data-v=no>✖ NO puedo</button></div>
    ${back ? `<div style="margin-top:10px">${back}</div>` : ''}<small style="display:block;margin-top:10px">Mientras no respondas no se habilita la tablet.</small></div>`;
}
function otTab() {
  const o = S.overtime || {calls: []};
  const nm = n => { const p = String(n || '').split(' '); return E(p[0]) + (p[1] ? ' ' + E(p[1][0]) + '.' : ''); };
  return `<h2>Horas extra</h2><small>Días programados para extras, su horario y quién va.</small>` + o.calls.map(c => {
    const ans = c.answer === 'si' ? '<span class="chip ok">Respondiste SÍ</span>' : c.answer === 'no' ? '<span class="chip rd">Respondiste NO</span>' : '<span class=chip>Sin responder</span>';
    return `<div class=c><div class=r2><div style="min-width:0"><b style="font-size:18px">${E(c.title)}</b>${c.note ? `<small style="display:block">${E(c.note)}</small>` : ''}</div>${ans}</div>
      ${c.answer === 'no' && c.reason ? `<small style="display:block;margin-top:6px">Tu motivo: ${E(c.reason)}</small>` : ''}
      ${c.days.map(d => `<div class="otday${d.mine ? ' mine' : ''}"><div class=r2 style="flex-wrap:nowrap"><div style="min-width:0"><b>${E(d.label)}</b> <span class=chip>${E(d.kind)}</span>
        <small style="display:block">de ${d.start} a ${d.end}${d.mine ? ` · <b class=ok>tú hasta las ${d.mine}</b>` : ''}</small></div><b class=n2 style="font-size:22px">${d.count}${d.needed ? `<small>/${d.needed}</small>` : ''}</b></div>
        ${d.going.length ? `<div class=otgo>${d.going.map((g, i) => `<span class=cp>${avc(g.name, i, '26px')}<span>${nm(g.name)}<small>hasta ${g.until}</small></span></span>`).join('')}</div>` : '<small>Aún no va nadie.</small>'}</div>`).join('')}
      ${c.can_change ? `<button style="width:100%;margin-top:12px" data-a=otedit data-v="${c.id}">Cambiar mi respuesta</button>` : ''}</div>`;
  }).join('');
}

// ---------------------------------------------------------------- llegada: a dónde va hoy + confirmar asistencia
const needCheckin = () => !!(S && S.checkin && !S.checkin.done);
function checkinView() {
  const c = S.checkin.info || {}, h = new Date(now()).getHours();
  const hi = h < 12 ? 'Buenos días' : h < 19 ? 'Buenas tardes' : 'Buenas noches';
  const row = (ic, k, v, sub) => v ? `<div class=ckr><span class=cki>${ic}</span><span class=ckv><small>${k}</small><b>${v}</b>${sub ? `<small>${sub}</small>` : ''}</span></div>` : '';
  const sn = c.supernumeraries || [];
  const opSub = [E(c.order), c.start ? 'programada ' + dT(c.start) + ' ' + hT(c.start) : ''].filter(Boolean).join(' · ');
  return `<div class="c ckin"><small class=ckh>${hi}, ${E(c.name || '')}</small><h2 style="font-size:clamp(24px,6vw,32px);margin:4px 0 2px">Hoy vas a…</h2>
    <small>Revisa a dónde vas y con quién. Al aceptar confirmas tu asistencia de hoy.</small>
    <div class=ckl>
      ${row('🏭', 'Bodega', E(c.site))}
      ${row('🧩', 'Línea', E(c.line))}
      ${row('🛠️', 'Operación', E(c.operation), opSub)}
      ${row('👷', 'Supervisor', E(c.supervisor) || '<span class=ckna>Sin asignar</span>')}
      ${c.line ? row('🤝', sn.length > 1 ? 'Supernumerarios' : 'Supernumerario', sn.map(E).join(', ') || '<span class=ckna>Sin asignar</span>') : ''}
      ${row('🧭', 'Tus líneas', (c.lines || []).map(E).join(' · '))}
      ${row('⏰', 'Turno', E(c.shift))}
      ${c.deadline ? row('🕒', 'Llegada hasta', hT(c.deadline)) : ''}
    </div>
    ${c.deadline ? (now() > T(c.deadline)
      ? `<div class="cknote late">Pasaste la hora máxima de llegada (<b>${hT(c.deadline)}</b>): tu asistencia quedará como <b>«Llegó tarde»</b>.</div>`
      : `<div class=cknote>Confirma antes de las <b>${hT(c.deadline)}</b> para quedar a tiempo.</div>`) : ''}
    ${c.operation ? (c.more ? `<small style="display:block;margin-top:10px">+${c.more} operación${c.more === 1 ? '' : 'es'} más en tu cola.</small>` : '')
      : `<div class=cknote>${c.role && c.role !== 'Operario' ? `Entras como <b>${E(c.role)}</b>.` : 'Aún no tienes una operación programada: tu supervisor te la asignará.'} Igual confirma tu llegada.</div>`}
    ${c.waiting ? `<small style="display:block;margin-top:6px">🔒 ${c.waiting} en espera de programación.</small>` : ''}
    <button class=pr data-a=checkin style="width:100%;min-height:66px;font-size:clamp(18px,5vw,21px);margin-top:16px">✔ Aceptar y confirmar mi asistencia</button>
    <div class=ckf><small>Hora de llegada: <b id=clk2>${hT(now())}</b></small><button class=sm data-a=logout>No soy yo · Salir</button></div></div>`;
}
function tick() {
  const k2 = $('#clk2'); if (k2) k2.textContent = hT(now());
  document.querySelectorAll('[data-lt]').forEach(e => { e.textContent = fmt((now() - T(e.dataset.lt)) / 1e3 + (+e.dataset.base || 0)); });
  const c = $('#clk'); if (c) c.textContent = new Date(now()).toLocaleTimeString('es-CO', {hour: '2-digit', minute: '2-digit', hour12: false});
  const b = $('#bkt'); if (b) { const r = (+b.dataset.end - now()) / 1e3; if (r <= 0) render(); else b.textContent = fmt(Math.ceil(r)); }
  const i = $('#bkc'); if (i && document.activeElement !== i && !i.disabled) i.focus();
  document.querySelectorAll('[data-due]').forEach(el => {   // cuentas regresivas hasta el próximo corte
    const due = T(el.dataset.due), st = T(el.dataset.st), rem = (due - now()) / 1e3, a = Math.abs(rem);
    el.textContent = (rem < 0 ? '−' : '') + (a >= 3600 ? hms(a) : mmss(a));
    el.classList.toggle('rd', rem < 0);
    const g = el.closest('.rg'), rb = g && g.querySelector('.rb');
    if (rb) { const C = 2 * Math.PI * 52, f = rem < 0 ? 1 : Math.min(1, Math.max(0, (now() - st) / ((due - st) || 1))); rb.setAttribute('stroke-dasharray', f * C + ' ' + C); rb.setAttribute('stroke', rem < 0 ? 'var(--rd)' : rem < 300 ? 'var(--yl)' : 'var(--a2)'); }
    if (rem <= 0 && !el.dataset.noalert) { SV.al = SV.al || {}; if (!SV.al[el.dataset.due]) { SV.al[el.dataset.due] = 1; dueAlert(); } }
  });
  const t = $('#tm');
  if (t && SV.takt && SV.takt.t) {
    const s = (now() - SV.takt.t) / 1e3; t.textContent = t.dataset.h ? hms(s) : mmss(s);
    const g = $('#tg'); if (g && SV.takt.k) { const C = 2 * Math.PI * 52; g.setAttribute('stroke-dasharray', Math.min(1, s / SV.takt.k) * C + ' ' + C); g.setAttribute('stroke', s > SV.takt.k ? 'var(--rd)' : '#d9d8e3'); }
  }
}
function showDone(t) {
  if (DN) return;
  const el = document.createElement('div'); el.className = 'dn';
  el.innerHTML = `<div class=c><div class=dck>✔</div><b style="font-size:24px">¡Tarea completada!</b><div class=k>${E(t.name)} · pedido ${E(t.order)}</div><small>${S && S.kind === 'linea' ? 'La línea completó el 100% de la operación. Quedó reportada en el MES.' : 'Se completó el 100% de la operación y quedó reportada en el MES.'}</small><div class=k id=dnx></div></div>`;
  document.body.appendChild(el); DN = el; fb(true);
  let n = 4; const x = () => { const d = $('#dnx'); if (d) d.textContent = n > 0 ? 'Cargando la siguiente tarea en ' + n + '…' : 'Cargando…'; }; x();
  const iv = setInterval(() => { n--; x(); if (n <= 0) { clearInterval(iv); el.remove(); DN = null; tab = 'inicio'; load(); } }, 1e3);
}

// ---------------------------------------------------------------- acciones
const act = {
  nop() {}, tab(b) { tab = b.dataset.v; scrollTo(0, 0); render(); if (tab === 'historial' && S && S.kind === 'operario' && !(SV.myrec || {}).rows) loadMyRec(false); }, reload() { load(); toast('Actualizado', 'ok'); },
  sheet(b) { sheet = b.dataset.v || null; if (!sheet) { SV.emp = null; SV.wc = null; } render(); if (sheet === 'qty' || sheet === 'ref') setTimeout(() => { const i = $(sheet === 'qty' ? '#qn' : '#rscan'); if (i) i.focus(); }, 50); },
  mark(b) {
    if (DN) return;
    const k = b.dataset.k || 'p';
    if (sheet) sheet = null;
    queueMark(k, 1);
    if (k !== 'p') toast(k === 'x' ? 'Rechazo registrado' : 'Reproceso registrado', 'ok');
  },
  async markqty() {
    const v = ($('#qn') || {}).value; if (!(+v > 0)) return toast('Escribe la cantidad.');
    document.activeElement && document.activeElement.blur(); sheet = null;
    if (!S.task) return render();
    if (+v > S.task.pending) { render(); return toast('Solo faltan ' + q(S.task.pending) + ' ' + S.task.unit + '.'); }
    const it = curRef(S.task);
    if (refOf(S.task) && !it) { sheet = 'ref'; render(); return toast('Primero elige o escanea la referencia (EAN).'); }
    if (it && it.left != null && +v > it.left) { render(); return toast('De la referencia ' + it.ean + ' solo faltan ' + q(it.left) + '.'); }
    await flushMarks();   // lo que estaba en cola va primero
    localMark('p', +v); paint(); busy = true;
    try { const r = await api('marcar', {k: 'p', q: v, op: S.task.id, e: it ? it.ean : ''}); fb(r.finished); if (r.finished) showDone(r.task); else toast('✔ ' + v + ' unidades marcadas', 'ok'); }
    catch (e) { if (!e.login) toast(e.message, e.done ? 'ok' : ''); } finally { busy = false; load(); }
  },
  flag() {
    if (S.kind === 'linea') { sheet = 'who'; return render(); }
    const st = S.stop;
    if (st && !st.is_break) return act.close({dataset: {v: st.id}});
    SV.emp = null; sheet = 'stop'; render();
  },
  async stop(b) {
    sheet = null; const body = {cause: b.dataset.v}; if (SV.emp) body.emp = SV.emp; if (SV.wc) body.wc = SV.wc; SV.emp = null; SV.wc = null;
    try { await api('paro', body); await load(); } catch (e) { if (!e.login) toast(e.message); render(); }
  },
  linestop() { SV.emp = null; sheet = 'stop'; render(); },
  crewstop(b) {
    const o = (S.crew || []).find(x => String(x.id) === b.dataset.v); if (!o) return;
    if (o.stop) { if (o.stop.is_break) { sheet = null; render(); return toast('Está en su ' + o.stop.cause.toLowerCase() + ': retoma con el QR.'); } sheet = null; return act.close({dataset: {v: o.stop.id}}); }
    SV.emp = o.id; SV.empName = o.name; sheet = 'stop'; render();
  },
  async close(b) { sheet = null; try { await api('paro/cerrar', {id: +b.dataset.v}); fb(); await load(); } catch (e) { if (!e.login) toast(e.message); render(); } },
  async brk(b) { sheet = null; try { await api('pausa', {kind: b.dataset.v}); await load(); } catch (e) { if (!e.login) toast(e.message); render(); } },
  async accept(b) {
    alarmOff();
    if (S.kind === 'linea') { me.seen = [+b.dataset.v, ...(me.seen || [])].slice(0, 50); save(); }   // la tablet de línea no tiene «leído» en el servidor
    else try { await api('mensaje/leido', {id: +b.dataset.v}); } catch (e) { /* se reintenta al recargar */ }
    S.messages = (S.messages || []).filter(m => String(m.id) !== b.dataset.v); render();
  },
  pmat(b) { SV.pwc = b.dataset.v; sheet = 'pmov'; render(); },
  async pmsend() {
    const body = {o: ($('#pmo') || {}).value || null, est: +($('#pme') || {}).value || 1, nota: ($('#pmn') || {}).value || '', pri: +($('#pmp') || {}).value || 0};
    const mm = ($('#pmm') || {}).value; if (mm !== undefined && mm !== '') body.m = +mm;   // vacío = el patinador de la línea
    if (S.kind === 'supernumerario') { if (!SV.pwc) return toast('Elige la línea.'); body.wc = +SV.pwc; }
    document.activeElement && document.activeElement.blur(); sheet = null;
    try { const r = await api('traslado/pedir', body); toast('✔ Pedido enviado' + (r.mover ? ' a ' + r.mover : ': abierto para cualquier patinador'), 'ok'); await load(); } catch (e) { if (!e.login) toast(e.message); render(); }
  },
  async madv(b) {
    if (!b.dataset.v || busy) return; busy = true; const was = S.current && String(S.current.id) === b.dataset.v ? S.current.status : null;
    try { await api('traslado/avanzar', {id: +b.dataset.v}); fb(was === 'c'); await load(); } catch (e) { if (!e.login) toast(e.message); } finally { busy = false; render(); }
  },
  async minc(b) { sheet = null; if (!S.current) return render(); try { await api('traslado/incidencia', {id: S.current.id, c: b.dataset.v}); await load(); } catch (e) { if (!e.login) toast(e.message); render(); } },
  pstop(b) {   // supernumerario: poner o quitar el paro de una persona
    const c = ((S.people || {}).cards || []).find(x => String(x.id) === b.dataset.v); if (!c) return;
    if (c.stop) { if (confirm('¿Reanudar a ' + c.name + ' (' + c.stop.cause + ')?')) act.close({dataset: {v: c.stop.id}}); return; }
    SV.emp = c.id; SV.wc = null; SV.empName = c.name; sheet = 'stop'; render();
  },
  plstop(b) {
    const l = ((S.people || {}).lines || []).find(x => String(x.id) === b.dataset.v); if (!l) return;
    if (l.stop) { if (confirm('¿Reanudar la línea ' + l.code + '?')) act.close({dataset: {v: l.stop.id}}); return; }
    SV.emp = null; SV.wc = l.id; SV.empName = 'Paro de toda la línea ' + l.code; sheet = 'stop'; render();
  },
  pmsg(b) { const c = ((S.people || {}).cards || []).find(x => String(x.id) === b.dataset.v); if (!c) return; SV.msg = {to: 'p', ids: [c.id], label: c.name}; SV.rv = {}; sheet = 'smsg'; render(); },
  lmsg(b) { const l = ((S.people || {}).lines || []).find(x => String(x.id) === b.dataset.v); if (!l) return; SV.msg = {to: 'l', line: l.id, label: 'línea ' + l.code}; SV.rv = {}; sheet = 'smsg'; render(); },
  amsg() { SV.msg = {to: 'a', label: 'todo mi personal'}; SV.rv = {}; sheet = 'smsg'; render(); },
  quick(b) { const t = $('#smt'); if (t) { t.value = b.textContent; SV.rv = {smt: t.value}; } },
  async smsgsend(b, buzz = false) {   // buzz = «solo zumbido»: alarma sin escribir nada
    const text = buzz ? '' : (($('#smt') || {}).value || '').trim(); if (!buzz && !text) return toast('Escribe el mensaje (o usa «Solo zumbido»).');
    const alarm = buzz || !!($('#sma') || {}).checked; SV.noAlarm = !alarm && !buzz;
    document.activeElement && document.activeElement.blur(); if (busy) return; busy = true;
    try { const j = await api('mensaje', {...SV.msg, text, alarm}); sheet = null; if (!buzz) SV.rv = {}; toast('✔ ' + (buzz ? 'Zumbido enviado' : 'Mensaje enviado') + ' a ' + j.audience + ' (' + j.n + ')', 'ok'); await load(); }
    catch (e) { if (!e.login) toast(e.message); } finally { busy = false; render(); }
  },
  buzz(b) { return act.smsgsend(b, true); },
  async testalarm() {
    const x = audio(); if (x && x.state !== 'running') await x.resume().catch(() => {});
    keepAlive();
    if (!siren(3)) toast('El navegador no deja sonar: revisa que el equipo no esté en silencio y vuelve a tocar.');
    if (navigator.vibrate) navigator.vibrate([600, 200, 600]);
    if (notifOk()) try { SWR.showNotification('🔊 Prueba de alarma', {body: 'Así llegan los mensajes cuando la app no está en pantalla.', tag: 'prueba', renotify: true, vibrate: [600, 200, 600], icon: CFG.icon}); } catch (e) { /* sin avisos */ }
    render();
  },
  async install() { if (!INST) return; INST.prompt(); try { await INST.userChoice; } catch (e) { /* cancelado */ } INST = null; render(); },
  async notif() { try { await Notification.requestPermission(); } catch (e) { /* sin avisos */ } render(); },
  rep(b) { SV.rep = {op: b.dataset.v, how: 'total'}; SV.rv = {}; sheet = 'rep'; render(); setTimeout(() => { const i = $('#rref') && !$('#rref').value ? $('#rscan') : $('#rg'); if (i) i.focus(); }, 50); },
  refpick(b) { pickRef(b.dataset.v); },
  eans(b) { SV.eop = b.dataset.v; SV.rep = null; sheet = 'eans'; render(); },
  async eanc(b) {
    if (busy) return; busy = true;
    try { await api('referencia', {op: +SV.eop, e: b.dataset.v, a: b.dataset.k}); await load(); toast({abrir: 'Referencia abierta', cerrar: 'Referencia cerrada', solo: 'Solo queda abierta ' + b.dataset.v, todas: 'Todas abiertas'}[b.dataset.k] || 'Orden actualizado', 'ok'); }
    catch (e) { if (!e.login) toast(e.message); } finally { busy = false; }
  },
  myrec(b) { loadMyRec(b.dataset.v === 'h'); },
  rephow(b) { if (SV.rep) SV.rep.how = b.dataset.v; render(); },
  async repsend() {
    const f = findRep(); if (!f || busy) return;
    const body = {op: f.o.id}, num0 = v => +String(v || '').replace(',', '.') || 0;
    body.l = ($('#rl') || {}).value || 0; body.e = ($('#rref') || {}).value || '';
    if (SV.rep.how === 'persona' && (f.o.crew_list || []).length) {
      body.per = f.o.crew_list.map(c => ({id: c.id, g: ($(`[data-pg="${c.id}"]`) || {}).value || 0, x: ($(`[data-px="${c.id}"]`) || {}).value || 0}))
        .filter(x => num0(x.g) > 0 || num0(x.x) > 0);
      if (!body.per.length && !(num0(body.l) > 0)) return toast('Escribe lo de al menos una persona.');
    } else {
      body.g = ($('#rg') || {}).value || 0; body.x = ($('#rx') || {}).value || 0;
      if (!(num0(body.g) > 0 || num0(body.x) > 0 || num0(body.l) > 0)) return toast('Escribe cuántas unidades se hicieron.');
    }
    const goodAll = body.per ? body.per.reduce((a, x) => a + num0(x.g), 0) : num0(body.g);
    if (refOf(f.o) && (goodAll > 0 || num0(body.l) > 0) && !body.e) return toast('Elige o escanea la referencia (EAN).');
    if (body.e) { me.ref = me.ref || {}; me.ref[f.o.id] = body.e; save(); }
    document.activeElement && document.activeElement.blur(); busy = true;
    try {
      const j = await api('reportar', body); sheet = null; SV.rv = {}; fb(j.finished);
      toast('✔ Reporte enviado: ' + q(j.good) + ' buenas' + (j.people > 1 ? ' repartidas entre ' + j.people + ' personas' : ''), 'ok');
      if (j.finished) showDone(j.task);
      await load();
    } catch (e) { if (!e.login) toast(e.message, e.done ? 'ok' : ''); } finally { busy = false; render(); }
  },
  async repown() {
    if (busy || !S.task) return;
    const g = ($('#rg') || {}).value || 0, x = ($('#rx') || {}).value || 0, n = v => +String(v).replace(',', '.') || 0;
    const l = ($('#rl') || {}).value || 0, e = ($('#rref') || {}).value || '';
    if (!(n(g) > 0 || n(x) > 0 || n(l) > 0)) return toast('Escribe cuántas unidades hiciste.');
    if (refOf(S.task) && (n(g) > 0 || n(l) > 0) && !e) return toast('Elige o escanea la referencia (EAN).');
    if (e) { me.ref = me.ref || {}; me.ref[S.task.id] = e; save(); }
    document.activeElement && document.activeElement.blur(); busy = true;
    try {
      const j = await api('reportar', {op: S.task.id, g, x, l, e}); SV.rv = {}; fb(j.finished);
      toast('✔ Reporte enviado: ' + q(j.good) + ' buenas', 'ok');
      if (j.finished) showDone(j.task);
      await load();
    } catch (e) { if (!e.login) toast(e.message, e.done ? 'ok' : ''); } finally { busy = false; render(); }
  },
  async undo(b) {
    if (!confirm('¿Anular este reporte? Se quitan sus unidades del pedido y del personal.')) return;
    try { await api('reporte/anular', {id: +b.dataset.v}); toast('Reporte anulado', 'ok'); await load(); } catch (e) { if (!e.login) toast(e.message); }
  },
  async supline(b) {
    try { await api('lineas', {wc: +b.dataset.v, on: b.dataset.on === '1'}); await load(); } catch (e) { if (!e.login) toast(e.message); }
  },
  cfs(b) { setCF(b.dataset.k, b.dataset.v); render(); },
  cfr() { if (!confirm('¿Restablecer tus ajustes de pantalla?')) return; if (me.cfgs) delete me.cfgs[ukey()]; save(); applyCfg(); render(); },
  otstep(b) { const c = otCall(); if (!c) return; const t = $('#otr'); if (t) otState(c).reason = t.value; otState(c).step = b.dataset.v; document.activeElement && document.activeElement.blur(); render(); },
  otday(b) {
    const c = otCall(); if (!c) return; const st = otState(c), d = c.days.find(x => String(x.id) === b.dataset.v); if (!d) return;
    document.querySelectorAll('[data-otu]').forEach(i => { st.picks[i.dataset.otu] = i.value; });
    if (st.picks[d.id] != null) { st.prev[d.id] = st.picks[d.id]; delete st.picks[d.id]; } else st.picks[d.id] = st.prev[d.id] || d.end;
    document.activeElement && document.activeElement.blur(); render();
  },
  otreason(b) { const c = otCall(); if (!c) return; const st = otState(c); st.reason = b.dataset.v === 'Otro' ? '' : b.dataset.v; render(); if (b.dataset.v === 'Otro') { const t = $('#otr'); if (t) t.focus(); } },
  otcancel() { SV.otEdit = null; SV.ot = null; render(); },
  otedit(b) { SV.otEdit = +b.dataset.v; SV.ot = null; render(); },
  async otsend(b) {
    const c = otCall(); if (!c || SV.otBusy) return; const st = otState(c), yes = b.dataset.v === 'yes';
    const t = $('#otr'); if (t) st.reason = t.value;
    document.querySelectorAll('[data-otu]').forEach(i => { st.picks[i.dataset.otu] = i.value; });
    const picks = Object.entries(st.picks).filter(([, h]) => h).map(([d, h]) => ({d: +d, h}));
    if (yes && !picks.length) return toast('Toca al menos un día en que puedas.');
    if (!yes && (st.reason || '').trim().length < 3) return toast('Escribe el motivo por el que no puedes.');
    SV.otBusy = true; b.disabled = true;
    try {
      const r = await api('extras', {req: c.req, yes, reason: st.reason, picks});
      SV.ot = null; SV.otEdit = null; if (yes) tab = 'extras';
      applyState(r.state); fb(); toast(yes ? '✔ Quedaste anotado para horas extra' : '✔ Respuesta enviada', 'ok');
    } catch (e) { if (!e.login) { toast(e.message); b.disabled = false; } }
    finally { SV.otBusy = false; }
  },
  async checkin(b) {
    if (SV.ckBusy) return; SV.ckBusy = true; b.disabled = true; b.textContent = 'Confirmando…';
    const late = !!(S.checkin && S.checkin.info && S.checkin.info.deadline && now() > T(S.checkin.info.deadline));
    try { const r = await api('llegada', {}); applyState(r.state); fb(); toast('✔ Asistencia confirmada · ' + hT(now()) + (late ? ' · llegada tarde' : ''), late ? '' : 'ok'); }
    catch (e) { if (!e.login) { toast(e.message); b.disabled = false; b.textContent = '✔ Aceptar y confirmar mi asistencia'; } }
    finally { SV.ckBusy = false; }
  },
  async logout() { sheet = null; try { await api('salir', {}); } catch (e) { /* igual se sale */ } S = null; pinBuf = ''; loginErr = ''; tab = 'inicio'; render(); },
  lm(b) { loginMode = b.dataset.v; loginErr = ''; render(); },
  pk(b) { const v = b.dataset.v; if (v === 'C') pinBuf = pinBuf.slice(0, -1); else if (pinBuf.length < 8) pinBuf += v; const d = $('#doc'); if (d) me.lastDoc = d.value; render(); },
  async bkgo(i) {
    const v = i.value.trim(); if (!v || SV.bkBusy) return; SV.bkBusy = true; i.disabled = true;
    try { await api('reanudar', {code: v}); SV.bkErr = ''; await load(); }
    catch (e) { SV.bkErr = e.message; i.disabled = false; i.value = ''; const p = $('#bke'); if (p) p.textContent = SV.bkErr; i.focus(); }
    finally { SV.bkBusy = false; }
  },
};
A.addEventListener('click', e => { const b = e.target.closest('[data-a]'); if (!b || !A.contains(b)) return; if (b.dataset.a === 'sheet' && e.target !== b && b.classList.contains('sh')) return; const f = act[b.dataset.a]; if (f) { e.preventDefault(); f(b); } });
A.addEventListener('change', e => { const t = e.target; if (t.dataset.c === 'cf') { t.blur(); setCF(t.dataset.k, t.type === 'checkbox' ? t.checked : t.value.trim()); render(); } });
A.addEventListener('input', e => {
  const t = e.target; if (t.id === 'bkc' && t.value.trim().length >= 12) act.bkgo(t); if (t.id === 'doc') { me.lastDoc = t.value; save(); }
  // lo escrito en un reporte se guarda para que no se pierda si la pantalla se actualiza
  const k = ['rg', 'rx', 'rl', 'rref', 'smt'].includes(t.id) ? t.id : t.dataset.pg ? 'g' + t.dataset.pg : t.dataset.px ? 'x' + t.dataset.px : null;
  if (k) { SV.rv = SV.rv || {}; SV.rv[k] = t.value; }
  if (SV.ot && t.dataset.otu) SV.ot.picks[t.dataset.otu] = t.value;   // horas extra: lo elegido no se pierde al refrescar
  if (SV.ot && t.id === 'otr') SV.ot.reason = t.value;
  if (t.id === 'rg' && sheet === 'rep') repPreview();
});
A.addEventListener('keydown', e => {
  const t = e.target; if (t.id === 'bkc' && e.key === 'Enter' && !t.disabled) act.bkgo(t);
  if (t.id === 'qn' && e.key === 'Enter') { e.preventDefault(); act.markqty(); }
  if (t.id === 'rscan' && e.key === 'Enter') {   // lector de código de barras: escribe el EAN y manda Enter
    e.preventDefault(); const v = t.value; t.value = '';
    if (t.dataset.for === 'pick') { pickRef(v); return; }
    const s = $('#' + t.dataset.for), code = normEan(v);
    if (!s || ![...s.options].some(o => o.value === code && !o.disabled)) return toast(s && [...s.options].some(o => o.value === code) ? 'Esa referencia está completa o cerrada.' : 'Ese EAN no es de este pedido.');
    s.value = code; SV.rv = SV.rv || {}; SV.rv[t.dataset.for] = code; toast('Referencia ' + code, 'ok');
    const g = $('#rg'); if (g) g.focus();
  }
  if ((t.id === 'rg' || t.id === 'rx' || t.id === 'rl') && e.key === 'Enter') { e.preventDefault(); if (sheet === 'rep') act.repsend(); else act.repown(); }
});
document.addEventListener('keydown', e => { if (!S && loginMode === 'op' && document.activeElement && document.activeElement.id !== 'doc' && /^[0-9]$/.test(e.key)) act.pk({dataset: {v: e.key}}); });
A.addEventListener('submit', async e => {
  e.preventDefault(); const f = e.target, d = Object.fromEntries(new FormData(f)); document.activeElement && document.activeElement.blur();
  if (f.dataset.f === 'login') {
    if (pinBuf.length < 4) { loginErr = 'Escribe tu PIN (mínimo 4 números).'; return render(); }
    me.lastDoc = d.doc; save();
    // la respuesta ya trae la pantalla del operario: aparece sin una segunda consulta
    try { const r = await api('entrar', {doc: d.doc, pin: pinBuf}); pinBuf = ''; loginErr = ''; tab = 'inicio'; if (r.state) applyState(r.state); else await load(); }
    catch (x) { pinBuf = ''; loginErr = x.message; render(); }
  } else if (f.dataset.f === 'pair') {
    try { const r = await api('vincular', {code: d.code}); loginErr = ''; tab = 'inicio'; if (r.state) applyState(r.state); else await load(); } catch (x) { loginErr = x.message; render(); }
  } else if (f.dataset.f === 'pin') {
    if (d.new !== d.new2) return toast('Los PIN nuevos no coinciden.');
    try { await api('pin', {pin: d.pin, new: d.new}); f.reset(); toast('✔ PIN actualizado', 'ok'); } catch (x) { if (!x.login) toast(x.message); }
  }
});
// Al tocar fuera de una hoja se cierra
document.addEventListener('click', e => { if (e.target.classList && e.target.classList.contains('sh') && sheet) { sheet = null; SV.emp = null; SV.wc = null; render(); } });
document.addEventListener('visibilitychange', () => { if (!document.hidden && S) { applyCfg(); load(); } });
setInterval(poll, 1500); setInterval(tick, 200); setInterval(() => { if (S) paintBg(); }, 30000);

// Vincular con el QR de la línea: /p/piso/?linea=CODIGO
const qs = new URLSearchParams(location.search), code = qs.get('linea');
if (code) {
  history.replaceState(null, '', location.pathname);
  api('vincular', {code}).then(r => { toast('✔ Tablet vinculada a ' + r.line, 'ok'); if (r.state) applyState(r.state); else load(); }).catch(e => { loginMode = 'line'; loginErr = e.message; booted = true; render(); });
} else {
  // La página ya trae el estado: se pinta al instante (sin «Cargando…» ni esperar otra consulta). Al rato se consulta
  // de nuevo para contarle al MES si este equipo puede sonar solo.
  let boot = null;
  try { const el = document.getElementById('piso-boot'); boot = el ? JSON.parse(el.textContent) : null; } catch (e) { boot = null; }
  if (boot && boot.login) { if (boot.closed) sessionClosed(); booted = true; render(); }
  else if (boot && boot.now) { applyState(boot); setTimeout(load, 1200); }
  else load();
}
})();
