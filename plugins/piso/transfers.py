"""Traslados de estibas con patinadores. No hay tiempo estándar: cada ruta se compara con la mediana de sus propios
viajes entregados (mínimo 3), que el sistema aprende solo."""

from __future__ import annotations

from datetime import timedelta
from statistics import median

from django.db import transaction
from django.utils import timezone

from mes_core.models import Employee, Operation, WorkCenter, WorkOrder

from .floor import OPEN_ORDER, FloorError, day_start, iso, month_start, open_stop, stop_json, unread_messages
from .live import bump
from .models import FloorAccess, Location, MoverAssignment, Stop, Transfer

S = Transfer.Status
MOVER_CAUSES = ("Baño", "Batería", "Espera de montacargas", "Falla del equipo")
POINTS_PER_WEIGHT = 10
_NEXT = {S.PENDING: (S.ACCEPTED, "accepted_at"), S.ACCEPTED: (S.LOADED, "loaded_at"),
         S.LOADED: (S.DELIVERED, "delivered_at")}
_RANK = {S.LOADED: 0, S.ACCEPTED: 1, S.PENDING: 2}


def _sort(items):
    far = timezone.now() + timedelta(days=3650)
    return sorted(items, key=lambda t: (_RANK.get(t.status, 3), not t.urgent, t.required_at or far, t.created_at))


def _qs():
    return Transfer.objects.select_related("origin", "destination", "work_center", "mover", "order", "requested_by")


# ---------------------------------------------------------------- patinadores: de oficio o asignados a una operación
def _open_assignments():
    return (MoverAssignment.objects.filter(operation__order__status__in=OPEN_ORDER)
            .exclude(operation__status=Operation.Status.DONE)
            .select_related("operation__work_center", "operation__order", "employee"))


def assigned_ops(emp: Employee) -> list[Operation]:
    """Operaciones abiertas en las que el supervisor puso a esta persona como patinador."""
    return [a.operation for a in _open_assignments().filter(employee=emp).order_by("operation__planned_start")]


def is_assigned_mover(emp_id: int) -> bool:
    return _open_assignments().filter(employee_id=emp_id).exists()


def movers_for(site=None) -> list[dict]:
    """A quién se le puede pedir material: los patinadores de oficio (acceso «patinador») y quienes están asignados
    como patinador a una operación abierta. Con las líneas a las que cada uno está asignado."""
    out: dict[int, dict] = {}
    # la bolsa de personal es una sola: un patinador de oficio sirve en cualquier bodega
    qs = Employee.objects.filter(floor_access__role=FloorAccess.Role.MOVER, floor_access__active=True, active=True)
    for e in qs.order_by("full_name"):
        out[e.pk] = {"id": e.pk, "name": e.display_name, "lines": []}
    assigned = _open_assignments()
    if site is not None:
        assigned = assigned.filter(operation__order__site=site)
    for a in assigned:
        r = out.setdefault(a.employee_id, {"id": a.employee_id, "name": a.employee.display_name, "lines": []})
        code = a.operation.work_center.code if a.operation.work_center_id else ""
        if code and code not in r["lines"]:
            r["lines"].append(code)
    return sorted(out.values(), key=lambda r: (not r["lines"], r["name"]))


def mover_ids(site=None) -> set[int]:
    return {m["id"] for m in movers_for(site)}


def default_mover(wc: WorkCenter) -> Employee | None:
    """El patinador asignado a la operación abierta de la línea (la que va primero)."""
    a = (_open_assignments().filter(operation__work_center=wc)
         .order_by("operation__planned_start", "operation__sequence").first())
    return a.employee if a else None


def set_operation_mover(op: Operation, employee: Employee | None, user=None) -> str:
    """Deja a ``employee`` como el patinador de la operación (o ninguno). Devuelve un texto para el aviso."""
    from django.core.exceptions import ValidationError

    if op.status == Operation.Status.DONE:
        raise ValidationError("La operación ya terminó.")
    current = list(op.mover_assignments.select_related("employee"))
    if employee is not None:
        if not employee.active:
            raise ValidationError(f"{employee.display_name} está inactivo.")
        if op.crew_assignments.filter(employee=employee).exists():
            raise ValidationError(f"{employee.display_name} está en la cuadrilla: el patinador debe ser otra persona.")
    if [a.employee_id for a in current] == ([employee.pk] if employee else []):
        return ""
    gone = [a.employee_id for a in current]
    op.mover_assignments.all().delete()
    if employee is not None:
        MoverAssignment.objects.create(operation=op, employee=employee,
                                       assigned_by=user if user and user.is_authenticated else None)
    bump("g", "t", *[f"e{i}" for i in gone], f"e{employee.pk}" if employee else "",
         f"l{op.work_center_id}" if op.work_center_id else "")
    if employee is None:
        return "Sin patinador para esta operación."
    has_app = FloorAccess.objects.filter(employee=employee, active=True).exists()
    return (f"{employee.display_name} queda como patinador: " +
            ("al abrir la app de planta verá la pantalla de patinador mientras dure la operación."
             if has_app else "aún no tiene acceso a la app de planta (dale un PIN en Tablets y QR)."))


