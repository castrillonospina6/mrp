// Servidor: Express + SQLite (node:sqlite, sin compilar nada) + SSE para tiempo real + usuarios con roles.
const express = require('express'), { DatabaseSync } = require('node:sqlite');
const crypto = require('crypto'), path = require('path'), fs = require('fs');
const PORT = process.env.PORT || 3000;
const DBF = process.env.DB_FILE || path.join(__dirname, 'data', 'sammu.db');

// Esquema: una tabla por entidad. Agregar una app nueva = agregar tablas aquí.
const SCHEMA = {
  mods:  { n: 'TEXT', ref: 'TEXT', lote: 'TEXT', meta: 'INTEGER', ini: 'INTEGER', fin: 'INTEGER' },   // ref/lote/meta/ini/fin = orden que le asigna el supervisor al módulo (igual que en ops)
  ops:   { n: 'TEXT', mq: 'TEXT', m: 'TEXT', ref: 'TEXT', lote: 'TEXT', meta: 'INTEGER', ini: 'INTEGER', fin: 'INTEGER', hi: 'TEXT', hs: 'TEXT', di: 'TEXT', df: 'TEXT', ai: 'TEXT', af: 'TEXT', efo: 'INTEGER' },   // hi/hs ingreso/salida, di/df desayuno, ai/af almuerzo, efo eficiencia objetivo %   // ref/lote = tarea que le asigna el supervisor
  refs:  { n: 'TEXT', s: 'REAL' },
  logs:  { op: 'TEXT', mod: 'TEXT', ref: 'TEXT', lote: 'TEXT', k: 'TEXT', q: 'INTEGER', ts: 'INTEGER', sam: 'REAL' },
  lotes: { n: 'TEXT', col: 'TEXT', t: 'TEXT', q: 'INTEGER', m: 'TEXT' },
  cola:  { op: 'TEXT', mod: 'TEXT', ref: 'TEXT', lote: 'TEXT', meta: 'INTEGER', ord: 'INTEGER' },   // cola de trabajo: tareas que siguen a la actual
  stops: { m: 'TEXT', op: 'TEXT', c: 'TEXT', s: 'INTEGER', e: 'INTEGER' },
  msgs:  { to: 'TEXT', txt: 'TEXT', ts: 'INTEGER' },
  // historial de tareas terminadas (una fila por tarea cumplida al 100%): op/opn operario, rn/ln nombres al momento de cerrar, pm min de paro, efi eficiencia de la tarea
  tareas: { op: 'TEXT', opn: 'TEXT', m: 'TEXT', ref: 'TEXT', rn: 'TEXT', lote: 'TEXT', ln: 'TEXT', meta: 'INTEGER', q: 'INTEGER', x: 'INTEGER', w: 'INTEGER', ini: 'INTEGER', fin: 'INTEGER', pm: 'REAL', efi: 'REAL', sam: 'REAL' },
  ubic:  { n: 'TEXT', f: 'INTEGER', m: 'TEXT' },                      // ubicaciones del patinador: f = distancia 1 cerca · 2 media · 3 lejana; m = módulo del que es "el frente"
  pats:  { n: 'TEXT', eq: 'TEXT' },                                   // patinadores
  // traslados: pat patinador (nulo = abierto), o/d origen/destino, mod módulo que pide, st p/a/c/e, src s=supervisor m=módulo, ts creado, ta aceptado, tg cargado, te entregado, inc incidencia
  movs:  { pat: 'TEXT', o: 'TEXT', d: 'TEXT', mod: 'TEXT', lote: 'TEXT', est: 'INTEGER', nota: 'TEXT', pri: 'INTEGER', req: 'INTEGER', src: 'TEXT', st: 'TEXT', ts: 'INTEGER', ta: 'INTEGER', tg: 'INTEGER', te: 'INTEGER', inc: 'TEXT' },
};
const JSONCOLS = { msgs: ['to'] };

fs.mkdirSync(path.dirname(DBF), { recursive: true });
const db = new DatabaseSync(DBF);
db.exec('PRAGMA journal_mode=WAL');
for (const [t, cols] of Object.entries(SCHEMA))
  db.exec(`CREATE TABLE IF NOT EXISTS ${t}(id TEXT PRIMARY KEY, ${Object.entries(cols).map(([k, v]) => `"${k}" ${v}`).join(', ')})`);
