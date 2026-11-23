// Asignación v2 · reemplaza asig() y asigM() de index.html sin tocar el resto.
// Cargar DESPUÉS del script principal:  <script src="asignacion.js"></script>  (justo antes de </body>)
//
// Qué cambia:
//  1. Una fila compacta por operario (semáforo, tarea, avance, tiempo libre); el detalle se abre solo al tocar la fila.
//  2. Filtros: Todos · Disponibles · Con tiempo libre · Excede · Con paro.
//  3. Pestañas: Operarios · Módulos · Traslados.
//  4. Horario general plegado + casillas para asignar desayuno/almuerzo solo a las personas elegidas (tandas).
// No requiere cambios en server.js: las pausas se guardan en los mismos campos di/df/ai/af de cada operario.
(() => {
if (window.__asigV2) return; window.__asigV2 = 1;

const AS = { tab: 'ops', f: 'all', open: null, sel: new Set(), g: false, bk: { di: '', df: '', ai: '', af: '' }, vis: [] };

const css = `
.asit{background:var(--c2);border:1px solid transparent;border-radius:16px;padding:4px 8px;margin-bottom:8px}
.asit.op{border-color:var(--ac)}.asit.sl{background:color-mix(in srgb,var(--ac) 14%,var(--c2))}
.asr{display:flex;align-items:center;gap:4px}
.asc{display:grid;place-content:center;width:44px;height:44px;flex:none;cursor:pointer}
.asc input{-webkit-appearance:none;appearance:none;width:22px;height:22px;min-height:0;padding:0;margin:0;border:2px solid var(--rt);border-radius:7px;background:var(--cd);position:relative;cursor:pointer}
.asc input:checked{background:var(--ac);border-color:var(--ac)}
.asc input:checked::after{content:"✓";position:absolute;inset:0;display:grid;place-content:center;color:#fff;font-size:14px;line-height:1}
.asm{flex:1;min-width:0;display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap;padding:8px 4px;cursor:pointer}
.asn{display:flex;align-items:center;gap:8px;flex:1 1 200px;min-width:0}.asn>span{min-width:0}.asn small,.ast small{display:block}
.asdot{width:12px;height:12px;border-radius:50%;flex:none}
.ast{flex:1 1 160px;min-width:0}
.asp{flex:0 1 120px;min-width:80px}.asp .pb{margin:0 0 4px}
.asm .chip{margin:0}.chev{color:var(--mu);width:14px;text-align:center}
.asd{border-top:1px solid var(--bd);padding:4px 4px 10px}.asd .ob{border-top:0}
.chip.on{outline:2px solid var(--ac)}
.asb{position:sticky;bottom:calc(env(safe-area-inset-bottom,0px) + 8px);z-index:6;background:var(--cd);border:1px solid var(--ac);border-radius:18px;padding:12px;box-shadow:0 8px 24px #0007;display:grid;gap:8px;margin-top:12px}
.asg2{display:grid;grid-template-columns:1fr 1fr;gap:10px}.asi{display:flex;gap:6px;align-items:center}.asi input{flex:1 1 0;min-width:0}
@media(max-width:480px){.asg2{grid-template-columns:1fr}}
`;
const sty = document.createElement('style'); sty.textContent = css; document.head.appendChild(sty);

// ---- Piezas del detalle (copiadas de la versión anterior: cuenta de 5 renglones, barra de tiempo, chips de capacidad) ----
const rn = x => E(by(S.refs, x)?.n || '—'),
sg = (a, b, J, k, tx, tt) => { const w = J.f - J.i, d = Math.min(b, J.f) - Math.max(a, J.i); return d > 0 ? `<i class="${k}" style="left:${Math.max(0, a - J.i) / w * 100}%;width:${d / w * 100}%" title="${E(tt || tx)}">${E(tx)}</i>` : ''; },
tlb = (o, p) => { const J = p.J, n = x => by(S.refs, x)?.n || '';
  return `<div class=tlb>${[p.c ? sg(p.c.a, p.c.b, J, 'sg0', 'Actual', 'Actual: ' + n(o.ref) + ' ' + hh(p.c.a) + '–' + hh(p.c.b)) : '', ...p.qs.map((z, i) => sg(z.a, z.b, J, 'sg' + (1 + i % 2), i + 1, (i + 1) + ': ' + n(z.x.ref) + ' ' + hh(z.a) + '–' + hh(z.b))), sg(p.t, J.f, J, 'fr', 'Libre', 'Libre ' + hh(p.t) + '–' + hh(J.f)), ...J.P.map(([a, b]) => sg(a, b, J, 'ps', '', 'Pausa ' + hh(a) + '–' + hh(b))), sg(J.i, p.t0, J, 'el', '', 'Ya transcurrido')].join('')}</div><div class=r2><small>${hh(J.i)}</small><small>Gris: ya pasó · rayas: pausas · verde: libre</small><small>${hh(J.f)}</small></div>`; },
ld = (o, p) => { const r = by(S.refs, o.ref), Q = p.qs.length, cur = p.c ? `${E(r?.n)} · faltan ${p.c.q} u × ${su(o, r)} s` : o.ref && !o.fin ? 'Sin meta: no se puede calcular' : 'Sin tarea: está disponible';
  return `<div class=ld>${p.ended ? `<small class=rd>La jornada ya terminó (${hh(p.J.f)}). Cambia a «Jornada completa» para planear el siguiente día.</small>` : ''}<div><span>Jornada neta<small>${hh(p.J.i)}–${hh(p.J.f)}, sin desayuno ni almuerzo</small></span><b>${dm(p.J.net)}</b></div>
 ${p.el > .5 ? `<div><span>Ya transcurrido<small>hasta las ${hh(p.t0)}</small></span><b>− ${dm(p.el)}</b></div>` : ''}
 <div><span>Tarea actual<small>${cur}</small></span><b>− ${dm(p.c ? p.c.m : 0)}</b></div>
 <div><span>Cola de trabajo<small>${Q} tarea${Q === 1 ? '' : 's'}</small></span><b>− ${dm(p.lq)}</b></div>
 <div class=tt><span>${p.free < 0 ? 'Excede la jornada' : 'Libre para asignar'}</span><b class="${p.free < 0 ? 'rd' : 'ok'}">${dm(Math.abs(p.free))}</b></div></div>`; },
cap = (o, p) => p.free > .5 ? `<div style="margin:6px 0"><small>Con ${dm(p.free)} libres caben (toca una para llenar la cola):</small><div>${S.refs.map(r => `<button class=chip data-a=qfill data-v="${o.id}:${r.id}">${E(r.n)} · ${fit(o, r, p.free)} u</button>`).join('')}</div></div>` : '',
det = o => { const p = plan(o), f = QF[o.id] || {}, Q = p ? p.qs : S.cola.filter(x => x.op === o.id).sort((a, b) => a.ord - b.ord).map(x => ({ x }));
  return `<div class=ob>${p ? ld(o, p) + tlb(o, p) + cap(o, p) : `<small>Falta el horario de ${E(o.n)}: ábrelo en «Horario» (abajo) o usa «Aplicar a todos» arriba.</small>`}
 <div><b>1 · Tarea actual</b><div class=ar style="border:0;padding:6px 0 0"><select data-c=asr data-v="${o.id}">${opts(S.refs, o.ref, 'Sin operación')}</select><select data-c=asl data-v="${o.id}">${lopt(o.lote)}</select><input type=number min=1 data-c=asm data-v="${o.id}" value="${o.meta || ''}" placeholder="Meta (u)"></div><small>${o.ref && !o.fin ? `Van ${doneN(o)} de ${o.meta || '?'} unidades${p && p.c ? ` · termina ≈ ${hh(p.c.b)}` : ' · pon la meta para contar su tiempo'}` : 'Sin tarea: lo que agregues abajo pasa a ser la tarea actual.'}</small></div>
 <div><b>2 · Cola de trabajo</b> <small>(lo que sigue, en orden)</small>${Q.map(({ x, m, a, b, ex }, i) => `<div class=qi><span><b>${i + 1}.</b> ${rn(x.ref)} · ${lbl(x.lote)} · <b>${x.meta} u</b>${p ? `<small style="display:block">${x.meta} u × ${su(o, by(S.refs, x.ref))} s = ${dm(m)} · ${hh(a)} → ${hh(b)}${ex ? ' <b class=rd>⚠ fuera de jornada</b>' : ''}</small>` : ''}</span><button data-a=qmv data-v="${x.id}:-1">▲</button><button data-a=qmv data-v="${x.id}:1">▼</button><button data-a=qdel data-v="${x.id}">✕</button></div>`).join('') || '<small style="display:block">Vacía.</small>'}</div>
 <div><b>3 · Agregar a la cola</b><div class=ar style="border:0;padding:6px 0 0"><select data-c=qr data-v="${o.id}">${opts(S.refs, f.ref, 'Operación…')}</select><select data-c=ql data-v="${o.id}">${lopt(f.lote)}</select><input type=number min=1 data-c=qq data-v="${o.id}" value="${f.q || ''}" placeholder="Unidades"><button data-a=qmax data-v="${o.id}">Máx.</button><button class=pr data-a=qadd data-v="${o.id}">＋ Agregar</button></div><small id="qh-${o.id}">${hint(o)}</small></div>
 <div><button data-a=tg data-v="${o.id}">${OPEN[o.id] ? '▾' : '▸'} Horario de ${E(o.n)}</button>${OPEN[o.id] ? sch(o, 'sch') : ''}</div></div>`; };

// ---- Estado de cada operario (semáforo) y filtros ----
const stat = (o, p) => { const z = S.stops.find(x => x.op === o.id && !x.e);
  if (z) return BRK.includes(z.c) ? { k: 'pausa', t: 'En pausa · ' + z.c, c: '#5aa7ff' } : { k: 'paro', t: 'Paro · ' + z.c, c: 'var(--rd)' };
  if (p && p.free < -.5) return { k: 'excede', t: 'Excede la jornada', c: 'var(--yl)' };
  if (!o.ref || o.fin) return { k: 'libre', t: o.fin ? '✔ Finalizada · Disponible' : 'Disponible', c: 'var(--ok)' };
  return { k: 'tarea', t: 'En tarea', c: 'var(--ac)' }; };
const FLT = {
  all: () => true,
  libre: r => r.s.k === 'libre',
  tiempo: r => !!r.p && r.p.free >= 15 && !['paro', 'pausa'].includes(r.s.k),
  excede: r => r.s.k === 'excede',
  paro: r => r.s.k === 'paro' },
FLB = [['all', 'Todos'], ['libre', 'Disponibles'], ['tiempo', 'Con tiempo libre'], ['excede', 'Excede'], ['paro', 'Con paro']];

// ---- Grupos de pausa (tandas): personas con exactamente los mismos horarios de desayuno y almuerzo ----
const sig = o => [o.di, o.df, o.ai, o.af].map(x => x || '').join('|'),
glab = g => { if (g === '|||') return 'Sin pausas'; const [di, df, ai, af] = g.split('|'), a = di && df ? `☕ ${di}–${df}` : '', b = ai && af ? `🍴 ${ai}–${af}` : ''; return [a, b].filter(Boolean).join(' · ') || 'Incompleto'; };

// ---- Aviso de cobertura: cruces con otras pausas y módulos que quedarían sin gente ----
const asHint = () => { const b = AS.bk,
  W = [['desayuno', b.di, b.df], ['almuerzo', b.ai, b.af]].map(([k, x, y]) => ({ k, a: hm(x), b: hm(y) })).filter(w => w.a != null && w.b != null && w.b > w.a);
  if (!W.length) return '<small>Elige la hora de desayuno y/o almuerzo y toca «Aplicar».</small>';
  const win = o => AS.sel.has(o.id) ? bkw({ di: b.di || o.di, df: b.df || o.df, ai: b.ai || o.ai, af: b.af || o.af }) : bkw(o),
    ov = (x, w) => x.a < w.b && w.a < x.b, out = [],
    others = S.ops.filter(o => !AS.sel.has(o.id) && win(o).some(x => W.some(w => ov(x, w)))).length;
  if (others) out.push(`<small>A esa hora ya están en pausa ${others} persona${others === 1 ? '' : 's'} fuera de tu selección.</small>`);
  new Set(S.ops.filter(o => AS.sel.has(o.id)).map(o => o.m)).forEach(m => { const L = S.ops.filter(o => o.m === m);
    W.forEach(w => { if (L.length > 1 && L.every(o => win(o).some(x => ov(x, w)))) out.push(`<small class=rd>⚠ En ${E(by(S.mods, m)?.n || 'el módulo')} todos tendrían pausa que se cruza con el ${w.k}: podría quedar sin nadie.</small>`); }); });
  return out.join('') || '<small class=ok>Sin cruces con otras pausas.</small>'; };

const row = ({ o, s, p }) => { const m = by(S.mods, o.m), r = by(S.refs, o.ref), dn = o.ref ? doneN(o) : 0, pc = o.ref && o.meta > 0 ? Math.min(100, dn / o.meta * 100) : 0,
  op = AS.open === o.id, on = AS.sel.has(o.id), pz = [o.di && o.df ? '☕ ' + o.di : '', o.ai && o.af ? '🍴 ' + o.ai : ''].filter(Boolean).join(' · ');
  return `<div class="asit${op ? ' op' : ''}${on ? ' sl' : ''}"><div class=asr><label class=asc><input type=checkbox data-a=assel data-v="${o.id}"${on ? ' checked' : ''}></label><div class=asm data-a=asopen data-v="${o.id}">
 <span class=asn><i class=asdot style="background:${s.c}" title="${E(s.t)}"></i>${av(o.n)}<span><b>${E(o.n)}</b><small>${E(m?.n || '')} · ${E(o.mq || '')}${pz ? ' · ' + pz : ''}</small></span></span>
 <span class=ast><b>${r ? E(r.n) : 'Sin tarea'}</b><small>${E(s.t)}</small></span>
 <span class=asp>${o.ref && !o.fin ? `<span class=pb><i style="width:${pc}%"></i></span><small>${dn}/${o.meta || '?'}</small>` : ''}</span>
 ${p ? (p.free < 0 ? `<span class="chip rd">Excede ${dm(-p.free)}</span>` : `<span class="chip ok">Libre ${dm(p.free)}</span>`) : '<span class=chip>Sin horario</span>'}<span class=chev>${op ? '▾' : '▸'}</span></div></div>${op ? `<div class=asd>${det(o)}</div>` : ''}</div>`; };

const asOps = () => {
  AS.sel = new Set([...AS.sel].filter(id => by(S.ops, id))); if (AS.open && !by(S.ops, AS.open)) AS.open = null;
  const rows = S.ops.map(o => { const p = plan(o); return { o, p, s: stat(o, p) }; }), cnt = {};
  FLB.forEach(([k]) => cnt[k] = rows.filter(FLT[k]).length);
  const vis = rows.filter(FLT[AS.f] || FLT.all); if (AS.f === 'tiempo') vis.sort((a, b) => b.p.free - a.p.free);
  AS.vis = vis.map(r => r.o.id);
  const allSel = AS.vis.length > 0 && AS.vis.every(id => AS.sel.has(id)),
    gh = me.gh || {}, J = jor(gh), gsm = gh.hi && gh.hs ? `Jornada ${E(gh.hi)}–${E(gh.hs)}` : 'Jornada sin definir',
    G = {}; S.ops.forEach(o => (G[sig(o)] = G[sig(o)] || []).push(o));
  const gl = Object.entries(G).sort((a, b) => (a[0] === '|||') - (b[0] === '|||') || b[1].length - a[1].length),
  gCard = `<div class=c style="margin-bottom:12px"><div class=r2><span><b>${gsm}</b><small style="display:block">${J ? dm(J.net) + ' netos por persona' : 'Defínela una vez y aplícala a todos'}</small></span><span class=sel><button data-a=asg>${AS.g ? '▾ Ocultar' : '▸ Editar'}</button><button class=pr data-a=gapl>Aplicar a todos</button></span></div>
 ${AS.g ? `<small style="display:block;margin:8px 0 0">Jornada neta = salida − ingreso − desayuno − almuerzo (sin paros). Cada unidad tarda SAM ÷ eficiencia objetivo. Deja desayuno y almuerzo vacíos aquí si salen por tandas: «Aplicar a todos» solo copia los campos que llenes, y las pausas por grupo se asignan abajo con las casillas.</small>${sch(null, 'gh')}<b id=gsum style="display:block;margin-top:10px">${gs()}</b>` : ''}</div>`,
  bar = AS.sel.size ? `<div class=asb><div class=r2><b>${AS.sel.size} seleccionado${AS.sel.size === 1 ? '' : 's'}</b><button data-a=ascl>Desmarcar todo</button></div>
 <div class=asg2><div><small>☕ Desayuno</small><div class=asi><input type=time data-c=as data-k=di value="${E(AS.bk.di)}"><span>a</span><input type=time data-c=as data-k=df value="${E(AS.bk.df)}"></div></div><div><small>🍴 Almuerzo</small><div class=asi><input type=time data-c=as data-k=ai value="${E(AS.bk.ai)}"><span>a</span><input type=time data-c=as data-k=af value="${E(AS.bk.af)}"></div></div></div>
 <div id=ashint>${asHint()}</div><div class=sel><button class=pr data-a=asap>Aplicar a seleccionados</button><button data-a=asqt>Quitar pausas</button></div></div>` : '';
  return `${gCard}<div class=c><div class=r2><b>Operarios</b><span class=sel><button class="${me.pm === 'now' ? '' : 'pr'}" data-a=pmo data-v=full>Jornada completa</button><button class="${me.pm === 'now' ? 'pr' : ''}" data-a=pmo data-v=now>Desde la hora actual</button></span></div>
 <small style="display:block;margin:6px 0">${me.pm === 'now' ? 'Descuenta lo que ya pasó hoy: sirve durante el turno.' : 'Planea el día completo, de ingreso a salida: sirve para armar la cola antes de empezar.'} Toca una fila para ver y editar a esa persona; usa las casillas para asignar pausas a varias a la vez.</small>
 <div class=r2 style="margin:8px 0"><span class=sel>${FLB.map(([k, t]) => `<button class="${AS.f === k ? 'pr' : ''}" data-a=asf data-v=${k}>${t} · ${cnt[k]}</button>`).join('')}</span><button data-a=assa>${allSel ? 'Desmarcar visibles' : 'Seleccionar visibles (' + AS.vis.length + ')'}</button></div>
 ${S.ops.length ? `<div style="margin:0 0 10px"><small>Grupos de pausa (toca uno para seleccionarlo):</small><div>${gl.map(([g, L]) => `<button class="chip${L.every(o => AS.sel.has(o.id)) ? ' on' : ''}" data-a=asgs data-v="${E(g)}">${E(glab(g))} · ${L.length}</button>`).join('')}</div></div>` : ''}
 ${vis.map(row).join('') || `<small>${S.ops.length ? 'Nadie cumple este filtro.' : 'Crea operarios en Configuración.'}</small>`}</div>${bar}`;
};

// ---- Se reemplazan las dos funciones que usa la ruta #asignacion: asig() ya trae las 3 pestañas y asigM() queda vacía ----
const ASM0 = asigM;
asigM = () => '';
asig = () => { const t = AS.tab;
  return `<div class=sel style="margin-bottom:12px">${[['ops', 'Operarios'], ['mods', 'Módulos'], ['tras', 'Traslados']].map(([k, n]) => `<button class="${t === k ? 'pr' : ''}" data-a=ast data-v=${k}>${n}</button>`).join('')}</div>`
    + (t === 'mods' ? ASM0() : t === 'tras' ? tras() : asOps()); };

// ---- Acciones ----
// render() no repinta mientras un campo tiene el foco (para no borrar lo que se escribe): se suelta el foco antes.
const rr = () => { const a = document.activeElement; if (a && a !== document.body && a.blur) a.blur(); render(); };
Object.assign(act, {
  ast(b) { AS.tab = b.dataset.v; rr(); },
  asf(b) { AS.f = b.dataset.v; rr(); },
  asg() { AS.g = !AS.g; rr(); },
  asopen(b) { const id = b.dataset.v; AS.open = AS.open === id ? null : id; rr(); },
  assel(b) { const id = b.dataset.v; AS.sel.has(id) ? AS.sel.delete(id) : AS.sel.add(id); rr(); },
  assa() { const all = AS.vis.length > 0 && AS.vis.every(id => AS.sel.has(id)); AS.vis.forEach(id => all ? AS.sel.delete(id) : AS.sel.add(id)); rr(); },
  ascl() { AS.sel.clear(); rr(); },
  asgs(b) { const g = b.dataset.v, L = S.ops.filter(o => sig(o) === g); AS.sel = new Set(L.map(o => o.id));
    if (g !== '|||' && L[0]) AS.bk = { di: L[0].di || '', df: L[0].df || '', ai: L[0].ai || '', af: L[0].af || '' }; rr(); },
  async asap() {
    document.querySelectorAll('.asb [data-c=as]').forEach(i => AS.bk[i.dataset.k] = i.value);
    const b = AS.bk, d = {};
    for (const [a, c, n] of [['di', 'df', 'desayuno'], ['ai', 'af', 'almuerzo']]) {
      if (!b[a] && !b[c]) continue;
      if (!b[a] || !b[c] || hm(b[c]) <= hm(b[a])) return alert(`Revisa el ${n}: indica «desde» y «hasta», y que «hasta» sea mayor.`);
      d[a] = b[a]; d[c] = b[c];
    }
    if (!Object.keys(d).length) return alert('Elige al menos el desayuno o el almuerzo.');
    const ids = [...AS.sel];
    try { await Promise.all(ids.map(id => db.doc('ops/' + id).update(d))); toast(`✔ Pausas aplicadas a ${ids.length} persona${ids.length === 1 ? '' : 's'}`, 'ok'); AS.sel = new Set(); AS.bk = { di: '', df: '', ai: '', af: '' }; }   // barra en blanco para la siguiente tanda
    catch (e) { toast(emsg(e)); }
    rr();
  },
  async asqt() {
    const ids = [...AS.sel]; if (!ids.length || !confirm(`¿Quitar el desayuno y el almuerzo a ${ids.length} persona${ids.length === 1 ? '' : 's'}?`)) return;
    try { await Promise.all(ids.map(id => db.doc('ops/' + id).update({ di: null, df: null, ai: null, af: null }))); AS.sel = new Set(); toast('✔ Pausas quitadas', 'ok'); }
    catch (e) { toast(emsg(e)); }
    rr();
  },
});

// Horas de la barra de pausas: se guardan al cambiar y se recalcula el aviso sin repintar
A.addEventListener('change', e => { const t = e.target; if (t.dataset && t.dataset.c === 'as') { AS.bk[t.dataset.k] = t.value; const h = $('#ashint'); if (h) h.innerHTML = asHint(); } });
})();
