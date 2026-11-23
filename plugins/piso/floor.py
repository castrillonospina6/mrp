"""Lógica de las tablets de piso: qué tarea tiene cada operario (la manda el supervisor desde el MES), marcaciones que
se vuelven reportes de producción del MES, paros, pausas con QR, mensajes e indicadores (eficiencia, OEE, puntos)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from mes_core import services
from mes_core.models import Attendance, Employee, Operation, WorkCenter, WorkOrder

from .live import bump, bump_employee
from .models import FloorAccess, LineCode, Mark, Message, MessageRecipient, Stop

OPEN_ORDER = (WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS)
STOP_CAUSES = ("Baño", "Espera de trabajo", "Falta de material", "Alistamiento (esperando patinador)", "Falla mecánica",
               "Reprocesos")
BREAKS = Stop.BREAKS
DONE_MSG = "Ya completaste el 100% de esta tarea. No puedes marcar más unidades."
DONE_LINE_MSG = "La línea ya completó el 100% de esta operación. No se pueden marcar más unidades."
MERGE_MINUTES = 15  # las marcaciones de un operario se acumulan en un reporte del MES cada 15 minutos
NO_MARKS_ALERT = timedelta(minutes=10)


def pts_per_minute() -> float:
    """Incentivo (regla provisional, ajustable en settings): puntos = minutos estándar producidos × esto."""
    return float(getattr(settings, "PISO_PUNTOS_POR_MINUTO", 10))


def point_value() -> float:
    return float(getattr(settings, "PISO_VALOR_PUNTO", 1))


class FloorError(Exception):
    def __init__(self, message: str, done: bool = False):
        super().__init__(message)
        self.message = message
        self.done = done


# ---------------------------------------------------------------- utilidades
def num(value) -> float | int | None:
    if value is None:
        return None
    d = Decimal(value)
    return int(d) if d == d.to_integral() else float(round(d, 3))


def iso(dt) -> str | None:
    return timezone.localtime(dt).isoformat() if dt else None


def day_start(now=None) -> datetime:
    local = timezone.localtime(now or timezone.now())
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now=None) -> datetime:
    return day_start(now).replace(day=1)


def parse_qty(raw) -> Decimal:
    try:
        q = Decimal(str(raw if raw not in (None, "") else 1).replace(",", "."))
    except (InvalidOperation, ValueError):
        raise FloorError("Cantidad no válida.") from None
    if q <= 0 or q > Decimal("1000000"):
        raise FloorError("La cantidad debe ser mayor que cero.")
    return q


def ref_info(ops) -> dict:
    """Referencias (EAN) de cada operación con lo que falta de cada una (las aporta el módulo de validación EAN)."""
    from mes_core.plugins import registry

    ops = [op for op in ops if op is not None]
    return registry.report_info(ops) if ops else {}


UNSCHEDULED_MSG = ("«{name}» ({order}) aún no está en el programador: se ve en la cola pero no se puede reportar hasta "
                   "que planeación la programe.")


def scheduled(op: Operation) -> bool:
    """En la app de planta solo se trabaja lo programado: la operación (o el lote) tiene fecha en el programador. Un
    saldo que aún no se ha metido al programador se ve, pero bloqueado."""
    return op.planned_start is not None


def check_scheduled(op: Operation) -> None:
    if not scheduled(op):
        raise FloorError(UNSCHEDULED_MSG.format(name=op.name, order=op.order.number))


def op_json(op: Operation, refs: dict | None = None) -> dict:
    o = op.order
    extra = (refs or {}).get(op.pk) or {}
    return {**extra, "scheduled": scheduled(op),
        "id": op.pk, "name": op.name, "seq": op.sequence, "order": o.number,
        "product": f"{o.product.sku} · {o.product.name}", "unit": o.product.get_unit_display(),
        "customer": o.customer.name if o.customer_id else "",
        "line": op.work_center.name if op.work_center_id else "", "line_id": op.work_center_id,
        "qty": num(op.qty), "done": num(op.qty_good), "pending": num(op.qty_pending), "scrap": num(op.qty_scrap),
        "mpu": float(op.minutes_per_unit), "crew": op.crew, "status": op.status,
        "start": iso(op.planned_start), "end": iso(op.planned_end),
        "due": iso(o.due_date) if getattr(o, "due_date", None) else None,
    }


def stop_json(st: Stop | None) -> dict | None:
    if st is None:
        return None
    return {"id": st.pk, "cause": st.cause, "start": iso(st.start), "end": iso(st.end), "is_break": st.is_break,
            "planned_end": iso(st.planned_end), "employee": st.employee.display_name if st.employee_id else "",
            "employee_id": st.employee_id}


def _ops_qs():
    return (Operation.objects.filter(order__status__in=OPEN_ORDER).exclude(status=Operation.Status.DONE)
            .select_related("order__product", "order__customer", "order__site", "work_center", "stage")
            .order_by(F("planned_start").asc(nulls_last=True), "order_id", "sequence"))


# ---------------------------------------------------------------- qué le toca a cada quien
def employee_tasks(emp: Employee) -> list[Operation]:
    """Operaciones a las que el supervisor asignó al operario (pedidos liberados a planta, sin terminar), en orden."""
    return list(_ops_qs().filter(crew_assignments__employee=emp).distinct())


def current_task(emp: Employee, tasks: list[Operation]) -> Operation | None:
    """La última en la que marcó (si sigue abierta); si no, la primera de su cola. Solo entre las programadas: lo que
    no está en el programador se ve en la cola, bloqueado."""
    tasks = [t for t in tasks if scheduled(t)]
    if not tasks:
        return None
    last = (Mark.objects.filter(employee=emp, operation__in=[t.pk for t in tasks])
            .order_by("-at").values_list("operation_id", flat=True).first())
    return next((t for t in tasks if t.pk == last), tasks[0])


def line_tasks(wc: WorkCenter) -> list[Operation]:
    return list(_ops_qs().filter(work_center=wc).prefetch_related("crew_assignments__employee"))


def line_current(wc: WorkCenter, tasks: list[Operation]) -> Operation | None:
    tasks = [t for t in tasks if scheduled(t)]  # solo lo programado (ver current_task)
    if not tasks:
        return None
    last = (Mark.objects.filter(work_center=wc, operation__in=[t.pk for t in tasks])
            .order_by("-at").values_list("operation_id", flat=True).first())
    return (next((t for t in tasks if t.pk == last), None)
            or next((t for t in tasks if t.status == Operation.Status.IN_PROGRESS), tasks[0]))


def line_crew(tasks: list[Operation]) -> list[Employee]:
    seen, out = set(), []
    for op in tasks:
        # .all() aprovecha el prefetch si viene (una sola consulta para toda la línea)
        for ca in sorted(op.crew_assignments.all(), key=lambda ca: ca.employee.full_name):
            if ca.employee_id not in seen:
                seen.add(ca.employee_id)
                out.append(ca.employee)
    return out


def employee_shift(emp: Employee, day=None):
    day = day or timezone.localdate()
    att = Attendance.objects.filter(employee=emp, day=day).select_related("shift", "shift__site").first()
    return (att.shift if att and att.shift_id else None) or emp.shift


# ---------------------------------------------------------------- marcaciones → reportes del MES
def open_stop(employee=None, work_center=None) -> Stop | None:
    if employee is not None:
        return Stop.objects.filter(employee=employee, end__isnull=True).select_related("employee").first()
    return Stop.objects.filter(work_center=work_center, employee__isnull=True, end__isnull=True).first()


def tap_times(raw, qty: Decimal, now=None) -> list[datetime] | None:
    """Horas de cada toque cuando la tablet manda varios juntos (ms desde 1970, hora del servidor). Se aceptan solo si
    hay una por unidad y son de los últimos 10 minutos; si no, la marcación queda con la hora de llegada."""
    now = now or timezone.now()
    if not isinstance(raw, list) or not raw or qty != len(raw) or len(raw) > 500:
        return None
    out = []
    for v in raw:
        try:
            t = datetime.fromtimestamp(float(v) / 1000, tz=timezone.get_current_timezone())
        except (TypeError, ValueError, OverflowError, OSError):
            return None
        out.append(min(now, max(now - timedelta(minutes=10), t)))
    return sorted(out)


def add_mark(*, operation: Operation, kind: str = Mark.Kind.GOOD, qty=1, employee: Employee | None = None,
             work_center: WorkCenter | None = None, times=None, ean: str = "") -> tuple[Mark, bool]:
    """Registra una marcación. Las buenas y los rechazos entran al MES como reporte de producción en el mismo
    instante; al completar lo programado la operación se cierra sola y el operario pasa a la siguiente de su cola.

    `times`: la tablet agrupa los toques rápidos en un solo envío y manda la hora de cada uno (así el tiempo de ciclo
    sigue siendo el real). `ean`: la referencia que se está trabajando (si el pedido tiene EAN)."""
    from .live import quiet

    from mes_core.dbqueue import write_turn

    token = quiet.set(True)
    try:
        with write_turn():  # turno de escritura en orden de llegada (SQLite): sin esperas largas con muchas tablets
            return _add_mark(operation=operation, kind=kind, qty=qty, employee=employee, work_center=work_center,
                             times=times, ean=ean)
    finally:
        quiet.reset(token)


@transaction.atomic
def _add_mark(*, operation, kind, qty, employee, work_center, times, ean=""):
    op = Operation.objects.select_for_update().select_related("order", "work_center").get(pk=operation.pk)
    done_msg = DONE_MSG if employee else DONE_LINE_MSG
    if kind not in Mark.Kind.values:
        raise FloorError("Tipo de marcación no válido.")
    if op.order.status not in OPEN_ORDER:
        raise FloorError(f"El pedido {op.order.number} no está liberado a planta.")
    if op.status == Operation.Status.DONE:
        raise FloorError(done_msg, done=True)
    check_scheduled(op)
    qty = parse_qty(qty)
    wc = work_center or op.work_center
    finished = False
    report = None
    try:
        if kind == Mark.Kind.GOOD:
            if open_stop(employee=employee) if employee else open_stop(work_center=wc):
                raise FloorError("Hay un paro abierto: reanuda para marcar unidades.")
            pending = op.qty_pending
            if pending <= 0:  # ya estaba completa pero sin cerrar
                services.report_production(op, finished=True, employee=employee, source="piso",
                                           comment="Cierre automático desde la tablet de piso")
                bump_employee(employee.pk if employee else None, wc.pk if wc else None)
                raise FloorError(done_msg, done=True)
            if qty > pending:
                raise FloorError(f"Solo faltan {num(pending)} unidades de «{op.name}».")
            finished = qty >= pending
            report = services.report_production(op, qty_good=qty, finished=finished, employee=employee, source="piso",
                                                merge_minutes=MERGE_MINUTES, comment="Tablet de piso", ean=ean)
        elif kind == Mark.Kind.SCRAP:
            report = services.report_production(op, qty_scrap=qty, employee=employee, source="piso",
                                                merge_minutes=MERGE_MINUTES, comment="Tablet de piso", ean=ean)
    except ValidationError as exc:
        raise FloorError(" ".join(exc.messages)) from None
    stamps = tap_times(times, qty)
    common = dict(operation=op, work_center=wc, employee=employee, kind=kind, minutes_per_unit=op.minutes_per_unit,
                  report=report)
    if stamps:  # una marcación por toque, con su hora
        Mark.objects.bulk_create([Mark(qty=1, at=t, **common) for t in stamps])
        mark = Mark.objects.filter(operation=op, employee=employee, kind=kind).order_by("-at", "-pk").first()
    else:
        mark = Mark.objects.create(qty=qty, **common)
    if finished:  # meta cumplida: se cierran sus paros abiertos (no las pausas)
        stops = Stop.objects.filter(end__isnull=True, is_break=False)
        stops = stops.filter(employee=employee) if employee else stops.filter(work_center=wc, employee__isnull=True)
        stops.update(end=timezone.now())
    bump_employee(employee.pk if employee else None, wc.pk if wc else None)
    if finished:
        bump("g")
    return mark, finished


# ---------------------------------------------------------------- paros y pausas
def start_stop(*, cause: str, employee: Employee | None = None, work_center: WorkCenter | None = None,
               operation: Operation | None = None, is_break: bool = False, planned_end=None) -> Stop:
    cause = " ".join(str(cause or "").split())[:60]
    if not cause:
        raise FloorError("Elige el motivo del paro.")
    if employee is not None:
        current = open_stop(employee=employee)
        if current:
            raise FloorError(f"{employee.display_name} ya tiene un paro abierto ({current.cause}).")
    elif open_stop(work_center=work_center):
        raise FloorError("La línea ya tiene un paro abierto.")
    st = Stop.objects.create(cause=cause, employee=employee, work_center=work_center or (
        operation.work_center if operation else None), operation=operation, is_break=is_break, planned_end=planned_end)
    bump_employee(employee.pk if employee else None, st.work_center_id)
    return st


def close_stop(stop: Stop, *, user=None, force: bool = False) -> None:
    if stop.end:
        return
    if stop.is_break and not force:
        raise FloorError(f"Es la pausa de {stop.cause.lower()}: se retoma escaneando el QR de la línea.")
    stop.end = timezone.now()
    stop.closed_by = user if getattr(user, "is_authenticated", False) else None
    stop.save(update_fields=["end", "closed_by"])
    bump_employee(stop.employee_id, stop.work_center_id)


def _at_shift(shift, day, t: time) -> datetime:
    from mes_core.scheduling import shift_window

    a, _ = shift_window(shift, day)
    dt = a.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return dt + timedelta(days=1) if dt < a else dt


def break_windows(shift, now=None) -> dict[str, list[tuple[datetime, datetime]]]:
    """Horarios de desayuno y almuerzo del turno en curso (con grupos escalonados, uno por grupo)."""
    from mes_core.staffing import shift_day_of

    if shift is None:
        return {}
    match, day = shift_day_of(shift.site, now or timezone.now(), [shift])
    if match is None:
        return {}
    out = {}
    for kind, groups in (("Desayuno", shift.breakfast_groups), ("Almuerzo", shift.lunch_groups)):
        wins = []
        for a, b in groups:
            s = _at_shift(shift, day, a)
            e = s + timedelta(minutes=shift.span_minutes(a, b))
            wins.append((s, e))
        if wins:
            out[kind] = wins
    return out


def _break_taken(emp, kind, since) -> bool:
    return Stop.objects.filter(employee=emp, is_break=True, cause=kind, start__gte=since).exists()


def auto_break(emp: Employee, shift, now=None) -> None:
    """Con un solo horario de desayuno / almuerzo, la pausa arranca sola al llegar la hora."""
    now = now or timezone.now()
    for kind, wins in break_windows(shift, now).items():
        if len(wins) != 1:
            continue  # grupos escalonados: el operario inicia su pausa
        a, b = wins[0]
        if a <= now < b and not _break_taken(emp, kind, a - timedelta(hours=2)):
            Stop.objects.filter(employee=emp, end__isnull=True).update(end=now)
            Stop.objects.create(employee=emp, cause=kind, is_break=True, planned_end=b, start=now)
            bump_employee(emp.pk)


def start_break(emp: Employee, kind: str, operation=None, now=None) -> Stop:
    now = now or timezone.now()
    if kind not in BREAKS:
        raise FloorError("Pausa no válida.")
    shift = employee_shift(emp, timezone.localdate(now))
    wins = break_windows(shift, now).get(kind, [])
    since = min(a for a, _ in wins) - timedelta(hours=2) if wins else now - timedelta(hours=8)
    if _break_taken(emp, kind, since):
        raise FloorError(f"Ya tomaste el {kind.lower()} de este turno.")
    inside = next(((a, b) for a, b in wins if a <= now < b), None)
    if inside:
        end = inside[1]
    elif wins:
        end = now + (wins[0][1] - wins[0][0])
    else:
        end = now + timedelta(minutes=15 if kind == "Desayuno" else 45)
    Stop.objects.filter(employee=emp, end__isnull=True).update(end=now)
    return Stop.objects.create(employee=emp, cause=kind, is_break=True, planned_end=end, start=now, operation=operation,
                               work_center=operation.work_center if operation else None)


def resume_break(emp: Employee, code: str, site=None) -> bool:
    """El operario escanea el QR de su línea (cualquier línea de la planta) para volver de la pausa."""
    st = Stop.objects.filter(employee=emp, end__isnull=True, is_break=True).first()
    if st is None:
        return True
    key = f"piso:qr:{emp.pk}"
    tries = cache.get(key, 0)
    if tries >= 5:
        raise FloorError("Demasiados intentos. Espera un minuto.")
    code = "".join(str(code or "").split()).upper()
    codes = LineCode.objects.filter(pause_code=code)
    if site is not None:
        codes = codes.filter(work_center__site=site)
    if not code or not codes.exists():
        cache.set(key, tries + 1, 60)
        raise FloorError("Código incorrecto. Escanea el QR pegado en la línea.")
    cache.delete(key)
    st.end = timezone.now()
    st.save(update_fields=["end"])
    bump_employee(emp.pk, st.work_center_id)
    return True


# ---------------------------------------------------------------- mensajes
def send_message(*, text: str, employees, user, audience: str = "", work_center: WorkCenter | None = None,
                 sender: Employee | None = None, alarm: bool = True) -> Message:
    text = " ".join(str(text or "").split())[:500]
    employees = list({e.pk: e for e in employees}.values())
    if not text and alarm:  # «solo zumbido»: llamar la atención sin escribir nada
        text = "🔔 ¡Atención! Mira la pantalla."
    if not text:
        raise FloorError("Escribe el mensaje.")
    if not employees and work_center is None:
        raise FloorError("No hay a quién enviarlo.")
    msg = Message.objects.create(text=text, sent_by=user if getattr(user, "is_authenticated", False) else None,
                                 sent_by_employee=sender, audience=audience[:120], work_center=work_center,
                                 alarm=bool(alarm))
    MessageRecipient.objects.bulk_create([MessageRecipient(message=msg, employee=e) for e in employees])
    bump(*[f"e{e.pk}" for e in employees[:500]], f"l{work_center.pk}" if work_center else "", "b")
    return msg


def unread_messages(emp: Employee, now=None) -> list[dict]:
    since = (now or timezone.now()) - timedelta(days=2)
    rows = (MessageRecipient.objects.filter(employee=emp, read_at__isnull=True, message__created_at__gte=since)
            .select_related("message__sent_by", "message__sent_by_employee").order_by("message__created_at"))
    return [_msg_json(r.message) for r in rows]


def _msg_json(m: Message) -> dict:
    who = ((m.sent_by.get_full_name() or m.sent_by.get_username()) if m.sent_by_id else
           f"{m.sent_by_employee.display_name} (supernumerario)" if m.sent_by_employee_id else "Producción")
    return {"id": m.pk, "text": m.text, "at": iso(m.created_at), "from": who, "alarm": m.alarm}


def mark_read(emp: Employee, message_id) -> None:
    MessageRecipient.objects.filter(employee=emp, message_id=message_id, read_at__isnull=True).update(
        read_at=timezone.now())


# ---------------------------------------------------------------- indicadores
def _stop_minutes(stops, since, until, weight=lambda st: 1) -> float:
    return sum(st.minutes(since, until) * weight(st) for st in stops
               if st.start < until and (st.end is None or st.end > since))


def _from(m) -> datetime:
    """Desde cuándo cuenta una marcación: la hora del toque, o el inicio del tramo si vino de un reporte por intervalo."""
    return m.since or m.at


def efficiency(marks, stops, since, until, people: int = 1) -> float | None:
    """Minutos estándar producidos ÷ minutos disponibles (desde la primera marcación, sin paros) × 100."""
    window = [m for m in marks if since <= m.at < until]
    if not window:
        return None
    s = max(since, min(_from(m) for m in window))
    people = max(1, people)
    avail = people * (until - s).total_seconds() / 60 - _stop_minutes(
        stops, s, until, lambda st: people if st.employee_id is None else 1)
    if avail < 1:
        return None
    return sum(m.std_minutes for m in window) / avail * 100


def month_efficiency(marks, stops, now) -> float | None:
    """Por cada día con marcaciones: minutos estándar ÷ minutos disponibles (primera → última marcación, sin paros)."""
    days = defaultdict(list)
    for m in marks:
        days[timezone.localtime(m.at).date()].append(m)
    today = timezone.localdate(now)
    std = avail = 0.0
    for d, ms in days.items():
        s = min(_from(m) for m in ms)
        e = now if d == today else max(m.at for m in ms)
        a = (e - s).total_seconds() / 60 - _stop_minutes(stops, s, e)
        if a >= 1:
            avail += a
            std += sum(m.std_minutes for m in ms)
    return std / avail * 100 if avail else None


def _sum(marks, kind, since=None) -> float:
    return num(sum((m.qty for m in marks if m.kind == kind and (since is None or m.at >= since)), Decimal(0))) or 0


def _cycle_seconds(marks) -> float | None:
    good = sorted((m.at for m in marks if m.kind == Mark.Kind.GOOD))
    return (good[-1] - good[-2]).total_seconds() if len(good) > 1 else None


def oee(eff, avail_pct, good, scrap) -> float | None:
    if eff is None or avail_pct is None:
        return None
    quality = (good / (good + scrap)) if (good + scrap) else 1
    return avail_pct * min(eff, 100) / 100 * quality


# ---------------------------------------------------------------- estado de cada pantalla
def operator_state(emp: Employee, now=None) -> dict:
    now = now or timezone.now()
    shift = employee_shift(emp, timezone.localdate(now))
    auto_break(emp, shift, now)
    tasks = employee_tasks(emp)
    cur = current_task(emp, tasks)
    t0, m0, h1 = day_start(now), month_start(now), now - timedelta(hours=1)
    marks = list(Mark.objects.filter(employee=emp, at__gte=m0).only("at", "since", "kind", "qty", "minutes_per_unit",
                                                                    "operation_id", "employee_id"))
    today = [m for m in marks if m.at >= t0]
    stops = list(Stop.objects.filter(Q(employee=emp) | Q(employee__isnull=True, work_center=cur.work_center_id if cur
                                                       else None), start__gte=m0 - timedelta(days=1))
                 .select_related("employee"))
    st = next((s for s in stops if s.employee_id == emp.pk and s.end is None), None)
    line_st = next((s for s in stops if s.employee_id is None and s.end is None), None)
    e_hour, e_day, e_month = efficiency(marks, stops, h1, now), efficiency(marks, stops, t0, now), \
        month_efficiency(marks, stops, now)
    std_hour = sum(m.std_minutes for m in marks if m.at >= h1)
    std_day = sum(m.std_minutes for m in today)
    std_month = sum(m.std_minutes for m in marks)
    mine_cur = _sum([m for m in marks if cur and m.operation_id == cur.pk], Mark.Kind.GOOD)
    finished_ids = {m.operation_id for m in today}
    done_today = [
        {"name": op.name, "order": op.order.number, "qty": num(op.qty_good), "goal": num(op.qty),
         "mine": _sum([m for m in today if m.operation_id == op.pk], Mark.Kind.GOOD), "end": iso(op.actual_end)}
        for op in Operation.objects.filter(pk__in=finished_ids, status=Operation.Status.DONE)
        .select_related("order").order_by("-actual_end")
    ]
    wins = break_windows(shift, now)
    taken = {s.cause for s in stops if s.is_break and s.start >= t0}
    good_today = _sum(today, Mark.Kind.GOOD)
    report = _report_info(cur, now, employee=emp) if cur else None
    return {
        "kind": "operario", "now": iso(now), "pts_per_min": pts_per_minute(), "report": report,
        "who": {"id": emp.pk, "name": emp.display_name, "doc": emp.document[-4:],
                "position": emp.position.capitalize() if emp.position else "",
                "shift": f"{shift.name} {shift.start_time:%H:%M}–{shift.end_time:%H:%M}" if shift else ""},
        "task": op_json(cur, ref_info([cur])) if cur else None,
        "queue": [op_json(t) for t in tasks if not cur or t.pk != cur.pk],  # incluye lo no programado (bloqueado)
        "mine": {
            "task_good": mine_cur, "good_hour": _sum(marks, Mark.Kind.GOOD, h1), "good_today": good_today,
            "scrap_today": _sum(today, Mark.Kind.SCRAP), "rework_today": _sum(today, Mark.Kind.REWORK),
            "good_month": _sum(marks, Mark.Kind.GOOD),
            "eff_hour": e_hour, "eff_day": e_day, "eff_month": e_month,
            "points_hour": std_hour * pts_per_minute(), "points_day": std_day * pts_per_minute(),
            "points_month": std_month * pts_per_minute(), "money_month": std_month * pts_per_minute() * point_value(),
            "last_mark": iso(max((m.at for m in today if m.kind == Mark.Kind.GOOD), default=None)),
            "cycle_s": _cycle_seconds(today),
            "stop_min_today": _stop_minutes([s for s in stops if s.employee_id == emp.pk], t0, now),
        },
        "stop": stop_json(st), "line_stop": stop_json(line_st),
        "stops_today": [stop_json(s) for s in sorted(stops, key=lambda s: s.start, reverse=True)
                        if s.employee_id == emp.pk and (s.end is None or s.end >= t0)][:20],
        "done_today": done_today,
        "messages": unread_messages(emp, now),
        "causes": list(STOP_CAUSES),
        "breaks": [{"kind": k, "taken": k in taken,
                    "windows": [[iso(a), iso(b)] for a, b in wins.get(k, [])]} for k in BREAKS],
    }


def _report_info(op: Operation, now, employee: Employee | None = None, panel: dict | None = None,
                 modes=None) -> dict:
    """Cómo se reporta la operación en curso (contador, intervalo o supernumerario), su próximo corte y la parte de la
    carga que le toca a cada persona de la etapa. ``panel``: el de la línea ya armado (para no repetir consultas)."""
    from . import reporting

    mi = (modes or reporting.Modes()).of(op)
    n = len(op.crew_assignments.all())  # con prefetch no consulta
    out = {**mi, "people": n, "share": reporting.workload_share(op, n)}
    if mi["mode"] == reporting.INTERVAL and employee is not None:
        out["period"] = reporting.period_json(reporting.period(op, interval=mi["interval"], employee=employee, now=now))
        out["reports"] = reporting.recent_reports(employee=employee, now=now, limit=12)
    elif mi["mode"] != reporting.COUNTER:
        same = next((o for o in (panel or {}).get("ops", []) if o["id"] == op.pk), None)
        out["period"] = same["period"] if same else reporting.period_json(
            reporting.period(op, interval=mi["interval"], work_center=op.work_center, now=now))
    return out


def line_state(wc: WorkCenter, now=None) -> dict:
    from . import reporting

    now = now or timezone.now()
    tasks = line_tasks(wc)
    cur = line_current(wc, tasks)
    crew = line_crew([cur] if cur else tasks[:1]) or line_crew(tasks)
    crew_ids = [e.pk for e in crew]
    t0, h1, m0 = day_start(now), now - timedelta(hours=1), month_start(now)
    marks = list(Mark.objects.filter(work_center=wc, at__gte=m0).only("at", "since", "kind", "qty", "minutes_per_unit",
                                                                       "operation_id", "employee_id"))
    today = [m for m in marks if m.at >= t0]
    stops = list(Stop.objects.filter(Q(work_center=wc) | Q(employee_id__in=crew_ids), start__gte=t0 - timedelta(days=1))
                 .select_related("employee"))
    people = max(1, len(crew))
    e_hour = efficiency(today, stops, h1, now, people)
    e_day = efficiency(today, stops, t0, now, people)
    open_by_emp = {s.employee_id: s for s in stops if s.employee_id and s.end is None}
    units_by_emp = defaultdict(Decimal)
    for m in today:
        if m.employee_id and m.kind == Mark.Kind.GOOD:
            units_by_emp[m.employee_id] += m.qty
    line_marks_cur = [m for m in marks if cur and m.operation_id == cur.pk and m.employee_id is None]
    modes = reporting.Modes()
    panel = None
    if cur and modes.of(cur)["mode"] != reporting.COUNTER:  # línea continua: la tablet reporta por tramos
        panel = reporting.line_panel(wc, now, modes, tasks=tasks)
        panel["reports"] = reporting.recent_reports(work_center=wc, now=now)
    report = _report_info(cur, now, panel=panel, modes=modes) if cur else None
    return {
        "kind": "linea", "now": iso(now), "report": report, "panel": panel,
        "who": {"id": wc.pk, "name": wc.name, "code": wc.code, "site": wc.site.name},
        "task": op_json(cur, ref_info([cur])) if cur else None,
        "queue": [op_json(t) for t in tasks if not cur or t.pk != cur.pk],  # incluye lo no programado (bloqueado)
        "crew": [{"id": e.pk, "name": e.display_name, "stop": stop_json(open_by_emp.get(e.pk)),
                  "units": num(units_by_emp.get(e.pk, 0))} for e in crew],
        "kpis": {
            "good_hour": _sum(today, Mark.Kind.GOOD, h1), "good_today": _sum(today, Mark.Kind.GOOD),
            "scrap_today": _sum(today, Mark.Kind.SCRAP), "rework_today": _sum(today, Mark.Kind.REWORK),
            "good_month": _sum(marks, Mark.Kind.GOOD), "line_task_good": _sum(line_marks_cur, Mark.Kind.GOOD),
            "eff_hour": e_hour, "eff_day": e_day, "people": people,
            "stop_min_today": _stop_minutes([s for s in stops if s.employee_id], t0, now),
            "last_mark": iso(max((m.at for m in today if m.kind == Mark.Kind.GOOD), default=None)),
            "cycle_s": _cycle_seconds(today),
        },
        "line_stop": stop_json(next((s for s in stops if s.employee_id is None and s.end is None), None)),
        "stops_today": [stop_json(s) for s in sorted(stops, key=lambda s: s.start, reverse=True)
                        if not s.is_break and (s.end is None or s.end >= t0)][:40],
        "messages": [_msg_json(m) for m in Message.objects.filter(work_center=wc, created_at__gte=t0)
                     .select_related("sent_by", "sent_by_employee").order_by("-created_at")[:3]],
        "causes": list(STOP_CAUSES),
    }


# ---------------------------------------------------------------- tablero en vivo (supervisores)
def sees_all(user) -> bool:
    return user.is_superuser or user.has_perm("mes_core.assign_supervisor")


def board(user, now=None) -> dict:
    """Tablero por operario para el supervisor (solo sus operaciones; jefe de planta y planeación ven todo)."""
    now = now or timezone.now()
    ops = _ops_qs().prefetch_related("crew_assignments__employee")
    marked_today = Mark.objects.filter(at__gte=day_start(now))
    if not sees_all(user):
        ops = ops.filter(supervisor=user)
        marked_today = marked_today.filter(operation__supervisor=user)
    # quien tiene la app abierta aparece aunque hoy no esté en ninguna operación (para poder escribirle)
    online = Employee.objects.filter(floor_access__active=True, floor_access__last_seen__gte=now - timedelta(
        seconds=FloorAccess.ONLINE_SECONDS))
    data = board_data(list(ops), marked_today, now, extra=list(online))
    data["all"] = sees_all(user)
    return data


def board_data(ops: list[Operation], marked_today, now, extra=()) -> dict:
    """Indicadores por persona y por línea de un grupo de operaciones (tablero en vivo y pestaña «Personal» del
    supernumerario). ``extra``: personas que se muestran aunque hoy no estén en esas operaciones."""
    t0, h1 = day_start(now), now - timedelta(hours=1)
    emp_ops: dict[int, list[Operation]] = defaultdict(list)
    emps: dict[int, Employee] = {}
    for op in ops:
        for ca in op.crew_assignments.all():
            emp_ops[ca.employee_id].append(op)
            emps[ca.employee_id] = ca.employee
    for e in Employee.objects.filter(pk__in=marked_today.values("employee_id")).exclude(pk__in=list(emps)):
        emps[e.pk] = e
    for e in extra:
        emps.setdefault(e.pk, e)
    marks = list(Mark.objects.filter(at__gte=t0).filter(Q(employee_id__in=list(emps))
                                                       | Q(employee__isnull=True, operation__in=[o.pk for o in ops]))
                 .select_related("operation"))
    by_emp = defaultdict(list)
    for m in marks:
        by_emp[m.employee_id].append(m)
    wc_ids = {o.work_center_id for o in ops if o.work_center_id}
    stops = list(Stop.objects.filter(Q(employee_id__in=list(emps)) | Q(employee__isnull=True, work_center_id__in=wc_ids),
                                     start__gte=t0 - timedelta(days=1)).select_related("employee", "work_center"))
    stops_by_emp = defaultdict(list)
    for s in stops:
        stops_by_emp[s.employee_id].append(s)
    cards = []
    tot_std = tot_avail = 0.0
    from .reporting import COUNTER, Modes

    modes = Modes()
    for eid, e in emps.items():
        ms = by_emp.get(eid, [])
        sts = stops_by_emp.get(eid, [])
        my_ops = emp_ops.get(eid, [])
        last_op = max(ms, key=lambda m: m.at).operation_id if ms else None
        cur = next((o for o in my_ops if o.pk == last_op), my_ops[0] if my_ops else None)
        e_day = efficiency(ms, sts, t0, now)
        good = _sum(ms, Mark.Kind.GOOD)
        scrap = _sum(ms, Mark.Kind.SCRAP)
        last = max((m.at for m in ms if m.kind == Mark.Kind.GOOD), default=None)
        st = next((s for s in sts if s.end is None), None)
        if ms:
            s0 = min(_from(m) for m in ms)
            mins = (now - s0).total_seconds() / 60
            avail = mins - _stop_minutes(sts, s0, now)
            if avail >= 1:
                tot_std += sum(m.std_minutes for m in ms)
                tot_avail += avail
            avail_pct = avail / mins * 100 if mins >= 1 else None
        else:
            avail_pct = None
        mi = modes.of(cur) if cur else None
        # en líneas que reportan por tramos, «sin marcar» es cuando se pasó del corte (no a los 10 minutos)
        alert = NO_MARKS_ALERT if mi is None or mi["mode"] == COUNTER else (
            timedelta(minutes=mi["interval"]) + NO_MARKS_ALERT)
        state = ("paro" if st else "disponible" if cur is None else
                 "sin_marcar" if last and now - last > alert else "ok" if ms else "sin_iniciar")
        cards.append({
            "id": eid, "name": e.display_name, "state": state, "stop": stop_json(st),
            "task": {"id": cur.pk, "name": cur.name, "order": cur.order.number, "line": cur.work_center.name
                     if cur.work_center_id else "", "line_id": cur.work_center_id, "done": num(cur.qty_good),
                     "qty": num(cur.qty)} if cur else None,
            "queue": max(0, len(my_ops) - 1), "eff_day": e_day, "eff_hour": efficiency(ms, sts, h1, now),
            "oee": oee(e_day, avail_pct, good, scrap), "good": good, "scrap": scrap,
            "rework": _sum(ms, Mark.Kind.REWORK), "last_mark": iso(last),
        })
    order = {"paro": 0, "sin_marcar": 1, "ok": 2, "sin_iniciar": 3, "disponible": 4}
    cards.sort(key=lambda c: (order[c["state"]], c["name"]))
    # conexión a la app: última vez que su pantalla habló con el servidor
    limit = now - timedelta(seconds=FloorAccess.ONLINE_SECONDS)
    access = {a.employee_id: a for a in FloorAccess.objects.filter(employee_id__in=list(emps))}
    for c in cards:
        a = access.get(c["id"])
        seen = a.last_seen if a and a.active else None
        c["online"] = bool(seen and seen >= limit)
        c["seen"] = iso(seen)
        c["sound"] = a.sound_ok if c["online"] else None  # False = su equipo no deja sonar las alarmas solo
        c["role"] = a.get_role_display().split(" (")[0] if a else ""
    good = _sum(marks, Mark.Kind.GOOD)
    scrap = _sum(marks, Mark.Kind.SCRAP)
    open_stops = [s for s in stops if s.end is None]
    lines = []
    tablets = {lc.work_center_id: lc for lc in LineCode.objects.filter(work_center_id__in=wc_ids)}
    for wc in WorkCenter.objects.filter(pk__in=wc_ids).order_by("code"):
        lc = tablets.get(wc.pk)
        t_on = bool(lc and lc.tablet_seen and lc.tablet_seen >= limit)
        lops = [o for o in ops if o.work_center_id == wc.pk]
        cur = next((o for o in lops if o.status == Operation.Status.IN_PROGRESS), lops[0] if lops else None)
        ls = next((s for s in open_stops if s.employee_id is None and s.work_center_id == wc.pk), None)
        lines.append({"id": wc.pk, "name": wc.name, "code": wc.code, "stop": stop_json(ls),
                      "tablet": {"online": t_on, "sound": lc.tablet_sound if t_on else None},
                      "task": {"name": cur.name, "order": cur.order.number, "done": num(cur.qty_good),
                               "qty": num(cur.qty)} if cur else None,
                      "good": _sum([m for m in marks if m.operation.work_center_id == wc.pk], Mark.Kind.GOOD)})
    return {
        "now": iso(now),
        "kpis": {"good": good, "scrap": scrap, "rework": _sum(marks, Mark.Kind.REWORK),
                 "eff": tot_std / tot_avail * 100 if tot_avail else None,
                 "people": len(emps), "working": sum(1 for c in cards if c["state"] in ("ok", "sin_marcar")),
                 "online": sum(1 for c in cards if c["online"]),
                 "open_stops": len(open_stops),
                 "stop_min": _stop_minutes([s for s in stops if s.employee_id], t0, now)},
        "cards": cards, "lines": lines,
        "stops": [dict(stop_json(s), line=s.work_center.name if s.work_center_id else "") for s in
                  sorted(open_stops, key=lambda s: s.start)],
        "messages": [dict(_msg_json(m), audience=m.audience) for m in
                     Message.objects.filter(created_at__gte=t0).select_related("sent_by", "sent_by_employee").order_by("-created_at")[:5]],
    }


# ---------------------------------------------------------------- histórico
def history(date_from, date_to, work_center=None) -> dict:
    """Totales por operario y por día entre dos fechas (incluidas)."""
    tz = timezone.get_current_timezone()
    a = datetime.combine(date_from, time.min, tzinfo=tz)
    b = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=tz)
    marks = Mark.objects.filter(at__gte=a, at__lt=b).select_related("employee", "work_center")
    stops = Stop.objects.filter(start__lt=b).filter(Q(end__isnull=True) | Q(end__gt=a)).select_related("employee")
    if work_center:
        marks = marks.filter(work_center=work_center)
        stops = stops.filter(work_center=work_center)
    marks, stops = list(marks), list(stops)
    now = timezone.now()
    stops_by = defaultdict(list)
    for s in stops:
        stops_by[s.employee_id].append(s)
    groups = defaultdict(lambda: defaultdict(list))  # quién → día → marcaciones
    for m in marks:
        who = ("e", m.employee_id) if m.employee_id else ("l", m.work_center_id)
        groups[who][timezone.localtime(m.at).date()].append(m)
    rows, days = [], defaultdict(lambda: {"good": 0, "scrap": 0, "rework": 0, "std": 0.0, "avail": 0.0})
    for who, by_day in groups.items():
        sample = next(iter(by_day.values()))[0]
        name = sample.employee.display_name if who[0] == "e" else f"{sample.work_center.name if sample.work_center else 'Línea'} (tablet de línea)"
        r = {"name": name, "good": 0, "scrap": 0, "rework": 0, "std": 0.0, "avail": 0.0, "stop_min": 0.0, "days": 0}
        for d, ms in by_day.items():
            s, e = min(_from(m) for m in ms), max(m.at for m in ms)
            if d == timezone.localdate(now) and e < now:
                e = now
            sm = _stop_minutes(stops_by.get(who[1], []) if who[0] == "e" else [], s, e)
            avail = (e - s).total_seconds() / 60 - sm
            g, x, w = _sum(ms, Mark.Kind.GOOD), _sum(ms, Mark.Kind.SCRAP), _sum(ms, Mark.Kind.REWORK)
            std = sum(m.std_minutes for m in ms)
            r["good"] += g; r["scrap"] += x; r["rework"] += w; r["stop_min"] += sm; r["days"] += 1  # noqa: E702
            day = days[d]
            day["good"] += g; day["scrap"] += x; day["rework"] += w  # noqa: E702
            if who[0] == "e" and avail >= 1:
                r["std"] += std; r["avail"] += avail  # noqa: E702
                day["std"] += std; day["avail"] += avail  # noqa: E702
        r["eff"] = r["std"] / r["avail"] * 100 if r["avail"] else None
        r["points"] = r["std"] * pts_per_minute()
        rows.append(r)
    rows.sort(key=lambda r: -r["good"])
    day_rows = [dict(v, day=d, eff=v["std"] / v["avail"] * 100 if v["avail"] else None)
                for d, v in sorted(days.items(), reverse=True)]
    tot_std, tot_avail = sum(r["std"] for r in rows), sum(r["avail"] for r in rows)
    return {
        "rows": rows, "days": day_rows,
        "total": {"good": sum(r["good"] for r in rows), "scrap": sum(r["scrap"] for r in rows),
                  "rework": sum(r["rework"] for r in rows), "eff": tot_std / tot_avail * 100 if tot_avail else None,
                  "stop_min": sum(s.minutes(a, min(b, now)) for s in stops if s.employee_id)},
        "stops": sorted(stops, key=lambda s: s.start, reverse=True)[:200],
    }