for (const [t, cols] of Object.entries(SCHEMA)) {            // migración: columnas nuevas en bases antiguas
  const have = db.prepare(`PRAGMA table_info(${t})`).all().map(c => c.name);
  for (const [k, v] of Object.entries(cols)) if (!have.includes(k)) db.exec(`ALTER TABLE ${t} ADD COLUMN "${k}" ${v}`);
}
db.exec('CREATE INDEX IF NOT EXISTS i_logs_ts ON logs(ts)');
db.exec('CREATE INDEX IF NOT EXISTS i_tareas_fin ON tareas(fin)');
db.exec('CREATE INDEX IF NOT EXISTS i_movs_ts ON movs(ts)');
// Usuarios y sesiones viven FUERA de SCHEMA: la API genérica /api nunca los expone.
db.exec('CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, u TEXT UNIQUE COLLATE NOCASE, h TEXT, rol TEXT, op TEXT, "mod" TEXT, act INTEGER DEFAULT 1)');
db.exec('CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, uid TEXT, exp INTEGER)');
setInterval(() => db.prepare('DELETE FROM sessions WHERE exp<?').run(Date.now()), 36e5);

const clean = (t, b = {}) => {          // solo columnas del esquema; JSON como texto
  const o = {};
  for (const k of Object.keys(SCHEMA[t])) if (b[k] !== undefined)
    o[k] = (JSONCOLS[t] || []).includes(k) ? JSON.stringify(b[k]) : b[k];
  return o;
};
const out = t => r => { const o = { ...r }; for (const k of JSONCOLS[t] || []) o[k] = JSON.parse(o[k] || '[]'); return o; };

// ---- Autenticación: contraseñas con scrypt, sesión en cookie HttpOnly guardada en SQLite ----
const ROLES = ['admin', 'operario', 'modulo', 'patinador'], DAYS = 30 * 864e5;
const hash = p => { const s = crypto.randomBytes(16); return s.toString('hex') + ':' + crypto.scryptSync(p, s, 64).toString('hex'); };
const check = (p, h) => { const [s, k] = h.split(':'); return crypto.timingSafeEqual(crypto.scryptSync(p, Buffer.from(s, 'hex'), 64), Buffer.from(k, 'hex')); };
const DUMMY = hash('x'), fails = new Map();                       // DUMMY: mismo costo de tiempo si el usuario no existe
const cookies = q => Object.fromEntries((q.get('cookie') || '').split(';').map(c => c.trim().split('=')).filter(c => c[0]));
const pub = r => ({ id: r.id, u: r.u, rol: r.rol, op: r.op, mod: r.mod });
const session = (q, s, r) => {
  const id = crypto.randomBytes(32).toString('hex');
  db.prepare('INSERT INTO sessions VALUES(?,?,?)').run(id, r.id, Date.now() + DAYS);
  s.set('Set-Cookie', `sid=${id}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${DAYS / 1e3}${q.secure || q.get('x-forwarded-proto') === 'https' ? '; Secure' : ''}`);
};
const auth = (q, s, n) => {
  const id = cookies(q).sid;
  q.user = id && db.prepare('SELECT u.* FROM sessions s JOIN users u ON u.id=s.uid WHERE s.id=? AND s.exp>? AND u.act=1').get(id, Date.now());
  q.user ? n() : s.status(401).json({ e: 'login' });
};
const only = (...r) => (q, s, n) => r.includes(q.user.rol) ? n() : s.sendStatus(403);
const valid = b => ROLES.includes(b.rol) && (b.rol !== 'operario' || b.op) && (b.rol !== 'modulo' || b.mod) && (b.rol !== 'patinador' || b.op);   // patinador: guarda su patinador en la columna op
const othersAdmin = id => db.prepare("SELECT COUNT(*) c FROM users WHERE rol='admin' AND act=1 AND id<>?").get(id).c > 0;

const app = express(), clients = new Set();
app.use(express.json({ limit: '100kb' }));
const known = (q, s, n) => SCHEMA[q.params.t] ? n() : s.sendStatus(404);
const notify = c => clients.forEach(r => r.write(`data: ${JSON.stringify({ c })}\n\n`));

