"""Llegada a la planta: antes de cualquier cosa la persona ve a dónde va y confirma su asistencia.

Una sola pantalla: bodega, línea, operación y pedido, supervisor, supernumerario y turno. Al aceptar queda la asistencia
del día como «Presente» en el MES (la misma que ve el supervisor en «Mi turno») y la tablet se habilita. Mientras no
acepte, el servidor no deja marcar, reportar ni nada más.

Vale por 14 horas: alcanza para un turno de noche que pasa la medianoche; al otro día se vuelve a pedir.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from mes_core.models import Attendance, Employee

from . import floor
from .models import CheckIn, FloorAccess

VALID = timedelta(hours=14)
BLOCKED_MSG = "Primero confirma tu asistencia: acepta la pantalla de llegada."


def _key(emp_id: int) -> str:
    return f"piso:checkin:{emp_id}"


def confirmed(emp: Employee, now=None) -> bool:
    """¿Ya confirmó su llegada en las últimas 14 horas? (memoria del servidor primero: lo consulta cada acción)."""
    now = now or timezone.now()
    at = cache.get(_key(emp.pk))
    if at is not None and now.timestamp() - at < VALID.total_seconds():
        return True
    last = (CheckIn.objects.filter(employee=emp, at__gte=now - VALID).order_by("-at")
            .values_list("at", flat=True).first())
    if last is not None:
        cache.set(_key(emp.pk), last.timestamp(), int(VALID.total_seconds()))
        return True
    return False


def deadline(shift, now) -> "datetime | None":
    """Hora máxima para confirmar la llegada en el turno que está por empezar o en curso (la del turno, «Llegada
    hasta» en Turnos y personal). Turnos de noche: si se confirma pasada la medianoche cuenta el turno de ayer."""
    if shift is None or shift.checkin_deadline is None:
        return None
    local = timezone.localtime(now)
    tz = local.tzinfo
    start = datetime.combine(local.date(), shift.start_time, tzinfo=tz)
    if start - local > timedelta(hours=6):      # p. ej. 00:30 con turno de 22:00: es el turno que empezó ayer
        start -= timedelta(days=1)
    elif local - start > timedelta(hours=18):   # el de mañana a primera hora
        start += timedelta(days=1)
    limit = datetime.combine(start.date(), shift.checkin_deadline, tzinfo=tz)
    if shift.checkin_deadline < shift.start_time and (start.time().hour - shift.checkin_deadline.hour) > 12:
        limit += timedelta(days=1)  # turno 23:30 con llegada hasta 00:10
    return limit


def _user_name(u) -> str:
    return (u.get_full_name() or u.get_username()) if u else ""


def info(emp: Employee, now=None) -> dict:
    """Lo que ve en la pantalla de llegada: a dónde va hoy y con quién."""
    now = now or timezone.now()
    acc = FloorAccess.objects.filter(employee=emp).first()
    role = acc.role if acc else FloorAccess.Role.OPERATOR
    shift = floor.employee_shift(emp, timezone.localdate(now))
    tasks = floor.employee_tasks(emp)
    op = floor.current_task(emp, tasks)
    wc = op.work_center if op and op.work_center_id else None
    site = (wc.site if wc else None) or (shift.site if shift and shift.site_id else None) or emp.site
    sup_user = (op.supervisor if op and op.supervisor_id else None) or emp.supervisor
    supers: list[str] = []
    if wc is not None:
        cond = Q(employee__sup_lines__work_center=wc)
        if op.supervisor_id:
            cond |= Q(employee__supervisor_id=op.supervisor_id)
        supers = sorted({a.employee.display_name for a in FloorAccess.objects.filter(
            cond, role=FloorAccess.Role.SUPERNUMERARY, active=True, employee__active=True)
            .exclude(employee=emp).select_related("employee")})
    lines = []
    if role == FloorAccess.Role.SUPERNUMERARY:
        from . import reporting

        tags = reporting.sup_lines(emp)
        from mes_core.models import WorkCenter

        lines = [f"{w.code} · {w.name}" for w in WorkCenter.objects.filter(pk__in=list(tags), active=True)
                 .order_by("code")]
    pending = [t for t in tasks if not floor.scheduled(t)]
    limit = deadline(shift, now)
    return {
        "deadline": floor.iso(limit), "late": bool(limit and now > limit),
        "name": emp.display_name.split(" ")[0] if emp.display_name else "",
        "full_name": emp.display_name,
        "role": FloorAccess.Role(role).label.split(" (")[0],
        "site": site.name if site else "",
        "site_code": site.code if site else "",
        "line": f"{wc.code} · {wc.name}" if wc else "",
        "operation": op.name if op else "",
        "order": " · ".join(x for x in (op.order.number, op.order.customer.name if op.order.customer_id else "",
                                         op.order.product.name) if x) if op else "",
        "start": floor.iso(op.planned_start) if op else None,
        "more": max(0, len([t for t in tasks if floor.scheduled(t)]) - 1),
        "waiting": len(pending),
        "supervisor": _user_name(sup_user),
        "supernumeraries": supers,
        "lines": lines,
        "shift": f"{shift.name} {shift.start_time:%H:%M}–{shift.end_time:%H:%M}" if shift else "",
        "op_id": op.pk if op else None, "wc_id": wc.pk if wc else None,
    }


def state(emp: Employee, now=None) -> dict:
    now = now or timezone.now()
    if confirmed(emp, now):
        return {"done": True}
    return {"done": False, "info": info(emp, now), "now": floor.iso(now)}


def confirm(emp: Employee, now=None) -> CheckIn:
    """Acepta la pantalla de llegada: guarda lo que vio y deja su asistencia del día como «Presente», o «Llegó tarde»
    si confirma después de la hora máxima de su turno (si el supervisor ya la había marcado presente o tarde, se
    respeta)."""
    from mes_core.dbqueue import write_turn
    from mes_core.staffing import sync_shift_absences

    now = now or timezone.now()
    day = timezone.localdate(now)
    data = info(emp, now)
    with write_turn(), transaction.atomic():
        ci, _ = CheckIn.objects.update_or_create(employee=emp, day=day, defaults={
            "at": now, "work_center_id": data["wc_id"], "operation_id": data["op_id"], "info": data})
        att = Attendance.objects.filter(employee=emp, day=day).first()
        note = f"Confirmó su llegada en la tablet a las {timezone.localtime(now):%H:%M}"
        if data["deadline"]:
            limit = timezone.localtime(deadline(floor.employee_shift(emp, day), now))
            note += f" (hora máxima {limit:%H:%M}{' · llegó tarde' if data['late'] else ''})"
        if att is None or att.status not in Attendance.WORKING:
            shift = (att.shift if att and att.shift_id else None) or floor.employee_shift(emp, day)
            status = Attendance.Status.LATE if data["late"] else Attendance.Status.PRESENT
            Attendance.objects.update_or_create(employee=emp, day=day, defaults={
                "status": status, "shift": shift, "notes": note[:255], "confirmed_by": None})
            if shift:
                sync_shift_absences(day, {shift.pk})
        elif not att.notes:
            Attendance.objects.filter(pk=att.pk).update(notes=note[:255])
    cache.set(_key(emp.pk), now.timestamp(), int(VALID.total_seconds()))
    from .live import bump

    bump(f"e{emp.pk}", "g")
    return ci
