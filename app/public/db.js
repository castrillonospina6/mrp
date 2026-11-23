// Cliente de datos: misma API que usa la interfaz (collection/doc/where/onSnapshot), contra el servidor propio.
// La sesión viaja en una cookie HttpOnly; si el servidor responde 401 se avisa a la interfaz (evento "sammu:auth").
window.sammuDB = (() => {
  const subs = new Set(), lost = () => window.dispatchEvent(new Event('sammu:auth'));
  const api = async (m, u, b) => {
    const r = await fetch('/api/' + u, { method: m, headers: { 'content-type': 'application/json' }, body: b ? JSON.stringify(b) : undefined });
    if (r.status === 401) { lost(); throw new Error('Sesión vencida'); }
    if (!r.ok) throw new Error(await r.text());
    return r.status === 204 ? null : r.json();
  };
  const load = s => api('GET', s.c + (s.ts ? '?ts_gte=' + s.ts : '')).then(rows => s.next({ docs: rows.map(({ id, ...d }) => ({ id, data: () => d })) }), e => s.err && s.err(e));
  let es, t; const pend = new Set();
  const connect = () => { es = new EventSource('/events');
    es.onopen = () => subs.forEach(load);
    es.onerror = () => { if (es.readyState === 2) lost(); };       // cerrada por el servidor (sin sesión)
    es.onmessage = e => { pend.add(JSON.parse(e.data).c); clearTimeout(t);   // acumula TODAS las colecciones avisadas en la ventana (antes solo recargaba la última)
      t = setTimeout(() => { const cs = [...pend]; pend.clear(); subs.forEach(s => cs.includes(s.c) && load(s)); }, 40); }; };
  const query = (c, ts) => ({
    where: (f, op, v) => query(c, v),                                // solo se usa where('ts','>=',x)
    onSnapshot(next, err) { const s = { c, ts, next, err }; subs.add(s); if (!es) connect(); load(s); return () => subs.delete(s); },
  });
  return {
    collection: c => ({ ...query(c), add: d => api('POST', c, d) }),
    doc: p => { const [c, id] = p.split('/'); return { update: d => api('PATCH', `${c}/${id}`, d), delete: () => api('DELETE', `${c}/${id}`) }; },
    reset() { es && es.close(); es = null; subs.clear(); },          // al cerrar sesión
  };
})();