app.get('/health', (q, s) => s.json({ ok: true }));              // para Docker y monitoreo
app.get('/auth/state', (q, s) => s.json({ setup: !db.prepare('SELECT 1 FROM users LIMIT 1').get() }));
app.post('/auth/setup', (q, s) => {                              // primer administrador: solo si no hay usuarios
  if (db.prepare('SELECT 1 FROM users LIMIT 1').get()) return s.sendStatus(403);
  const b = q.body || {}, u = String(b.u || '').trim(), p = String(b.p || '');
  if (!u || p.length < 6) return s.status(400).json({ e: 'Usuario y contraseña (mínimo 6 caracteres).' });
  const id = crypto.randomUUID();
  db.prepare("INSERT INTO users(id,u,h,rol,act) VALUES(?,?,?,'admin',1)").run(id, u, hash(p));
  const r = db.prepare('SELECT * FROM users WHERE id=?').get(id); session(q, s, r); s.json(pub(r));
});
app.post('/auth/login', (q, s) => {
  const u = String(q.body?.u || ''), p = String(q.body?.p || ''), k = q.ip + '|' + u.toLowerCase(), f = fails.get(k);
  if (f && f.n >= 5 && Date.now() - f.t < 6e4) return s.status(429).json({ e: 'Demasiados intentos. Espera un minuto.' });
  const r = db.prepare('SELECT * FROM users WHERE u=? AND act=1').get(u), ok = r ? check(p, r.h) : (check(p, DUMMY), false);
  if (!ok) { fails.set(k, { n: (f?.n || 0) + 1, t: Date.now() }); return s.status(401).json({ e: 'Usuario o contraseña incorrectos.' }); }
  fails.delete(k); session(q, s, r); s.json(pub(r));
});
app.get('/auth/me', auth, (q, s) => s.json(pub(q.user)));
app.post('/auth/password', auth, (q, s) => {                      // cada usuario cambia su propia contraseña
  const p = String(q.body?.p || ''), n = String(q.body?.n || ''), k = 'pw|' + q.user.id, f = fails.get(k);
  if (n.length < 6) return s.status(400).json({ e: 'La contraseña nueva debe tener mínimo 6 caracteres.' });
  if (f && f.n >= 5 && Date.now() - f.t < 6e4) return s.status(429).json({ e: 'Demasiados intentos. Espera un minuto.' });
  if (!check(p, q.user.h)) { fails.set(k, { n: (f?.n || 0) + 1, t: Date.now() }); return s.status(403).json({ e: 'La contraseña actual no es correcta.' }); }
  fails.delete(k);
  db.prepare('UPDATE users SET h=? WHERE id=?').run(hash(n), q.user.id);
  db.prepare('DELETE FROM sessions WHERE uid=? AND id<>?').run(q.user.id, cookies(q).sid || '');   // cierra sus otras sesiones
  s.sendStatus(204);
});
app.post('/auth/logout', (q, s) => {
  const id = cookies(q).sid; if (id) db.prepare('DELETE FROM sessions WHERE id=?').run(id);
  s.set('Set-Cookie', 'sid=; HttpOnly; Path=/; Max-Age=0'); s.sendStatus(204);
});

// ---- Administración de usuarios (solo rol admin) ----
app.get('/admin/users', auth, only('admin'), (q, s) => s.json(db.prepare('SELECT id,u,rol,op,"mod",act FROM users ORDER BY u').all()));
app.post('/admin/users', auth, only('admin'), (q, s) => {
  const b = q.body || {}, u = String(b.u || '').trim(), p = String(b.p || '');
  if (!u || p.length < 6 || !valid(b)) return s.status(400).json({ e: 'Revisa usuario, contraseña (mínimo 6) y el puesto asignado.' });
  try {
    const id = crypto.randomUUID();
    db.prepare('INSERT INTO users(id,u,h,rol,op,"mod",act) VALUES(?,?,?,?,?,?,1)').run(id, u, hash(p), b.rol, ['operario', 'patinador'].includes(b.rol) ? b.op : null, b.rol === 'modulo' ? b.mod : null);
    s.json({ id });
  } catch (e) { s.status(409).json({ e: 'Ese usuario ya existe.' }); }
});
app.patch('/admin/users/:id', auth, only('admin'), (q, s) => {
  const r = db.prepare('SELECT * FROM users WHERE id=?').get(q.params.id); if (!r) return s.sendStatus(404);
  const b = q.body || {}, n = { ...r };
  for (const k of ['rol', 'op', 'mod']) if (b[k] !== undefined) n[k] = b[k] || null;
  if (b.act !== undefined) n.act = b.act ? 1 : 0;
  if (b.p !== undefined) { if (String(b.p).length < 6) return s.status(400).json({ e: 'La contraseña debe tener mínimo 6 caracteres.' }); n.h = hash(String(b.p)); }
  if (!valid(n)) return s.status(400).json({ e: 'Falta el puesto asignado para ese rol.' });
  if (r.rol === 'admin' && r.act && (n.rol !== 'admin' || !n.act) && !othersAdmin(r.id)) return s.status(409).json({ e: 'Debe quedar al menos un administrador activo.' });
  db.prepare('UPDATE users SET rol=?,op=?,"mod"=?,act=?,h=? WHERE id=?').run(n.rol, ['operario', 'patinador'].includes(n.rol) ? n.op : null, n.rol === 'modulo' ? n.mod : null, n.act, n.h, r.id);
  db.prepare('DELETE FROM sessions WHERE uid=? AND id<>?').run(r.id, cookies(q).sid || '');   // cierra sus otras sesiones
  s.sendStatus(204);
});
app.delete('/admin/users/:id', auth, only('admin'), (q, s) => {
  const r = db.prepare('SELECT * FROM users WHERE id=?').get(q.params.id); if (!r) return s.sendStatus(404);
  if (r.rol === 'admin' && r.act && !othersAdmin(r.id)) return s.status(409).json({ e: 'Debe quedar al menos un administrador activo.' });
  db.prepare('DELETE FROM sessions WHERE uid=?').run(r.id); db.prepare('DELETE FROM users WHERE id=?').run(r.id); s.sendStatus(204);
});

