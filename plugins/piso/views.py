"""Vistas de control de piso.

Tablet (sin usuario del MES): el operario entra con su documento + PIN; la tablet fija de una línea se vincula con el
código de la línea. Todo lo demás (qué tarea tiene cada quién) lo deciden los supervisores desde el MES.

MES (con usuario): tablero en vivo, mensajes a la planta, histórico, traslados y códigos de tablets / QR.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import wraps

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from mes_core import site_scope
from mes_core.models import Employee, Operation, Site, WorkCenter, WorkOrder

from . import checkin, floor, overtime, reporting, transfers
from .floor import FloorError
from .live import bump, versions
from .models import (FloorAccess, IntervalReport, LineCode, Location, Mark, ProcessReporting, Stop, SupernumeraryLine,
                     Transfer, new_code, new_pause_code)

SESSION_KEY = "piso"


@dataclass
class Actor:
    role: str  # "op" operario · "mov" patinador · "sup" supernumerario · "line" tablet de línea
    employee: Employee | None = None
    work_center: WorkCenter | None = None
    access: FloorAccess | None = None

    @property
    def pulse_keys(self) -> list[str]:
        if self.role == "line":
            return ["g", f"l{self.work_center.pk}", "t"]
        keys = ["g", f"e{self.employee.pk}"]
        return keys + ["t"] if self.role in ("mov", "sup") else keys  # el supernumerario sigue sus pedidos


def session_cutoff(now=None):
    """Último cierre general de sesiones (por defecto todos los días a las 22:00, ``PISO_CIERRE_SESIONES``). Las
    terminales no son de una sola persona: al otro día cada quien entra en la que le toque. Vacío = sin cierre."""
    raw = str(getattr(settings, "PISO_CIERRE_SESIONES", "22:00") or "").strip()
    if not raw:
        return None
    try:
        hh, mm = (int(x) for x in raw.split(":")[:2])
    except ValueError:
        return None
    local = timezone.localtime(now or timezone.now())
    cut = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return cut if cut <= local else cut - timedelta(days=1)


def get_actor(request) -> Actor | None:
    s = request.session.get(SESSION_KEY) or {}
    if s.get("k") == "emp":  # cierre diario: la sesión de una persona no pasa del corte de la noche
        cut = session_cutoff()
        if s.get("t") is None:
            s["t"] = time.time()
            request.session[SESSION_KEY] = s
        elif cut is not None and s["t"] < cut.timestamp():
            request.session.pop(SESSION_KEY, None)
            request.piso_closed = True
            return None
    if s.get("k") == "line":
        lc = LineCode.objects.select_related("work_center__site").filter(work_center_id=s.get("id")).first()
        if lc and lc.pair_code == s.get("code") and lc.work_center.active:  # código nuevo = tablet desvinculada
            return Actor("line", work_center=lc.work_center)
        return None
    if s.get("k") == "emp":
        acc = (FloorAccess.objects.select_related("employee__shift__site")
               .filter(employee_id=s.get("id"), active=True, employee__active=True).first())
        if acc and acc.pin_hash[-16:] == s.get("h"):  # si el administrador cambia el PIN, la sesión se cierra
            role = {FloorAccess.Role.MOVER: "mov", FloorAccess.Role.SUPERNUMERARY: "sup"}.get(acc.role, "op")
            # un operario que el supervisor puso de patinador en una operación abierta ve la app como patinador
            # mientras dure esa operación; al terminarla vuelve a su pantalla de operario
            if role == "op" and transfers.is_assigned_mover(acc.employee_id):
                role = "mov"
            return Actor(role, employee=acc.employee, access=acc)
    return None


def _body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


CHECKIN_FREE = {"api_checkin"}  # lo único que se puede hacer antes de confirmar la llegada
OVERTIME_FREE = {"api_checkin", "api_overtime"}  # lo único antes de responder la convocatoria de horas extra


def _err(message, status=400, **extra):
    return JsonResponse({"e": message, **extra}, status=status)


def tablet_api(*roles):
    def deco(fn):
        @wraps(fn)
        def inner(request, *args, **kwargs):
            actor = get_actor(request)
            if actor is None:
                return _err("login", 401, closed=getattr(request, "piso_closed", False))
            if roles and actor.role not in roles:
                return _err("Esta tablet no puede hacer eso.", 403)
            # lo primero del día es confirmar la llegada: antes no se puede hacer nada más
            if actor.employee is not None and fn.__name__ not in CHECKIN_FREE and not checkin.confirmed(actor.employee):
                return _err(checkin.BLOCKED_MSG, 409, checkin=True)
            # convocatoria de horas extra sin responder: tampoco se habilita nada hasta que responda
            if actor.employee is not None and fn.__name__ not in OVERTIME_FREE and overtime.pending_for(actor.employee):
                return _err(overtime.BLOCKED_MSG, 409, overtime=True)
            try:
                return fn(request, actor, *args, **kwargs)
            except FloorError as exc:
                return _err(exc.message, 409, done=exc.done)
        return inner
    return deco


def _too_many(key: str, limit: int = 5) -> bool:
    return cache.get(key, 0) >= limit


def _fail(key: str) -> None:
    cache.set(key, cache.get(key, 0) + 1, 60)


# ================================================================== tablet
@never_cache
@ensure_csrf_cookie
def tablet(request):
    # La página trae ya el estado (o «entra con tu PIN»): la tablet pinta su pantalla sin esperar otra consulta.
    actor = get_actor(request)
    try:
        boot = state_data(actor) if actor else {"login": True, "closed": getattr(request, "piso_closed", False)}
    except Exception:  # noqa: BLE001 — si algo falla aquí, la tablet lo pide como siempre
        boot = None
    return render(request, "piso/tablet.html", {
        "pulse_url": reverse("piso:api_pulse"), "base": reverse("piso:tablet"), "boot": boot,
    })


def manifest(request):
    """La app de planta se puede «instalar» en el equipo (pantalla de inicio). Instalada, Chrome deja sonar las alarmas
    sin que nadie toque la pantalla y se abre a pantalla completa."""
    from django.templatetags.static import static

    base = reverse("piso:tablet")
    icons = [{"src": static(f"piso/icon-{s}{m}.png"), "sizes": f"{s}x{s}", "type": "image/png",
              "purpose": "maskable" if m else "any"} for s in (192, 512) for m in ("", "-maskable")]
    data = {"name": "Planta en vivo · Control de piso", "short_name": "Planta", "id": base, "start_url": base,
            "scope": base, "display": "standalone", "orientation": "any", "lang": "es",
            "background_color": "#0e0e12", "theme_color": "#0e0e12", "icons": icons}
    return JsonResponse(data, content_type="application/manifest+json")


SW_JS = """// Service worker de la app de planta: permite instalarla y mostrar avisos del sistema (con el tono del equipo).
const SCOPE = %(scope)s, STATIC = %(static)s, BOX = 'piso-static-v1';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
// Estilos, código, fuente e íconos: del equipo al instante (su URL lleva ?v=fecha, cambia al actualizar). Los datos
// nunca se guardan: siempre llegan frescos del MES.
async function fromBox(req) {
  const box = await caches.open(BOX), hit = await box.match(req);
  if (hit) return hit;
  const r = await fetch(req);
  if (r.ok) {
    const path = new URL(req.url).pathname;
    for (const k of await box.keys()) if (new URL(k.url).pathname === path) box.delete(k);   // versión vieja fuera
    box.put(req, r.clone());
  }
  return r;
}
self.addEventListener('fetch', e => {   // sin red, una pantalla que se reintenta sola
  const u = new URL(e.request.url);
  if (e.request.method === 'GET' && self.caches && u.origin === location.origin && u.pathname.startsWith(STATIC)) {
    e.respondWith(fromBox(e.request).catch(() => fetch(e.request)));
    return;
  }
  if (e.request.mode !== 'navigate') return;
  e.respondWith(fetch(e.request).catch(() => new Response(
    '<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><body style="font:20px sans-serif;padding:24px;background:#0e0e12;color:#fff">Sin conexión con el MES. Reintentando…<script>setTimeout(()=>location.reload(),5000)</script>',
    {headers: {'content-type': 'text/html; charset=utf-8'}})));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(ws => {
    const w = ws.find(x => new URL(x.url).pathname.startsWith(SCOPE));
    return w ? w.focus() : self.clients.openWindow(SCOPE);
  }));
});
"""


def service_worker(request):
    scope = reverse("piso:tablet")
    from django.templatetags.static import static

    resp = HttpResponse(SW_JS % {"scope": json.dumps(scope), "static": json.dumps(static("x")[:-1])},
                        content_type="text/javascript; charset=utf-8")
    resp["Service-Worker-Allowed"] = scope
    resp["Cache-Control"] = "no-cache"
    return resp


@require_GET
def api_pulse(request):
    """Barato a propósito: no toca la base de datos ni la sesión."""
    keys = [k for k in request.GET.get("k", "").split(",") if k]
    return JsonResponse({"v": versions(keys), "now": timezone.now().isoformat()})


@require_GET
def api_state(request):
    actor = get_actor(request)
    if actor is None:
        return _err("login", 401, closed=getattr(request, "piso_closed", False))
    return JsonResponse(state_data(actor, {"1": True, "0": False}.get(request.GET.get("snd"))))


def state_data(actor: Actor, snd: bool | None = None) -> dict:
    """Todo lo que pinta la tablet. Lo usan la consulta del estado, la página (va incluido para pintar al instante, sin
    esperar otra consulta) y la entrada (documento + PIN): la pantalla aparece con la misma respuesta."""
    # presencia (quién tiene la app abierta) y si su equipo deja sonar las alarmas solo; se guarda a lo sumo cada 20 s
    from mes_core.dbqueue import write_turn

    now = timezone.now()
    if actor.access is not None:
        a = actor.access
        if a.last_seen is None or now - a.last_seen > timedelta(seconds=20) or (snd is not None and snd != a.sound_ok):
            with write_turn():
                FloorAccess.objects.filter(pk=a.pk).update(last_seen=now,
                                                           **({"sound_ok": snd} if snd is not None else {}))
    elif actor.role == "line":
        lc = actor.work_center.floor_code
        if lc.tablet_seen is None or now - lc.tablet_seen > timedelta(seconds=20) or (
                snd is not None and snd != lc.tablet_sound):
            with write_turn():
                LineCode.objects.filter(pk=lc.pk).update(tablet_seen=now,
                                                         **({"tablet_sound": snd} if snd is not None else {}))
    keys, tags = actor.pulse_keys, None
    if actor.role == "sup":
        tags = reporting.sup_lines(actor.employee)
        keys = keys + [f"l{i}" for i in sorted(tags)][:20]
    # versión de los pulsos ANTES de armar los datos: la tablet la guarda y no vuelve a pedir lo que ya tiene
    # (si algo cambia mientras se arman, la versión ya no coincide y la tablet recarga: nunca se queda con datos viejos)
    v = versions(keys)
    if actor.role == "line":
        data = floor.line_state(actor.work_center)
        data["requests"] = transfers.line_requests(actor.work_center)
        data.update(transfers.request_form(actor.work_center.site, actor.work_center))
    elif actor.role == "mov":
        data = transfers.mover_state(actor.employee)
    elif actor.role == "sup":
        data = reporting.sup_state(actor.employee, tags=tags)
        data["material"] = sup_material(data)
    else:
        data = floor.operator_state(actor.employee)
    data["pulse"], data["v"] = keys, v
    if actor.employee is not None:  # pantalla de llegada: a dónde va hoy + confirmar asistencia
        data["checkin"] = checkin.state(actor.employee, now)
        data["overtime"] = overtime.tablet_state(actor.employee, now)  # convocatorias de horas extra
    return data


def sup_material(data: dict) -> dict:
    """Pestaña «Material» del supernumerario: pedidos de hoy de sus líneas y lo necesario para pedir (de dónde, a qué
    patinador, cuál va por defecto en cada línea)."""
    ids = [ln["id"] for ln in data.get("lines", [])]
    lines = list(WorkCenter.objects.filter(pk__in=ids).select_related("site").order_by("code"))
    site = lines[0].site if lines else None
    form = transfers.request_form(site) if site else {"locations": [], "movers": [], "default_mover": None}
    return {
        "lines": [{"id": wc.pk, "code": wc.code, "name": wc.name,
                   "default_mover": (lambda m: m.pk if m else None)(transfers.default_mover(wc)),
                   "requests": transfers.line_requests(wc, limit=6)} for wc in lines],
        "locations": form["locations"], "movers": form["movers"],
    }


@require_POST
@tablet_api("op", "mov", "sup")
def api_overtime(request, actor):
    """Respuesta a una convocatoria de horas extra: SÍ (días + hasta qué hora) o NO (motivo)."""
    from django.core.exceptions import ValidationError as DjValidationError

    from .models import OvertimeRequest

    b = _body(request)
    req = OvertimeRequest.objects.filter(pk=b.get("req"), employee=actor.employee).first()
    if req is None:
        return _err("Esa convocatoria no es tuya o ya no existe.", 404)
    try:
        overtime.answer(req, yes=bool(b.get("yes")), reason=str(b.get("reason", "")), picks=b.get("picks") or [])
    except DjValidationError as exc:
        return _err(" ".join(exc.messages), 400)
    return JsonResponse({"ok": True, "state": state_data(actor)})


@require_POST
@tablet_api("op", "mov", "sup")
def api_checkin(request, actor):
    """«Aceptar» en la pantalla de llegada: confirma la asistencia y devuelve la pantalla ya habilitada."""
    checkin.confirm(actor.employee)
    return JsonResponse({"ok": True, "state": state_data(actor)})


@require_POST
def api_login(request):
    from mes_core.normalize import normalize_document

    b = _body(request)
    doc, pin = normalize_document(b.get("doc", "")), str(b.get("pin", "")).strip()
    key = f"piso:login:{request.META.get('REMOTE_ADDR', '')}:{doc}"
    if _too_many(key):
        return _err("Demasiados intentos. Espera un minuto.", 429)
    acc = FloorAccess.objects.select_related("employee").filter(employee__document=doc, active=True,
                                                                employee__active=True).first()
    if acc is None or not acc.check_pin(pin):
        _fail(key)
        return _err("Documento o PIN incorrectos. Si no tienes PIN, pídelo a tu supervisor.", 401)
    cache.delete(key)
    request.session.cycle_key()
    request.session[SESSION_KEY] = {"k": "emp", "id": acc.employee_id, "h": acc.pin_hash[-16:], "t": time.time()}
    request.session.set_expiry(14 * 86400)
    return JsonResponse({"ok": True, "state": _state_or_none(request)})


def _state_or_none(request):
    """Estado para la misma respuesta de entrar / vincular: la pantalla aparece sin una segunda consulta."""
    try:
        actor = get_actor(request)
        return state_data(actor) if actor else None
    except Exception:  # noqa: BLE001 — la tablet lo pide como siempre
        return None


@require_POST
def api_pair(request):
    b = _body(request)
    code = "".join(str(b.get("code", "")).split()).upper()
    key = f"piso:pair:{request.META.get('REMOTE_ADDR', '')}"
    if _too_many(key, 8):
        return _err("Demasiados intentos. Espera un minuto.", 429)
    lc = LineCode.objects.select_related("work_center").filter(pair_code=code).first() if code else None
    if lc is None:
        _fail(key)
        return _err("Código de línea incorrecto.", 401)
    request.session.cycle_key()
    request.session[SESSION_KEY] = {"k": "line", "id": lc.work_center_id, "code": lc.pair_code}
    request.session.set_expiry(180 * 86400)
    return JsonResponse({"ok": True, "line": lc.work_center.name, "state": _state_or_none(request)})


@require_POST
def api_logout(request):
    s = request.session.pop(SESSION_KEY, None) or {}
    if s.get("k") == "emp":
        FloorAccess.objects.filter(employee_id=s.get("id")).update(last_seen=None)
    return JsonResponse({"ok": True})


def _pick_task(actor: Actor, op_id) -> "floor.Operation | None":
    tasks = floor.line_tasks(actor.work_center) if actor.role == "line" else floor.employee_tasks(actor.employee)
    if op_id:
        return next((t for t in tasks if str(t.pk) == str(op_id)), None)
    return floor.line_current(actor.work_center, tasks) if actor.role == "line" else floor.current_task(
        actor.employee, tasks)


@require_POST
@tablet_api("op", "line")
def api_mark(request, actor):
    b = _body(request)
    op = _pick_task(actor, b.get("op"))
    if op is None:
        return _err("Aún no tienes una tarea asignada." if actor.role == "op" else
                    "Esta línea aún no tiene una operación liberada.", 409)
    kind = b.get("k") or Mark.Kind.GOOD
    mark, finished = floor.add_mark(operation=op, kind=kind, qty=b.get("q", 1),
                                    employee=actor.employee if actor.role == "op" else None,
                                    work_center=actor.work_center if actor.role == "line" else op.work_center,
                                    times=b.get("t"), ean=str(b.get("e") or ""))
    op.refresh_from_db(fields=["qty_good", "qty_scrap", "status"])
    return JsonResponse({"ok": True, "finished": finished, "id": mark.pk, "op": op.pk,
                         "task": {"name": op.name, "order": op.order.number, "done": floor.num(op.qty_good),
                                  "pending": floor.num(op.qty_pending), "scrap": floor.num(op.qty_scrap)}})


@require_POST
@tablet_api("op", "line", "sup")
def api_report(request, actor):
    """Reporte por tramo de tiempo (líneas continuas).

    Operario: lo suyo («g» buenas, «x» rechazos). Supernumerario y tablet de la línea: el total de la línea, que se
    reparte entre el personal de la etapa, o por persona (``per``: [{id, g, x}])."""
    b = _body(request)
    if actor.role == "op":
        op = _pick_task(actor, b.get("op"))
        if op is None:
            return _err("Aún no tienes una tarea asignada.", 409)
        if reporting.mode_of(op)["mode"] != reporting.INTERVAL:
            return _err("En esta operación no reportas por tramos: la reporta el supernumerario o se marca con toques.",
                        409)
        ir, finished = reporting.report_interval(operation=op, kind=IntervalReport.Kind.OWN, good=b.get("g"),
                                                 scrap=b.get("x"), reporter=actor.employee,
                                                 ean=str(b.get("e") or ""), leftover=b.get("l"))
    else:
        op = floor._ops_qs().filter(pk=b.get("op") or 0).first()
        if op is None:
            return _err("Esa operación ya no está abierta.", 409)
        if actor.role == "line" and op.work_center_id != actor.work_center.pk:
            return _err("Esa operación no es de esta línea.", 403)
        if actor.role == "sup" and not reporting.can_report_line(actor.employee, op):
            return _err("Esa línea no está entre las tuyas: agrégala en «Líneas».", 403)
        per = b.get("per") if isinstance(b.get("per"), list) else None
        kind = IntervalReport.Kind.PEOPLE if per is not None else IntervalReport.Kind.LINE
        ir, finished = reporting.report_interval(
            operation=op, kind=kind, good=b.get("g"), scrap=b.get("x"), per=per,
            reporter=actor.employee if actor.role == "sup" else None, work_center=op.work_center,
            ean=str(b.get("e") or ""), leftover=b.get("l"))
    return JsonResponse({"ok": True, "id": ir.pk, "finished": finished, "good": floor.num(ir.qty_good),
                         "people": ir.people, "task": {"name": op.name, "order": op.order.number}})


@tablet_api("op", "sup")
def api_my_records(request, actor):
    """«Mis registros» (como Control tiempos de la app anterior): lo del operario en una fecha, o sus últimos 100."""
    try:
        from plugins.registros.records import Filters, search
    except Exception:  # noqa: BLE001 - módulo de registros desinstalado
        return JsonResponse({"ok": True, "rows": [], "off": True})
    today = timezone.localdate()
    hist = request.GET.get("h") == "1"
    day = parse_date(request.GET.get("fecha") or "") or today
    if day > today:
        return _err("No se permiten fechas futuras.", 400)
    f = Filters(employee_id=actor.employee.pk, estado="activos")
    if not hist:
        f.d_from = f.d_to = day
    rows, _ = search(f, limit=100)
    return JsonResponse({"ok": True, "day": day.isoformat(), "hist": hist, "rows": [{
        "fecha": r.fecha.isoformat() if r.fecha else "", "ini": floor.iso(r.inicio), "fin": floor.iso(r.fin),
        "pedido": r.pedido, "cliente": r.cliente, "proceso": r.proceso, "ean": r.ean, "desc": r.ean_desc,
        "u": floor.num(r.unidades), "x": floor.num(r.merma), "l": floor.num(r.sobrantes), "h": r.horas,
        "e": r.eficiencia, "sup": r.supernumerario} for r in rows[:100]]})


@require_POST
@tablet_api("op", "line", "sup")
def api_report_undo(request, actor):
    ir = IntervalReport.objects.filter(pk=_body(request).get("id")).select_related("report").first()
    if ir is None:
        return JsonResponse({"ok": True})
    mine = (ir.employee_id == actor.employee.pk) if actor.role != "line" else (
        ir.employee_id is None and ir.work_center_id == actor.work_center.pk)
    if not mine:
        return _err("Solo puedes anular tus propios reportes.", 403)
    reporting.undo_interval(ir)
    return JsonResponse({"ok": True})


@require_POST
@tablet_api("sup")
def api_sup_message(request, actor):
    """Mensaje del supernumerario a una persona, a varias, a una de sus líneas o a todo su personal."""
    b = _body(request)
    to, allowed = b.get("to", "p"), reporting.people_ids(actor.employee)
    wc = None
    if to == "l":
        wc = WorkCenter.objects.filter(pk=b.get("line") or 0).first()
        if wc is None or wc.pk not in reporting.sup_lines(actor.employee):
            return _err("Esa línea no está entre las tuyas.", 403)
        ids = {i for i in floor._ops_qs().filter(work_center=wc).values_list("crew_assignments__employee", flat=True) if i}
        label = f"Línea {wc.name}"
    elif to == "a":
        ids, label = allowed, "Todo mi personal"
    else:
        ids = {int(i) for i in (b.get("ids") or []) if str(i).isdigit()}
        if not ids or not ids <= allowed:
            return _err("Elige a quién enviarlo (solo tu personal).", 403)
        label = ""
    employees = list(Employee.objects.filter(pk__in=ids, active=True))
    if not label:
        label = employees[0].display_name if len(employees) == 1 else f"{len(employees)} personas"
    floor.send_message(text=b.get("text", ""), employees=employees, user=None, audience=label, work_center=wc,
                       sender=actor.employee, alarm=b.get("alarm", True) is not False)
    return JsonResponse({"ok": True, "n": len(employees), "audience": label})


@require_POST
@tablet_api("sup")
def api_sup_lines(request, actor):
    b = _body(request)
    wc = WorkCenter.objects.filter(pk=b.get("wc") or 0, active=True).first()
    if wc is None:
        return _err("Línea no encontrada.", 404)
    reporting.set_tablet_line(actor.employee, wc, bool(b.get("on")))
    return JsonResponse({"ok": True})


@require_POST
@tablet_api("sup")
def api_sup_ean(request, actor):
    """El supernumerario abre / cierra las referencias (EAN) de una operación de sus líneas o cambia su orden."""
    if not apps.is_installed("plugins.eanes"):
        return _err("El módulo de validación EAN no está instalado.", 404)
    from django.core.exceptions import ValidationError

    from plugins.eanes import eans

    b = _body(request)
    op = Operation.objects.filter(pk=b.get("op") or 0).select_related("order", "stage").first()
    if op is None:
        return _err("Operación no encontrada.", 404)
    if not reporting.can_report_line(actor.employee, op):
        return _err("Esa operación no está en tus líneas.", 403)
    try:
        eans.set_control(op, str(b.get("e") or ""), str(b.get("a") or ""), who=actor.employee.display_name)
    except ValidationError as exc:
        return _err(" ".join(exc.messages))
    bump("g", f"l{op.work_center_id}")
    return JsonResponse({"ok": True})


def _sup_stop(actor, b, cause):
    """El supernumerario pone paros a una persona de su personal o a toda una de sus líneas (p. ej. en banda)."""
    if cause not in floor.STOP_CAUSES:
        return _err("Motivo no válido.")
    lines = reporting.sup_lines(actor.employee)
    if b.get("emp"):
        if int(b["emp"]) not in reporting.people_ids(actor.employee):
            return _err("Esa persona no está en tus líneas.", 403)
        emp = get_object_or_404(Employee, pk=b["emp"])
        op = floor._ops_qs().filter(crew_assignments__employee=emp, work_center_id__in=list(lines)).first() or \
            floor.current_task(emp, floor.employee_tasks(emp))
        st = floor.start_stop(cause=cause, employee=emp, operation=op, work_center=op.work_center if op else None)
    else:
        wc = WorkCenter.objects.filter(pk=b.get("wc") or 0).first()
        if wc is None or wc.pk not in lines:
            return _err("Esa línea no está entre las tuyas.", 403)
        st = floor.start_stop(cause=cause, work_center=wc)
    return JsonResponse({"ok": True, "id": st.pk})


@require_POST
@tablet_api("op", "line", "mov", "sup")
def api_stop(request, actor):
    b = _body(request)
    cause = str(b.get("cause", ""))
    if actor.role == "sup":
        return _sup_stop(actor, b, cause)
    if actor.role == "line":
        allowed = floor.STOP_CAUSES
        emp = None
        if b.get("emp"):
            crew = floor.line_crew(floor.line_tasks(actor.work_center))
            emp = next((e for e in crew if str(e.pk) == str(b["emp"])), None)
            if emp is None:
                return _err("Ese operario no está en esta línea.", 403)
        if cause not in allowed:
            return _err("Motivo no válido.")
        st = floor.start_stop(cause=cause, employee=emp, work_center=actor.work_center)
    elif actor.role == "mov":
        if cause not in transfers.MOVER_CAUSES:
            return _err("Motivo no válido.")
        st = floor.start_stop(cause=cause, employee=actor.employee)
    else:
        if cause not in floor.STOP_CAUSES:
            return _err("Motivo no válido.")
        op = _pick_task(actor, None)
        st = floor.start_stop(cause=cause, employee=actor.employee, operation=op,
                              work_center=op.work_center if op else None)
    return JsonResponse({"ok": True, "id": st.pk})


@require_POST
@tablet_api("op", "line", "mov", "sup")
def api_stop_close(request, actor):
    st = Stop.objects.filter(pk=_body(request).get("id"), end__isnull=True).first()
    if st is None:
        return JsonResponse({"ok": True})
    if actor.role == "sup":  # también puede cerrar una pausa (como el supervisor en Piso en vivo)
        ok = (st.employee_id in reporting.people_ids(actor.employee)) if st.employee_id else (
            st.work_center_id in reporting.sup_lines(actor.employee))
        if not ok:
            return _err("Ese paro no es de tu personal.", 403)
        floor.close_stop(st, force=True)
        return JsonResponse({"ok": True})
    if actor.role == "line":
        crew_ids = {e.pk for e in floor.line_crew(floor.line_tasks(actor.work_center))}
        if not (st.work_center_id == actor.work_center.pk and st.employee_id is None) and st.employee_id not in crew_ids:
            return _err("Ese paro no es de esta línea.", 403)
    elif st.employee_id != actor.employee.pk:
        return _err("Ese paro no es tuyo.", 403)
    floor.close_stop(st)
    return JsonResponse({"ok": True})


@require_POST
@tablet_api("op")
def api_break(request, actor):
    op = _pick_task(actor, None)
    floor.start_break(actor.employee, str(_body(request).get("kind", "")), operation=op)
    floor.bump_employee(actor.employee.pk, op.work_center_id if op else None)
    return JsonResponse({"ok": True})


@require_POST
@tablet_api("op")
def api_resume(request, actor):
    shift = actor.employee.shift
    op = _pick_task(actor, None)
    site = op.order.site if op else (shift.site if shift else None)
    floor.resume_break(actor.employee, _body(request).get("code", ""), site=site)
    return JsonResponse({"ok": True})


@require_POST
@tablet_api("op", "mov", "sup")
def api_read(request, actor):
    floor.mark_read(actor.employee, _body(request).get("id"))
    return JsonResponse({"ok": True})


@require_POST
@tablet_api("op", "mov", "sup")
def api_pin(request, actor):
    b = _body(request)
    key = f"piso:pin:{actor.employee.pk}"
    if _too_many(key):
        return _err("Demasiados intentos. Espera un minuto.", 429)
    new = str(b.get("new", "")).strip()
    if not actor.access.check_pin(str(b.get("pin", "")).strip()):
        _fail(key)
        return _err("El PIN actual no es correcto.", 403)
    if not (new.isdigit() and 4 <= len(new) <= 8):
        return _err("El PIN nuevo debe tener de 4 a 8 números.")
    actor.access.set_pin(new)
    actor.access.save(update_fields=["pin_hash", "updated_at"])
    request.session[SESSION_KEY] = {**request.session[SESSION_KEY], "h": actor.access.pin_hash[-16:]}
    return JsonResponse({"ok": True})


@require_POST
@tablet_api("mov", "line", "sup")
def api_transfer(request, actor, action):
    b = _body(request)
    if action == "pedir":
        if actor.role == "line":
            wc, by = actor.work_center, None
        elif actor.role == "sup":
            wc = WorkCenter.objects.filter(pk=b.get("wc") or 0, active=True).first()
            if wc is None or wc.pk not in reporting.sup_lines(actor.employee):
                return _err("Elige una de tus líneas.", 403)
            by = actor.employee
        else:
            return _err("Solo la tablet de la línea o el supernumerario piden material.", 403)
        m = b.get("m")  # sin «m»: el patinador de la línea · 0: abierto para cualquiera · id: ese patinador
        try:
            mover_id = None if m is None or m == "" else int(m)
        except (TypeError, ValueError):
            return _err("Patinador no válido.")
        t = transfers.request_material(wc, origin_id=b.get("o"), pallets=b.get("est") or 1, note=b.get("nota", ""),
                                       urgent=bool(b.get("pri")), mover_id=mover_id, requested_by=by)
        return JsonResponse({"ok": True, "id": t.pk, "mover": t.mover.display_name if t.mover_id else ""})
    if actor.role != "mov":
        return _err("Solo un patinador mueve traslados.", 403)
    t = get_object_or_404(Transfer, pk=b.get("id"))
    if action == "avanzar":
        t = transfers.advance(t, actor.employee)
        return JsonResponse({"ok": True, "status": t.status})
    if action == "incidencia":
        transfers.set_incident(t, actor.employee, str(b.get("c", "")))
        return JsonResponse({"ok": True})
    return _err("Acción no válida.", 404)


# ================================================================== MES: supervisores
def can_supervise(user) -> bool:
    return user.is_superuser or any(user.has_perm(p) for p in (
        "mes_core.supervise_crew", "mes_core.approve_plan", "mes_core.assign_supervisor"))


def can_configure(user) -> bool:
    return user.is_superuser or user.is_staff or user.has_perm("mes_core.approve_plan")


def supervisor_required(fn):
    @wraps(fn)
    @login_required
    def inner(request, *args, **kwargs):
        if not can_supervise(request.user):
            raise PermissionDenied
        return fn(request, *args, **kwargs)
    return inner


@supervisor_required
def live(request):
    return render(request, "piso/live.html", {
        "data_url": reverse("piso:live_data"), "pulse_url": reverse("piso:api_pulse"),
        "lines": WorkCenter.objects.order_by("site__code", "code"),
    })


@supervisor_required
def live_data(request):
    return JsonResponse(floor.board(request.user))


@require_POST
@supervisor_required
def live_message(request):
    b = _body(request)
    to, text = b.get("to", "p"), b.get("text", "")
    data = floor.board(request.user)
    scope = {c["id"]: c for c in data["cards"]}
    wc = None
    if to == "l":
        wc = get_object_or_404(WorkCenter, pk=b.get("line"))
        ids = list(floor._ops_qs().filter(work_center=wc).values_list("crew_assignments__employee", flat=True))
        ids = [i for i in ids if i]
        label = f"Línea {wc.name}"
    elif to == "b":
        ids = [c["id"] for c in data["cards"] if c["eff_day"] is not None and c["eff_day"] < 70]
        label = "Baja eficiencia (< 70 %)"
    elif to == "a":
        ids = [c["id"] for c in data["cards"] if c["eff_day"] is not None and c["eff_day"] > 100]
        label = "Alta eficiencia (> 100 %)"
    else:
        ids = [int(i) for i in (b.get("ids") or []) if str(i).isdigit() and int(i) in scope]
        label = scope[ids[0]]["name"] if len(ids) == 1 else f"{len(ids)} personas"
    employees = list(Employee.objects.filter(pk__in=ids, active=True))
    try:
        floor.send_message(text=text, employees=employees, user=request.user, audience=label, work_center=wc,
                           alarm=b.get("alarm", True) is not False)
    except FloorError as exc:
        return _err(exc.message)
    return JsonResponse({"ok": True, "n": len(employees), "audience": label})


@require_POST
@supervisor_required
def live_close_stop(request, pk):
    st = get_object_or_404(Stop, pk=pk)
    if not floor.sees_all(request.user) and st.operation_id and st.operation.supervisor_id != request.user.pk:
        raise PermissionDenied
    floor.close_stop(st, user=request.user, force=True)
    return JsonResponse({"ok": True})


def _parse_day(raw, default: date) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


@supervisor_required
def history_view(request):
    today = timezone.localdate()
    d_from = _parse_day(request.GET.get("desde"), today.replace(day=1))
    d_to = _parse_day(request.GET.get("hasta"), today)
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    d_to = min(d_to, d_from + timedelta(days=366))
    wc = WorkCenter.objects.filter(pk=request.GET.get("linea") or None).first()
    return render(request, "piso/history.html", {
        "h": floor.history(d_from, d_to, wc), "d_from": d_from, "d_to": d_to, "line": wc,
        "lines": WorkCenter.objects.order_by("site__code", "code"), "points_rule": floor.pts_per_minute(),
    })


# ---------------------------------------------------------------- traslados (oficina)
@supervisor_required
def transfers_view(request):
    site, sites = site_scope.site_and_sites(request)  # traslados entre ubicaciones: siempre de una bodega
    if request.method == "POST":
        _transfers_post(request, site)
        return redirect(f"{reverse('piso:transfers')}?site={site.pk if site else ''}")
    return render(request, "piso/transfers.html", {
        "site": site, "sites": sites, "o": transfers.office(site),
        "locations": Location.objects.filter(site=site).select_related("work_center") if site else [],
        "movers": transfers.movers_for(site),
        "lines": WorkCenter.objects.filter(site=site).order_by("code") if site else [],
        "orders": WorkOrder.objects.filter(site=site, status__in=floor.OPEN_ORDER).select_related("product")[:200]
        if site else [],
        "pulse_url": reverse("piso:api_pulse"),
    })


def _office_mover(request, site):
    pk = request.POST.get("pat")
    if not pk or not pk.isdigit() or int(pk) not in transfers.mover_ids(site):
        return None
    return Employee.objects.filter(pk=pk).first()


def _transfers_post(request, site):
    a = request.POST.get("a")
    try:
        if a == "nuevo" and site:
            req = request.POST.get("req")
            req_at = None
            if req:
                hh, mm = (int(x) for x in req.split(":")[:2])
                req_at = timezone.localtime().replace(hour=hh, minute=mm, second=0, microsecond=0)
            transfers.create(
                site=site, user=request.user,
                origin=Location.objects.filter(site=site, pk=request.POST.get("o") or None).first(),
                destination=Location.objects.filter(site=site, pk=request.POST.get("d") or None).first(),
                mover=_office_mover(request, site),
                order=WorkOrder.objects.filter(site=site, pk=request.POST.get("lote") or None).first(),
                pallets=int(request.POST.get("est") or 1), note=request.POST.get("nota", ""),
                urgent=request.POST.get("pri") == "1", required_at=req_at)
            messages.success(request, "Traslado creado.")
        elif a == "asignar":
            t = get_object_or_404(Transfer, pk=request.POST.get("id"), status=Transfer.Status.PENDING)
            t.mover = _office_mover(request, site)
            t.save(update_fields=["mover"])
            bump("t", f"e{t.mover_id}" if t.mover_id else "")
        elif a == "cancelar":
            t = get_object_or_404(Transfer, pk=request.POST.get("id"))
            if t.status != Transfer.Status.DELIVERED:
                t.delete()
                bump("t", f"l{t.work_center_id}" if t.work_center_id else "")
                messages.info(request, "Traslado cancelado.")
        elif a == "ubicacion" and site and can_configure(request.user):
            name = " ".join(request.POST.get("n", "").split())[:80]
            if name:
                Location.objects.get_or_create(site=site, name=name, defaults={
                    "distance": min(3, max(1, int(request.POST.get("f") or 1))),
                    "work_center": WorkCenter.objects.filter(site=site, pk=request.POST.get("m") or None).first()})
        elif a == "quitar_ubicacion" and can_configure(request.user):
            Location.objects.filter(pk=request.POST.get("id")).update(active=False)
        elif a == "activar_ubicacion" and can_configure(request.user):
            Location.objects.filter(pk=request.POST.get("id")).update(active=True)
    except (FloorError, ValueError) as exc:
        messages.error(request, getattr(exc, "message", "Revisa los datos del traslado."))


# ---------------------------------------------------------------- tablets y QR
def _qr_svg(data: str, scale: int = 5) -> str:
    try:
        import segno
    except ImportError:  # la dependencia es opcional: sin ella se muestra solo el código
        return ""
    # sin width/height fijos y con viewBox: el QR se ajusta al ancho de su caja (no se sale en pantallas pequeñas)
    return segno.make(data, error="m").svg_inline(scale=scale, dark="#111", light="#fff", omitsize=True)


@login_required
def codes(request):
    if not can_configure(request.user):
        raise PermissionDenied
    if request.method == "POST":
        lc = get_object_or_404(LineCode, pk=request.POST.get("id"))
        if request.POST.get("a") == "tablet":
            lc.pair_code = new_code()
            messages.info(request, f"Nuevo código de tablet para {lc.work_center.name}: la tablet anterior queda desvinculada.")
        elif request.POST.get("a") == "pausa":
            lc.pause_code = new_pause_code()
            messages.info(request, f"Nuevo QR de pausas para {lc.work_center.name}: reimprímelo, el anterior deja de servir.")
        lc.save()
        return redirect("piso:codes")
    base = request.build_absolute_uri(reverse("piso:tablet"))
    rows = []
    for wc in WorkCenter.objects.filter(active=True).select_related("site").order_by("site__code", "code"):
        lc = LineCode.for_line(wc)
        rows.append({"wc": wc, "lc": lc, "pair_url": f"{base}?linea={lc.pair_code}",
                     "pair_qr": _qr_svg(f"{base}?linea={lc.pair_code}", 4), "pause_qr": _qr_svg(lc.pause_code, 6)})
    return render(request, "piso/codes.html", {
        "rows": rows, "base": base, "base_qr": _qr_svg(base, 5),
        "n_access": FloorAccess.objects.filter(active=True).count(),
        "n_movers": FloorAccess.objects.filter(active=True, role=FloorAccess.Role.MOVER).count(),
        "n_employees": Employee.objects.filter(active=True).count(),
        "only": request.GET.get("imprimir"),
    })


# ---------------------------------------------------------------- forma de reporte (MES)
def _interval(raw, default=None):
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return min(480, max(5, v))


@supervisor_required
def reporting_view(request):
    """Cómo reporta cada línea y cada proceso, y por qué líneas reporta cada supernumerario."""
    from mes_core.models import PlanStage

    edit_cfg = can_configure(request.user)
    modes = PlanStage.ReportMode
    if request.method == "POST":
        a = request.POST.get("a")
        if a == "lineas" and edit_cfg:
            for wc in WorkCenter.objects.filter(active=True):
                if f"m{wc.pk}" not in request.POST:
                    continue
                lc = LineCode.for_line(wc)
                mode = request.POST.get(f"m{wc.pk}", "")
                lc.report_mode = mode if mode in modes.values else ""
                lc.interval_minutes = _interval(request.POST.get(f"i{wc.pk}"), lc.interval_minutes)
                lc.save(update_fields=["report_mode", "interval_minutes", "updated_at"])
            bump("g", "b")
            messages.success(request, "Forma de reportar de las líneas guardada.")
        elif a == "procesos" and edit_cfg:
            names = request.POST.getlist("pn")
            for i, name in enumerate(names):
                name = " ".join(name.split())[:120]
                if not name:
                    continue
                mode = request.POST.get(f"pm{i}", "")
                key = ProcessReporting.key_of(name)
                if mode in modes.values:
                    pr = ProcessReporting.objects.filter(key=key).first() or ProcessReporting(name=name)
                    pr.report_mode, pr.interval_minutes = mode, _interval(request.POST.get(f"pi{i}"))
                    pr.save()
                else:
                    ProcessReporting.objects.filter(key=key).delete()
            bump("g", "b")
            messages.success(request, "Forma de reportar de los procesos guardada.")
        elif a == "sup":
            emp = get_object_or_404(Employee, pk=request.POST.get("emp"), floor_access__role=FloorAccess.Role.SUPERNUMERARY)
            want = {int(x) for x in request.POST.getlist("wc") if x.isdigit()}
            have = set(SupernumeraryLine.objects.filter(employee=emp, source=SupernumeraryLine.Source.MES)
                       .values_list("work_center_id", flat=True))
            SupernumeraryLine.objects.filter(employee=emp, source=SupernumeraryLine.Source.MES,
                                             work_center_id__in=have - want).delete()
            for wc in WorkCenter.objects.filter(pk__in=want - have, active=True):
                SupernumeraryLine.objects.create(employee=emp, work_center=wc, source=SupernumeraryLine.Source.MES,
                                                 assigned_by=request.user)
            bump(f"e{emp.pk}")
            messages.success(request, f"Líneas de {emp.display_name} guardadas.")
        return redirect(reverse("piso:reporting") + ("#sup" if a == "sup" else "#procesos" if a == "procesos" else ""))

    lines = list(WorkCenter.objects.filter(active=True).select_related("site").order_by("site__code", "code"))
    codes_by = {lc.work_center_id: lc for lc in LineCode.objects.filter(work_center__in=lines)}
    procs = {p.key: p for p in ProcessReporting.objects.all()}
    names = {}
    for n in PlanStage.objects.order_by("-order_id").values_list("name", flat=True)[:3000]:
        names.setdefault(ProcessReporting.key_of(n), " ".join(n.split()))
    for k, p in procs.items():
        names[k] = p.name
    process_rows = [{"name": n, "p": procs.get(k)} for k, n in sorted(names.items(), key=lambda kv: kv[1].lower())][:200]
    sups = []
    for acc in (FloorAccess.objects.filter(role=FloorAccess.Role.SUPERNUMERARY, active=True, employee__active=True)
                .select_related("employee__supervisor").order_by("employee__full_name")):
        tags = reporting.sup_lines(acc.employee)
        sups.append({"emp": acc.employee, "mes": {i for i, t in tags.items() if "mes" in t},
                     "tablet": [wc for wc in lines if "tablet" in tags.get(wc.pk, ())],
                     "supervisor": [wc for wc in lines if "supervisor" in tags.get(wc.pk, ())]})
    return render(request, "piso/reporting.html", {
        "edit_cfg": edit_cfg, "modes": modes.choices,
        "line_rows": [{"wc": wc, "lc": codes_by.get(wc.pk)} for wc in lines],
        "process_rows": process_rows, "sups": sups, "lines": lines,
    })


def pins_xlsx(rows) -> HttpResponse:
    """Excel con los PIN recién generados (solo se pueden ver en ese momento)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "PIN de tablet"
    ws.append(["Documento", "Nombre", "Rol", "PIN"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append(list(r))
    for col, width in zip("ABCD", (16, 34, 14, 10)):
        ws.column_dimensions[col].width = width
    resp = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="pin-tablet-{timezone.localdate():%Y%m%d}.xlsx"'
    wb.save(resp)
    return resp