def mover_queue(mover: Employee):
    return _sort(_qs().filter(mover=mover).exclude(status=S.DELIVERED))


def open_pool(site=None):
    qs = _qs().filter(status=S.PENDING, mover__isnull=True)
    if site is not None:
        qs = qs.filter(site=site)
    return _sort(qs)


def tr_json(t: Transfer, medians=None) -> dict:
    med = (medians or {}).get(t.route_key)
    return {
        "id": t.pk, "status": t.status, "status_label": t.get_status_display(),
        "origin": t.origin.name if t.origin_id else "—",
        "destination": t.destination.name if t.destination_id else (t.work_center.name if t.work_center_id else "—"),
        "line": t.work_center.name if t.work_center_id else "", "order": t.order.number if t.order_id else "",
        "pallets": t.pallets, "note": t.note, "urgent": t.urgent, "required_at": iso(t.required_at),
        "from_line": t.from_line, "incident": t.incident, "mover": t.mover.display_name if t.mover_id else "",
        "by": t.requested_by.display_name if t.requested_by_id else "",
        "mover_id": t.mover_id, "created_at": iso(t.created_at), "accepted_at": iso(t.accepted_at),
        "loaded_at": iso(t.loaded_at), "delivered_at": iso(t.delivered_at), "median_s": med,
    }


def route_medians(site=None, since_days: int = 90) -> dict[str, float]:
    qs = Transfer.objects.filter(status=S.DELIVERED, loaded_at__isnull=False, delivered_at__isnull=False,
                                 delivered_at__gte=timezone.now() - timedelta(days=since_days))
    if site is not None:
        qs = qs.filter(site=site)
    groups: dict[str, list[float]] = {}
    for t in qs.only("origin_id", "destination_id", "loaded_at", "delivered_at"):
        groups.setdefault(t.route_key, []).append((t.delivered_at - t.loaded_at).total_seconds())
    return {k: median(v) for k, v in groups.items() if len(v) >= 3}


# ---------------------------------------------------------------- acciones
@transaction.atomic
def advance(transfer: Transfer, mover: Employee) -> Transfer:
    t = Transfer.objects.select_for_update().get(pk=transfer.pk)
    if t.mover_id and t.mover_id != mover.pk:
        raise FloorError("Ese traslado lo tiene otro patinador.")
    if open_stop(employee=mover):
        raise FloorError("Hay un paro abierto: reanuda para continuar.")
    nxt = _NEXT.get(t.status)
    if nxt is None:
        raise FloorError("Ese traslado ya fue entregado.")
    status, field = nxt
    if status != S.DELIVERED and Transfer.objects.filter(mover=mover, status__in=(S.ACCEPTED, S.LOADED)).exclude(
            pk=t.pk).exists():
        raise FloorError("Termina primero el traslado que tienes en curso.")
    t.status = status
    setattr(t, field, timezone.now())
    t.mover = mover
    t.save(update_fields=["status", field, "mover"])
    bump("t", f"e{mover.pk}", f"l{t.work_center_id}" if t.work_center_id else "")
    return t


def set_incident(transfer: Transfer, mover: Employee, cause: str) -> None:
    if transfer.mover_id and transfer.mover_id != mover.pk:
        raise FloorError("Ese traslado lo tiene otro patinador.")
    transfer.incident = cause if cause in Transfer.INCIDENTS else ""
    transfer.save(update_fields=["incident"])
    bump("t", f"e{mover.pk}")