// ---- Históricos (solo admin): totales por día y operario, y borrado de registros ----
app.get('/admin/hist', auth, only('admin'), (q, s) => {
  const f = +q.query.from, t = +q.query.to, z = +q.query.tz || 0;      // z: desfase horario del navegador (minutos)
  if (!(t > f)) return s.sendStatus(400);
  const days = db.prepare(`SELECT strftime('%Y-%m-%d',(ts-?*60000)/1000,'unixepoch') d, op, "mod" m,
    SUM(CASE WHEN k='p' THEN COALESCE(q,0) ELSE 0 END) p, SUM(CASE WHEN k='x' THEN COALESCE(q,0) ELSE 0 END) x, SUM(CASE WHEN k='w' THEN COALESCE(q,0) ELSE 0 END) w,
    SUM(CASE WHEN k='p' THEN COALESCE(q,0)*COALESCE(sam,0) ELSE 0 END) sam, MIN(ts) f, MAX(ts) l
    FROM logs WHERE ts>=? AND ts<? GROUP BY d, op, "mod"`).all(z, f, t);
  const stops = db.prepare('SELECT m,op,c,s,e FROM stops WHERE s<? AND (e IS NULL OR e>=?)').all(t, f);
  const tasks = db.prepare('SELECT * FROM tareas WHERE fin>=? AND fin<? ORDER BY fin DESC').all(f, t);
  s.json({ days, stops, tasks });
});
app.post('/admin/purge', auth, only('admin'), (q, s) => {
  const b = q.body || {};
  if (b.reset === true) {                                          // reinicio de pruebas: conserva configuración y usuarios
    for (const tb of ['logs', 'stops', 'cola', 'msgs', 'tareas', 'movs']) db.exec(`DELETE FROM ${tb}`);
    db.exec('UPDATE ops SET ref=NULL,lote=NULL,meta=NULL,ini=NULL,fin=NULL');
    db.exec('UPDATE mods SET ref=NULL,lote=NULL,meta=NULL,ini=NULL,fin=NULL');
    for (const c of ['logs', 'stops', 'cola', 'msgs', 'ops', 'mods', 'tareas', 'movs']) notify(c);
    return s.sendStatus(204);
  }
  const f = +b.from, t = +b.to, o = b.op ? String(b.op) : null, w = o ? ' AND op=?' : '', a = o ? [o] : [];
  if (!(t > f)) return s.sendStatus(400);
  const l = db.prepare(`DELETE FROM logs WHERE ts>=? AND ts<?${w}`).run(f, t, ...a);
  const p = db.prepare(`DELETE FROM stops WHERE s>=? AND s<?${w}`).run(f, t, ...a);
  const k = db.prepare(`DELETE FROM tareas WHERE fin>=? AND fin<?${w}`).run(f, t, ...a);
  notify('logs'); notify('stops'); notify('tareas'); s.json({ logs: l.changes, stops: p.changes, tareas: k.changes });
});