def request_material(wc: WorkCenter, *, origin_id=None, pallets=1, note: str = "", urgent: bool = False,
                     mover_id=None, requested_by: Employee | None = None) -> Transfer:
    """Pedido de material de una línea (tablet de la línea o supernumerario). ``mover_id``: a qué patinador va; sin
    elegir, al patinador asignado a la operación de la línea; si no hay, queda abierto para cualquiera."""
    origin = Location.objects.filter(pk=origin_id, site=wc.site_id).first() if origin_id else None
    dest = Location.objects.filter(work_center=wc, active=True).first()
    mover = None
    if mover_id:
        if int(mover_id) not in mover_ids(wc.site):
            raise FloorError("Esa persona no es patinador de esta bodega.")
        mover = Employee.objects.get(pk=mover_id)
    elif mover_id is None:
        mover = default_mover(wc)
    try:
        pallets = max(1, min(99, int(pallets or 1)))
    except (TypeError, ValueError):
        pallets = 1
    order = (Operation.objects.filter(work_center=wc, order__status__in=OPEN_ORDER)
             .exclude(status=Operation.Status.DONE).order_by("planned_start").values_list("order_id", flat=True).first())
    t = Transfer.objects.create(site=wc.site, work_center=wc, origin=origin, destination=dest, from_line=True,
                                pallets=pallets, note=" ".join(str(note).split())[:200], urgent=bool(urgent),
                                mover=mover, requested_by=requested_by,
                                order=WorkOrder.objects.filter(pk=order).first() if order else None)
    bump("t", f"l{wc.pk}", f"e{mover.pk}" if mover else "")
    return t


def create(*, site, user, origin=None, destination=None, mover=None, order=None, pallets=1, note="", urgent=False,
           required_at=None) -> Transfer:
    if origin is None or destination is None:
        raise FloorError("Elige origen y destino.")
    t = Transfer.objects.create(site=site, origin=origin, destination=destination, mover=mover, order=order,
                                work_center=destination.work_center, pallets=max(1, int(pallets or 1)),
                                note=" ".join(str(note).split())[:200], urgent=urgent, required_at=required_at,
                                created_by=user)
    bump("t", f"e{mover.pk}" if mover else "")
    return t


# ---------------------------------------------------------------- indicadores
def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def kpis(mover: Employee, since, now=None, medians=None, transfers=None, stops=None) -> dict:
    now = now or timezone.now()
    medians = medians if medians is not None else route_medians()
    mine = transfers if transfers is not None else list(
        _qs().filter(mover=mover).filter(accepted_at__isnull=False).exclude(delivered_at__lt=since))
    done = [t for t in mine if t.status == S.DELIVERED and t.delivered_at and t.delivered_at >= since]
    active = [t for t in mine if t.accepted_at and (t.delivered_at or now) > since]
    first = max(since, min((t.accepted_at for t in active), default=now))
    stops = stops if stops is not None else list(Stop.objects.filter(employee=mover, start__lt=now).exclude(
        end__lt=first))
    busy = sum(((t.delivered_at or now) - max(t.accepted_at, since)).total_seconds() for t in active)
    paused = sum(s.minutes(first, now) * 60 for s in stops)
    avail = (now - first).total_seconds() - paused if active else 0
    with_req = [t for t in done if t.required_at]
    vs_route = _avg([min(150.0, medians[t.route_key] / (t.delivered_at - t.loaded_at).total_seconds() * 100)
                     for t in done if t.route_key in medians and t.loaded_at
                     and (t.delivered_at - t.loaded_at).total_seconds() > 0])
    weight = sum(t.weight for t in done)
    return {
        "trips": len(done), "pallets": sum(t.pallets for t in done), "points": weight * POINTS_PER_WEIGHT,
        "response_s": _avg([(t.accepted_at - t.created_at).total_seconds() for t in done if t.accepted_at]),
        "pickup_s": _avg([(t.loaded_at - t.accepted_at).total_seconds() for t in done if t.loaded_at and t.accepted_at]),
        "transport_s": _avg([(t.delivered_at - t.loaded_at).total_seconds() for t in done if t.loaded_at]),
        "cycle_s": _avg([(t.delivered_at - t.accepted_at).total_seconds() for t in done if t.accepted_at]),
        "on_time": (sum(1 for t in with_req if t.delivered_at <= t.required_at) / len(with_req) * 100) if with_req else None,
        "vs_route": vs_route,
        "utilization": min(100.0, busy / avail * 100) if avail > 60 else None,
        "per_hour": len(done) / (avail / 3600) if avail > 900 else None,
    }


def mover_state(emp: Employee, now=None) -> dict:
    now = now or timezone.now()
    t0, m0 = day_start(now), month_start(now)
    medians = route_medians()
    queue = mover_queue(emp)
    cur = queue[0] if queue else None
    ops = assigned_ops(emp)
    site = cur.site if cur else (ops[0].order.site if ops else (emp.shift.site if emp.shift_id else emp.site))
    month = list(_qs().filter(mover=emp, status=S.DELIVERED, delivered_at__gte=m0).order_by("-delivered_at"))
    st = open_stop(employee=emp)
    stops_today = list(Stop.objects.filter(employee=emp).exclude(end__lt=t0).order_by("-start")[:20])
    today = kpis(emp, t0, now, medians)
    return {
        "kind": "patinador", "now": iso(now),
        "who": {"id": emp.pk, "name": emp.display_name, "position": emp.position.capitalize() if emp.position else ""},
        "assigned": [{"op": op.pk, "name": op.name, "order": op.order.number,
                      "line": op.work_center.code if op.work_center_id else ""} for op in ops],
        "current": tr_json(cur, medians) if cur else None,
        "queue": [tr_json(t, medians) for t in queue[1:]],
        "pool": [tr_json(t, medians) for t in open_pool(site)[:20]],
        "today": today, "month": {"trips": len(month), "pallets": sum(t.pallets for t in month),
                                  "points": sum(t.weight for t in month) * POINTS_PER_WEIGHT,
                                  "cycle_s": _avg([(t.delivered_at - t.accepted_at).total_seconds()
                                                   for t in month if t.accepted_at])},
        "delivered": [tr_json(t, medians) for t in month[:60]],
        "pending_pallets": sum(t.pallets for t in queue),
        "stop": stop_json(st), "stops_today": [stop_json(s) for s in stops_today],
        "causes": list(MOVER_CAUSES), "incidents": list(Transfer.INCIDENTS),
        "messages": unread_messages(emp, now),
    }


def line_requests(wc: WorkCenter, now=None, limit: int = 4) -> list[dict]:
    t0 = day_start(now)
    qs = _qs().filter(work_center=wc).exclude(status=S.DELIVERED, delivered_at__lt=t0).order_by("-created_at")[:limit]
    return [tr_json(t) for t in qs]


def request_form(site, wc: WorkCenter | None = None) -> dict:
    """Lo que necesita la tablet para pedir material: de dónde, a qué patinador y cuál va por defecto."""
    dm = default_mover(wc) if wc is not None else None
    return {"locations": [{"id": loc.id, "name": loc.name} for loc in Location.objects.filter(
                site=site, active=True, work_center__isnull=True)],
            "movers": movers_for(site), "default_mover": dm.pk if dm else None}


def office(site=None, now=None) -> dict:
    now = now or timezone.now()
    t0 = day_start(now)
    qs = _qs()
    if site is not None:
        qs = qs.filter(site=site)
    active = _sort(qs.exclude(status=S.DELIVERED))
    today = list(qs.filter(status=S.DELIVERED, delivered_at__gte=t0))
    pending = [t for t in active if t.status == S.PENDING]
    with_req = [t for t in today if t.required_at]
    medians = route_medians(site)
    movers = []
    for emp in Employee.objects.filter(pk__in=mover_ids(site)).order_by("full_name"):
        q = [t for t in active if t.mover_id == emp.pk]
        movers.append({"id": emp.pk, "name": emp.display_name, "kpis": kpis(emp, t0, now, medians),
                       "current": tr_json(q[0]) if q else None, "stop": stop_json(open_stop(employee=emp))})
    routes: dict[str, list[Transfer]] = {}
    for t in qs.filter(status=S.DELIVERED, loaded_at__isnull=False, delivered_at__gte=now - timedelta(days=90)):
        routes.setdefault(t.route_key, []).append(t)
    route_rows = sorted(({"origin": v[0].origin.name if v[0].origin_id else "—",
                          "destination": v[0].destination.name if v[0].destination_id else "—",
                          "trips": len(v), "median_s": medians.get(k)} for k, v in routes.items()),
                        key=lambda r: -r["trips"])
    return {
        "active": [tr_json(t, medians) for t in active], "pending": len(pending),
        "in_course": len(active) - len(pending), "delivered_today": len(today),
        "pallets_today": sum(t.pallets for t in today),
        "on_time": (sum(1 for t in with_req if t.delivered_at <= t.required_at) / len(with_req) * 100) if with_req else None,
        "oldest": iso(min((t.created_at for t in pending), default=None)),
        "line_wait_s": _avg([(t.delivered_at - t.created_at).total_seconds() for t in today if t.from_line]),
        "utilization": _avg([m["kpis"]["utilization"] for m in movers]),
        "movers": movers, "routes": route_rows[:30],
        "recent": [tr_json(t) for t in qs.filter(status=S.DELIVERED).order_by("-delivered_at")[:15]],
    }