app.get('/events', auth, (q, s) => {                              // tiempo real (Server-Sent Events)
  s.set({ 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
  s.write('retry: 2000\n\n'); clients.add(s); q.on('close', () => clients.delete(s));
});
setInterval(() => clients.forEach(r => r.write(': ping\n\n')), 25000);

// ---- API de datos: lectura para todo usuario con sesión; escritura según rol ----
// admin: todo. operario/modulo: solo crear marcaciones (logs) y paros (stops) y cerrar paros, siempre en SU puesto.
const mineMod = q => (q.user.rol === 'modulo' ? q.user.mod : db.prepare('SELECT m FROM ops WHERE id=?').get(q.user.op)?.m) || null;
const perm = (q, s, n) => q.user.rol === 'admin' || q.method === 'GET' ||
  (q.params.t === 'logs' && q.method === 'POST' && q.user.rol !== 'patinador') || (q.params.t === 'stops' && ['POST', 'PATCH'].includes(q.method)) ? n() : s.sendStatus(403);
const bind = q => {                                              // el servidor fija identidad, tarea, hora y SAM: el cliente no manda
  const b = { ...q.body }, m = mineMod(q), a = q.user.rol === 'operario' ? (db.prepare('SELECT ref,lote FROM ops WHERE id=?').get(q.user.op) || {}) : null,
    md = q.user.rol === 'modulo' ? (db.prepare('SELECT ref,lote FROM mods WHERE id=?').get(q.user.mod) || {}) : null;
  if (q.params.t === 'logs') {
    if (a) { if (!a.ref) return null; b.ref = a.ref; b.lote = a.lote; }   // el trabajador solo marca la tarea que le asignaron
    else if (md) { b.ref = md.ref; b.lote = md.lote; }                    // el módulo solo marca la orden que le asignaron (si no tiene, el control del POST lo rechaza)
    Object.assign(b, { mod: m, op: a ? q.user.op : null, q: 1, ts: Date.now(), k: ['p', 'x', 'w'].includes(b.k) ? b.k : 'p', sam: db.prepare('SELECT s FROM refs WHERE id=?').get(b.ref)?.s ?? 0 });
  } else {                                                       // paro: el módulo puede registrarlo a nombre de UN operario de su módulo
    const po = a || q.user.rol === 'patinador' ? q.user.op : (q.user.rol === 'modulo' && b.op && db.prepare('SELECT 1 FROM ops WHERE id=? AND m=?').get(b.op, m) ? b.op : null);
    Object.assign(b, { m, op: po, s: Date.now(), e: null });
  }
  return b;
};

// Meta cumplida: la tarea se finaliza sola (fin), se cierran sus paros y el operario queda disponible para otra asignación.
const LOCK = new Map(), LOCKMS = 4500, MSGM = 'El módulo ya completó el 100% de esta orden. No se pueden marcar más unidades.';   // tras cerrar una tarea el operario no puede marcar unos segundos (su pantalla muestra el cierre y se recarga)
const done = op => {
  const o = db.prepare('SELECT n,m,ref,lote,meta,ini,fin FROM ops WHERE id=?').get(op); if (!o || o.fin || !(o.meta > 0)) return;
  const g = db.prepare("SELECT COALESCE(SUM(CASE WHEN k='p' THEN q END),0) n, COALESCE(SUM(CASE WHEN k='x' THEN q END),0) x, COALESCE(SUM(CASE WHEN k='w' THEN q END),0) w, COALESCE(SUM(CASE WHEN k='p' THEN COALESCE(q,0)*COALESCE(sam,0) END),0) sm, MIN(ts) t0 FROM logs WHERE op=? AND ref=? AND COALESCE(lote,'')=COALESCE(?,'') AND ts>=COALESCE(?,0)").get(op, o.ref, o.lote, o.ini);
  if (g.n < o.meta) return;
  const f = Date.now(), nx = db.prepare('SELECT * FROM cola WHERE op=? ORDER BY ord LIMIT 1').get(op);
  db.prepare('UPDATE stops SET e=? WHERE op=? AND e IS NULL').run(f, op);
  // Registro en el historial: se guarda la tarea terminada (cantidades, tiempos, paros y eficiencia)
  const ini = o.ini || g.t0 || f, r = db.prepare('SELECT n,s FROM refs WHERE id=?').get(o.ref), z = o.lote ? db.prepare('SELECT n,col,t FROM lotes WHERE id=?').get(o.lote) : null,
    pm = db.prepare('SELECT COALESCE(SUM((MIN(COALESCE(e,?),?)-MAX(s,?))/60000.0),0) v FROM stops WHERE op=? AND s<? AND COALESCE(e,?)>?').get(f, f, ini, op, f, f, ini).v, av = (f - ini) / 6e4 - pm,
    rec = { op, opn: o.n, m: o.m, ref: o.ref, rn: r?.n || null, lote: o.lote, ln: z ? `${z.n} · ${z.col} · ${z.t}` : null, meta: o.meta, q: g.n, x: g.x, w: g.w, ini, fin: f, pm, efi: av >= 1 ? g.sm / av * 100 : null, sam: r?.s ?? null }, ks = Object.keys(rec);
  db.prepare(`INSERT INTO tareas(id${ks.map(k => `,"${k}"`).join('')}) VALUES(?${ks.map(() => ',?').join('')})`).run(crypto.randomUUID(), ...ks.map(k => rec[k]));
  LOCK.set(op, f + LOCKMS);
  if (nx) {                                                      // hay cola: la siguiente tarea arranca sola
    db.prepare('UPDATE ops SET ref=?,lote=?,meta=?,ini=?,fin=NULL WHERE id=?').run(nx.ref, nx.lote, nx.meta, f, op);
    db.prepare('DELETE FROM cola WHERE id=?').run(nx.id); notify('cola');
  } else db.prepare('UPDATE ops SET fin=? WHERE id=?').run(f, op);   // sin cola: queda disponible
  notify('ops'); notify('stops'); notify('tareas');
};

// Orden del módulo cumplida: misma lógica que done(), pero cuenta solo las marcaciones del módulo (op nulo) y la eficiencia se calcula sobre todos sus operarios.
const doneMod = id => {
  const o = db.prepare('SELECT n,ref,lote,meta,ini,fin FROM mods WHERE id=?').get(id); if (!o || o.fin || !(o.meta > 0)) return;
  const g = db.prepare("SELECT COALESCE(SUM(CASE WHEN k='p' THEN q END),0) n, COALESCE(SUM(CASE WHEN k='x' THEN q END),0) x, COALESCE(SUM(CASE WHEN k='w' THEN q END),0) w, COALESCE(SUM(CASE WHEN k='p' THEN COALESCE(q,0)*COALESCE(sam,0) END),0) sm, MIN(ts) t0 FROM logs WHERE op IS NULL AND \"mod\"=? AND ref=? AND COALESCE(lote,'')=COALESCE(?,'') AND ts>=COALESCE(?,0)").get(id, o.ref, o.lote, o.ini);
  if (g.n < o.meta) return;
  const f = Date.now(), nx = db.prepare('SELECT * FROM cola WHERE "mod"=? ORDER BY ord LIMIT 1').get(id);
  db.prepare('UPDATE stops SET e=? WHERE m=? AND op IS NULL AND e IS NULL').run(f, id);          // cierra los paros de todo el módulo
  const ini = o.ini || g.t0 || f, r = db.prepare('SELECT n,s FROM refs WHERE id=?').get(o.ref), z = o.lote ? db.prepare('SELECT n,col,t FROM lotes WHERE id=?').get(o.lote) : null,
    no = Math.max(1, db.prepare('SELECT COUNT(*) c FROM ops WHERE m=?').get(id).c),
    pm = db.prepare('SELECT COALESCE(SUM((MIN(COALESCE(e,?),?)-MAX(s,?))/60000.0*CASE WHEN op IS NULL THEN ? ELSE 1 END),0) v FROM stops WHERE m=? AND s<? AND COALESCE(e,?)>?').get(f, f, ini, no, id, f, f, ini).v, av = no * (f - ini) / 6e4 - pm,
    rec = { op: null, opn: o.n + ' (módulo)', m: id, ref: o.ref, rn: r?.n || null, lote: o.lote, ln: z ? `${z.n} · ${z.col} · ${z.t}` : null, meta: o.meta, q: g.n, x: g.x, w: g.w, ini, fin: f, pm, efi: av >= 1 ? g.sm / av * 100 : null, sam: r?.s ?? null }, ks = Object.keys(rec);
  db.prepare(`INSERT INTO tareas(id${ks.map(k => `,"${k}"`).join('')}) VALUES(?${ks.map(() => ',?').join('')})`).run(crypto.randomUUID(), ...ks.map(k => rec[k]));
  LOCK.set(id, f + LOCKMS);
  if (nx) {                                                      // hay cola: la siguiente orden arranca sola
    db.prepare('UPDATE mods SET ref=?,lote=?,meta=?,ini=?,fin=NULL WHERE id=?').run(nx.ref, nx.lote, nx.meta, f, id);
    db.prepare('DELETE FROM cola WHERE id=?').run(nx.id); notify('cola');
  } else db.prepare('UPDATE mods SET fin=? WHERE id=?').run(f, id);   // sin cola: queda disponible
  notify('mods'); notify('stops'); notify('tareas');
};

// ---- Pausas (desayuno/almuerzo): el trabajo se retoma escaneando el QR del módulo ----
const BRK = ['Desayuno', 'Almuerzo'];
let QRL; try { QRL = require('qrcode'); } catch (e) { console.log('Falta la dependencia "qrcode": ejecuta npm install'); }
db.exec('CREATE TABLE IF NOT EXISTS qrs("mod" TEXT PRIMARY KEY, code TEXT)');      // los códigos nunca salen por la API genérica
const qrFor = m => {
  let r = db.prepare('SELECT code FROM qrs WHERE "mod"=?').get(m);
  if (!r) { r = { code: crypto.randomBytes(6).toString('hex').toUpperCase() }; db.prepare('INSERT INTO qrs("mod",code) VALUES(?,?)').run(m, r.code); }
  return r.code;
};
app.get('/admin/qr/:m', auth, only('admin'), async (q, s) => {
  if (!db.prepare('SELECT 1 FROM mods WHERE id=?').get(q.params.m)) return s.sendStatus(404);
  const code = qrFor(q.params.m);
  s.json({ code, svg: QRL ? await QRL.toString(code, { type: 'svg', margin: 2 }) : '' });
});
app.post('/admin/qr/:m/new', auth, only('admin'), (q, s) => { db.prepare('DELETE FROM qrs WHERE "mod"=?').run(q.params.m); qrFor(q.params.m); s.sendStatus(204); });
app.post('/api/resume', auth, only('operario'), (q, s) => {        // el operario envía el código escaneado
  const k = 'qr|' + q.user.id, f = fails.get(k);
  if (f && f.n >= 5 && Date.now() - f.t < 6e4) return s.status(429).json({ e: 'Demasiados intentos. Espera un minuto.' });
  const st = db.prepare("SELECT id FROM stops WHERE op=? AND e IS NULL AND c IN ('Desayuno','Almuerzo')").get(q.user.op);
  if (!st) return s.sendStatus(204);
  const m = mineMod(q);
  if (!m || String(q.body?.code || '').trim().toUpperCase() !== qrFor(m)) { fails.set(k, { n: (f?.n || 0) + 1, t: Date.now() }); return s.status(403).json({ e: 'Código incorrecto.' }); }
  fails.delete(k); db.prepare('UPDATE stops SET e=? WHERE id=?').run(Date.now(), st.id); notify('stops'); s.sendStatus(204);
});

// ---- Traslados del patinador: el servidor fija la hora de cada paso (pendiente → aceptada → cargada → entregada) ----
const NEXT = { p: ['a', 'ta'], a: ['c', 'tg'], c: ['e', 'te'] }, MINC = ['Estiba dañada', 'Ruta bloqueada', 'No encontré la estiba'];
app.post('/api/mov/:a', auth, only('admin', 'patinador', 'modulo'), (q, s) => {
  const b = q.body || {}, u = q.user, A = q.params.a;
  if (A === 'pedir') {                                             // el módulo pide material: nace pendiente y abierto (cualquier patinador lo puede tomar)
    if (u.rol !== 'modulo') return s.sendStatus(403);
    const dst = db.prepare('SELECT id FROM ubic WHERE m=?').get(u.mod), org = b.o && db.prepare('SELECT id FROM ubic WHERE id=?').get(b.o), id = crypto.randomUUID();
    db.prepare('INSERT INTO movs(id,o,d,"mod",est,nota,pri,st,src,ts) VALUES(?,?,?,?,?,?,?,?,?,?)')
      .run(id, org ? org.id : null, dst ? dst.id : null, u.mod, Math.max(1, Math.min(99, +b.est || 1)), String(b.nota || '').slice(0, 200), +b.pri ? 1 : 0, 'p', 'm', Date.now());
    notify('movs'); return s.json({ id });
  }
  if (u.rol === 'modulo') return s.sendStatus(403);
  const pid = u.rol === 'admin' ? b.pat : u.op, r = db.prepare('SELECT * FROM movs WHERE id=?').get(b.id);      // el admin indica a nombre de qué patinador actúa
  if (!pid || !r) return s.sendStatus(404);
  if (r.pat && r.pat !== pid) return s.sendStatus(403);
  if (A === 'inc') { db.prepare('UPDATE movs SET inc=? WHERE id=?').run(MINC.includes(b.c) ? b.c : null, r.id); notify('movs'); return s.sendStatus(204); }
  if (A !== 'avanzar') return s.sendStatus(404);
  if (db.prepare('SELECT 1 FROM stops WHERE op=? AND e IS NULL').get(pid)) return s.status(409).json({ e: 'Hay un paro abierto: reanuda para continuar.' });
  const n = NEXT[r.st]; if (!n) return s.status(409).json({ e: 'Ese traslado ya fue entregado.' });
  if (n[0] !== 'e' && db.prepare("SELECT 1 FROM movs WHERE pat=? AND id<>? AND st IN ('a','c')").get(pid, r.id)) return s.status(409).json({ e: 'Termina primero el traslado que tienes en curso.' });
  db.prepare(`UPDATE movs SET st=?,${n[1]}=?,pat=? WHERE id=?`).run(n[0], Date.now(), pid, r.id);
  notify('movs'); s.sendStatus(204);
});

app.use('/api', auth);
app.get('/api/:t', known, (q, s) => {
  const t = q.params.t, c = t === 'tareas' ? 'fin' : 'ts', w = q.query.ts_gte && SCHEMA[t][c] ? ` WHERE ${c}>=?` : '';
  s.json(db.prepare(`SELECT * FROM ${t}${w}`).all(...(w ? [+q.query.ts_gte] : [])).map(out(t)));
});
app.post('/api/:t', known, perm, (q, s) => {
  const t = q.params.t, id = crypto.randomUUID(), OP = q.user.rol === 'operario' ? db.prepare('SELECT ref,fin FROM ops WHERE id=?').get(q.user.op) : null;
  if (OP && t === 'logs' && (LOCK.get(q.user.op) || 0) > Date.now()) return s.status(409).json({ e: 'Ya completaste el 100% de esta tarea. No puedes marcar más unidades.', done: true });   // recién cerrada: nada se marca hasta pasar a la siguiente
  if (OP && t === 'logs' && (!OP.ref || OP.fin)) return s.status(409).json({ e: OP.fin ? 'Ya completaste el 100% de esta tarea: espera tu nueva asignación.' : 'Sin tarea asignada' });
  if (t === 'stops' && q.user.rol === 'operario' && BRK.includes(q.body?.c)) {   // idempotente: un solo paro por pausa
    const d = db.prepare('SELECT id FROM stops WHERE op=? AND c=? AND s>?').get(q.user.op, q.body.c, Date.now() - 6 * 36e5);
    if (d) return s.json({ id: d.id });
  }
  const b = q.user.rol === 'admin' ? q.body : bind(q);
  if (!b) return s.status(400).json({ e: 'Sin tarea asignada' });
  if (t === 'logs' && !b.op && b.mod) {                          // marcación de módulo: necesita una orden cargada y sin terminar
    const z = db.prepare('SELECT ref,fin FROM mods WHERE id=?').get(b.mod);
    if ((LOCK.get(b.mod) || 0) > Date.now()) return s.status(409).json({ e: MSGM, done: true });   // recién cerrada: su pantalla muestra el cierre y se recarga
    if (!z || !z.ref) return s.status(409).json({ e: 'Este módulo aún no tiene una orden cargada.' });
    if (z.fin) return s.status(409).json({ e: MSGM, done: true });
  }
  if (t === 'stops' && q.user.rol === 'patinador' && db.prepare('SELECT 1 FROM stops WHERE op=? AND e IS NULL').get(q.user.op)) return s.status(409).json({ e: 'Ya tienes un paro abierto.' });
  if (t === 'stops' && q.user.rol === 'modulo' && b.op && db.prepare('SELECT 1 FROM stops WHERE op=? AND e IS NULL').get(b.op)) return s.status(409).json({ e: 'Ese operario ya tiene un paro abierto.' });
  if (t === 'logs' && q.user.rol !== 'admin' && b.k === 'p' && (q.user.rol === 'operario'      // en paro no se marcan unidades
      ? db.prepare('SELECT 1 FROM stops WHERE op=? AND e IS NULL').get(q.user.op)
      : db.prepare('SELECT 1 FROM stops WHERE m=? AND op IS NULL AND e IS NULL').get(q.user.mod)))
    return s.status(409).json({ e: 'Hay un paro abierto: reanuda para marcar unidades.' });
  const d = clean(t, b); if (t === 'movs') Object.assign(d, { ts: Date.now(), st: 'p', src: 's' });   // traslado nuevo: nace pendiente
  const ks = Object.keys(d);
  db.prepare(`INSERT INTO ${t}(id${ks.map(k => `,"${k}"`).join('')}) VALUES(?${ks.map(() => ',?').join('')})`).run(id, ...ks.map(k => d[k]));
  if (t === 'logs' && d.op && d.k === 'p') done(d.op);
  else if (t === 'logs' && !d.op && d.mod && d.k === 'p') doneMod(d.mod);
  notify(t); s.json({ id });
});
app.patch('/api/:t/:id', known, perm, (q, s) => {
  const t = q.params.t; let b = q.body;
  if (q.user.rol !== 'admin') {                                  // solo cerrar paros de su propio módulo
    const st = db.prepare('SELECT m,op,c FROM stops WHERE id=?').get(q.params.id);
    if (!st || st.m !== mineMod(q) || ((q.user.rol === 'operario' || q.user.rol === 'patinador') && st.op !== q.user.op)) return s.sendStatus(403);
    if (BRK.includes(st.c)) return s.sendStatus(403);        // desayuno/almuerzo: solo se cierra con el QR (o lo fuerza el admin)
    b = { e: Date.now() };
  }
  const d = clean(t, b);
  if ((t === 'ops' || t === 'mods') && (b.ref !== undefined || b.lote !== undefined)) { d.ini = Date.now(); d.fin = null; }   // tarea nueva: reinicia el avance
  const ks = Object.keys(d);
  if (ks.length) db.prepare(`UPDATE ${t} SET ${ks.map(k => `"${k}"=?`).join(',')} WHERE id=?`).run(...ks.map(k => d[k]), q.params.id);
  notify(t); s.sendStatus(204);
});
app.delete('/api/:t/:id', known, only('admin'), (q, s) => { db.prepare(`DELETE FROM ${q.params.t} WHERE id=?`).run(q.params.id); notify(q.params.t); s.sendStatus(204); });

app.use(express.static(path.join(__dirname, 'public')));
app.listen(PORT, () => console.log(`sammu-os en http://localhost:${PORT}  (base: ${DBF})`));
