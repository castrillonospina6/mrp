"""Formas de reportar la producción desde la planta.

* Contador: módulos y mesas. El operario toca la pantalla por cada unidad (lo de siempre, en ``floor.add_mark``).
* Intervalo: líneas continuas (banda, Vélez…). Nadie puede parar a tocar la pantalla por cada unidad: cada operario
  ve una cuenta regresiva y al llegar a cero escribe cuánto hizo en ese tramo.
* Supernumerario: el asistente del supervisor escribe cada tramo el total de la línea y el sistema lo reparte entre
  el personal de la etapa (según el tiempo trabajado o en partes iguales, lo elige planeación), o lo escribe por
  persona. Lo puede hacer en cualquier línea a su cargo, sea cual sea su forma de reportar.

Qué forma aplica a una operación: la que planeación eligió en la etapa del pedido; si no, la del tipo de proceso
(nombre de la etapa); si no, la de la línea; si no, contador. El tramo de cada reporte va desde el reporte anterior
(o el inicio del turno) hasta el momento en que se envía; la cuenta regresiva apunta al siguiente corte del turno
(inicio del turno + N × el intervalo).
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Min, Q, Sum
from django.utils import timezone

from mes_core import services
from mes_core.models import Attendance, CrewAssignment, Employee, Operation, PlanStage, WorkCenter

from . import floor
from .floor import FloorError, day_start, iso, num
from .live import bump, quiet
from .models import IntervalReport, LineCode, Mark, ProcessReporting, Stop, SupernumeraryLine

COUNTER = PlanStage.ReportMode.COUNTER
INTERVAL = PlanStage.ReportMode.INTERVAL
SUPER = PlanStage.ReportMode.SUPERNUMERARY
TIME, EQUAL = PlanStage.SplitMode.TIME, PlanStage.SplitMode.EQUAL
DEFAULT_INTERVAL = 60
EARLY_GRACE = timedelta(minutes=5)   # quien reporta poco antes del corte no queda debiendo ese corte
UNDO_WINDOW = timedelta(minutes=30)  # se puede anular un reporte propio durante este tiempo


def stage_name(op: Operation) -> str:
    return op.stage.name if op.stage_id and op.stage else op.name.split(" · ")[0]


class Modes:
    """Forma de reportar de cada operación (con caché para una sola petición)."""

    def __init__(self):
        self._proc = None
        self._lines: dict[int, LineCode | None] = {}

    def _process(self, op) -> ProcessReporting | None:
        if self._proc is None:
            self._proc = {p.key: p for p in ProcessReporting.objects.all()}
        return self._proc.get(ProcessReporting.key_of(stage_name(op)))

    def _line(self, wc_id) -> LineCode | None:
        if wc_id not in self._lines:
            self._lines[wc_id] = LineCode.objects.filter(work_center_id=wc_id).first()
        return self._lines[wc_id]

    def of(self, op: Operation) -> dict:
        st = op.stage if op.stage_id else None
        pr = self._process(op)
        lc = self._line(op.work_center_id) if op.work_center_id else None
        if st and st.report_mode:
            mode, src = st.report_mode, "la etapa del pedido"
        elif pr:
            mode, src = pr.report_mode, f"el proceso «{pr.name}»"
        elif lc and lc.report_mode:
            mode, src = lc.report_mode, "la línea"
        else:
            mode, src = COUNTER, "por defecto"
        interval = (pr.interval_minutes if pr and pr.interval_minutes else None) or (
            lc.interval_minutes if lc else None) or DEFAULT_INTERVAL
        return {"mode": mode, "source": src, "interval": int(interval),
                "split": (st.split_mode if st else "") or TIME}


def mode_of(op: Operation) -> dict:
    return Modes().of(op)


# ---------------------------------------------------------------- tramos y cuenta regresiva
def _shift_base(op: Operation, employee: Employee | None, now):
    """Inicio del turno en curso (el del operario, o el de la línea)."""
    from mes_core.scheduling import center_shifts, shift_window
    from mes_core.staffing import shift_day_of

    shifts = None
    if employee is not None:
        sh = floor.employee_shift(employee, timezone.localdate(now))
        shifts = [sh] if sh else None
    if shifts is None and op.work_center_id:
        shifts = list(center_shifts(op.work_center))
    if not shifts:
        return None
    sh, day = shift_day_of(op.order.site, now, shifts)
    return shift_window(sh, day)[0] if sh else None


def period(op: Operation, *, interval: int, employee: Employee | None = None, work_center: WorkCenter | None = None,
           now=None) -> dict:
    """Tramo que toca reportar: desde el último reporte de hoy (o el inicio del turno) y el próximo corte."""
    now = now or timezone.now()
    t0 = day_start(now)
    qs = IntervalReport.objects.filter(period_end__gte=t0, period_end__lte=now)
    if employee is not None:
        qs = qs.filter(employee=employee, kind=IntervalReport.Kind.OWN)
    else:
        qs = qs.filter(work_center=work_center).exclude(kind=IntervalReport.Kind.OWN)
    qs = qs.order_by("-period_end").values_list("period_end", flat=True)
    # el tramo de una operación sigue desde su último reporte; si aún no tiene, desde el último de la línea / persona
    last = qs.filter(operation=op).first() or qs.first()
    base = _shift_base(op, employee, now)
    if base is None or base > now or base < t0 - timedelta(hours=12):
        base = t0
    # si la gente se puso hoy en la operación (empezó a mitad de turno), el primer tramo arranca ahí
    ca = CrewAssignment.objects.filter(operation=op, assigned_at__gte=t0, assigned_at__lte=now)
    if employee is not None:
        ca = ca.filter(employee=employee)
    assigned = ca.aggregate(m=Min("assigned_at"))["m"]
    return _period(last, base, assigned, interval)


def _period(last, base, assigned, interval: int) -> dict:
    """El tramo arranca en lo más reciente entre el último reporte, el inicio del turno y la hora en que se armó la
    cuadrilla; el corte es el siguiente «inicio del turno + N × intervalo»."""
    start = max(x for x in (last, base, assigned) if x)
    step = timedelta(minutes=max(5, interval))
    n = max(1, math.floor((start + EARLY_GRACE - base) / step) + 1)
    return {"start": start, "due": base + n * step, "last": last, "interval": interval}


def period_json(p: dict) -> dict:
    return {"start": iso(p["start"]), "due": iso(p["due"]), "last": iso(p["last"]), "interval": p["interval"]}


# ---------------------------------------------------------------- reparto
def crew_of(op: Operation) -> list[Employee]:
    return [ca.employee for ca in CrewAssignment.objects.filter(operation=op).select_related("employee")
            .order_by("employee__full_name")]


def worked_minutes(employees, work_center, start, end) -> dict[int, float]:
    """Minutos que trabajó cada uno en el tramo: el tramo menos sus paros y pausas y los de toda la línea. Quien quedó
    ausente en la asistencia de ese día no trabajó."""
    ids = [e.pk for e in employees]
    stops = list(Stop.objects.filter(Q(employee_id__in=ids) | Q(employee__isnull=True, work_center=work_center),
                                     start__lt=end).filter(Q(end__isnull=True) | Q(end__gt=start)))
    absent = set(Attendance.objects.filter(employee_id__in=ids, day=timezone.localdate(end))
                 .exclude(status__in=Attendance.WORKING).values_list("employee_id", flat=True))
    return _worked(ids, stops, absent, start, end)


def _worked(ids, stops, absent, start, end) -> dict[int, float]:
    """Igual que ``worked_minutes`` con los paros y ausencias ya leídos (para armar varias operaciones a la vez)."""
    total = max(0.0, (end - start).total_seconds() / 60)
    line_min = sum(s.minutes(start, end) for s in stops if s.employee_id is None)
    own = defaultdict(float)
    for s in stops:
        if s.employee_id in ids:
            own[s.employee_id] += s.minutes(start, end)
    return {i: 0.0 if i in absent else max(0.0, total - line_min - own[i]) for i in ids}


def split_quantity(total: Decimal, weights: dict[int, float]) -> dict[int, Decimal]:
    """Reparte en proporción a los pesos, sin perder unidades (enteras si el total es entero)."""
    total = Decimal(total)
    if not weights:
        return {}
    w = {i: v for i, v in weights.items() if v > 0} or {i: 1.0 for i in weights}
    s = sum(w.values())
    unit = Decimal(1) if total == total.to_integral_value() else Decimal("0.001")
    raw = {i: total * Decimal(str(v)) / Decimal(str(s)) for i, v in w.items()}
    out = {i: (r / unit).to_integral_value(rounding=ROUND_DOWN) * unit for i, r in raw.items()}
    rest = int((total - sum(out.values())) / unit)
    for i in sorted(raw, key=lambda i: (raw[i] - out[i], w[i]), reverse=True)[:max(0, rest)]:
        out[i] += unit
    for i in weights:
        out.setdefault(i, Decimal(0))
    return out


def shares_for(op, employees, work_center, total, split, start, end) -> dict[int, Decimal]:
    if split == EQUAL:
        weights = {e.pk: 1.0 for e in employees}
    else:
        weights = worked_minutes(employees, work_center, start, end)
    return split_quantity(total, weights)


def workload_share(op: Operation, n: int) -> float | int | None:
    """Carga de la etapa repartida en el personal: la parte de cada uno (p. ej. 2.400 ÷ 10 = 240)."""
    return num(op.qty / n) if n else None


# ---------------------------------------------------------------- reportar un tramo
def _dec(raw, label) -> Decimal:
    if raw in (None, ""):
        return Decimal(0)
    try:
        v = Decimal(str(raw).replace(",", "."))
    except Exception:
        raise FloorError(f"{label}: cantidad no válida.") from None
    if v < 0 or v > Decimal("1000000"):
        raise FloorError(f"{label}: la cantidad no puede ser negativa.")
    return v


def report_interval(*, operation: Operation, kind: str, good=0, scrap=0, per=None, reporter: Employee | None = None,
                    work_center: WorkCenter | None = None, now=None, ean: str = "",
                    leftover=0) -> tuple[IntervalReport, bool]:
    """``ean``: la referencia del tramo; ``leftover``: códigos sobrantes (completan la referencia, no son producción
    de nadie)."""
    from mes_core.dbqueue import write_turn

    token = quiet.set(True)
    try:
        with write_turn():
            return _report_interval(operation=operation, kind=kind, good=good, scrap=scrap, per=per, reporter=reporter,
                                    work_center=work_center, now=now, ean=ean, leftover=leftover)
    finally:
        quiet.reset(token)


@transaction.atomic
def _report_interval(*, operation, kind, good, scrap, per, reporter, work_center, now, ean="", leftover=0):
    now = now or timezone.now()
    op = (Operation.objects.select_for_update().select_related("order__site", "work_center", "stage")
          .get(pk=operation.pk))
    if op.order.status not in floor.OPEN_ORDER:
        raise FloorError(f"El pedido {op.order.number} no está liberado a planta.")
    if op.status == Operation.Status.DONE:
        raise FloorError("Esta operación ya está completa.", done=True)
    floor.check_scheduled(op)  # lo que no está en el programador no se reporta
    wc = work_center or op.work_center
    mi = mode_of(op)
    crew = crew_of(op)
    if kind == IntervalReport.Kind.OWN:
        p = period(op, interval=mi["interval"], employee=reporter, now=now)
    else:
        p = period(op, interval=mi["interval"], work_center=wc, now=now)
    start = min(p["start"], now - timedelta(minutes=1))

    goods: dict[int | None, Decimal] = {}
    scraps: dict[int | None, Decimal] = {}
    if kind == IntervalReport.Kind.OWN:
        goods[reporter.pk], scraps[reporter.pk] = _dec(good, "Buenas"), _dec(scrap, "Rechazos")
    elif kind == IntervalReport.Kind.PEOPLE:
        ids = {e.pk for e in crew}
        for row in per or []:
            try:
                eid = int(row.get("id"))
            except (TypeError, ValueError, AttributeError):
                continue
            if eid not in ids:
                raise FloorError("Una de las personas no está en esta operación.")
            name = next(e.display_name for e in crew if e.pk == eid)
            goods[eid], scraps[eid] = _dec(row.get("g"), name), _dec(row.get("x"), name)
    else:
        g, x = _dec(good, "Buenas"), _dec(scrap, "Rechazos")
        if crew:
            goods = shares_for(op, crew, wc, g, mi["split"], start, now)
            scraps = shares_for(op, crew, wc, x, mi["split"], start, now) if x else {}
        else:
            goods, scraps = {None: g}, {None: x}
    total_good, total_scrap = sum(goods.values(), Decimal(0)), sum(scraps.values(), Decimal(0))
    left = _dec(leftover, "Sobrantes")
    if total_good <= 0 and total_scrap <= 0 and left <= 0:
        raise FloorError("Escribe cuántas unidades se hicieron.")
    pending = op.qty_pending
    if total_good + left > pending:
        raise FloorError(f"Solo faltan {num(pending)} unidades de «{op.name}» (estás reportando "
                         f"{num(total_good + left)}{' con sobrantes' if left else ''}).")
    finished = (total_good + left) > 0 and total_good + left >= pending
    who = reporter.display_name if reporter else f"tablet de la línea {wc.code if wc else ''}".strip()
    label = {IntervalReport.Kind.OWN: "reporte propio", IntervalReport.Kind.LINE: f"total de la línea repartido entre "
             f"{len([v for v in goods.values() if v > 0])}", IntervalReport.Kind.PEOPLE: "por persona"}[kind]
    comment = (f"Piso · {who} · {timezone.localtime(start):%H:%M}–{timezone.localtime(now):%H:%M} · {label}")[:255]
    try:
        report = services.report_production(
            op, qty_good=total_good, qty_scrap=total_scrap, finished=finished, source="piso", comment=comment,
            employee=reporter if kind == IntervalReport.Kind.OWN else None, ean=ean, qty_leftover=left)
    except ValidationError as exc:
        raise FloorError(" ".join(exc.messages)) from None
    ir = IntervalReport.objects.create(
        operation=op, work_center=wc, employee=reporter, kind=kind, split_mode=mi["split"] if kind ==
        IntervalReport.Kind.LINE else "", period_start=start, period_end=now, qty_good=total_good,
        qty_scrap=total_scrap, people=len([v for v in goods.values() if v > 0]) or 1, report=report)
    common = dict(operation=op, work_center=wc, minutes_per_unit=op.minutes_per_unit, at=now, since=start,
                  report=report, interval=ir)
    marks = [Mark(employee_id=eid, kind=Mark.Kind.GOOD, qty=q, **common) for eid, q in goods.items() if q > 0]
    marks += [Mark(employee_id=eid, kind=Mark.Kind.SCRAP, qty=q, **common) for eid, q in scraps.items() if q > 0]
    Mark.objects.bulk_create(marks)
    if finished:  # meta cumplida: se cierran los paros abiertos de la línea y su gente (no las pausas)
        Stop.objects.filter(end__isnull=True, is_break=False).filter(
            Q(work_center=wc, employee__isnull=True) | Q(employee_id__in=[e.pk for e in crew])).update(end=now)
    keys = [f"e{i}" for i in goods if i] + [f"e{reporter.pk}" if reporter else "", f"l{wc.pk}" if wc else "", "b"]
    bump(*keys[:400])
    if finished:
        bump("g")
    return ir, finished


def undo_interval(ir: IntervalReport, user=None) -> None:
    from mes_core import corrections

    if timezone.now() - ir.created_at > UNDO_WINDOW:
        raise FloorError("Ya pasó más de media hora: pídele al supervisor que lo corrija en el pedido.")
    if ir.report_id:  # queda en «Anulados» (se puede reactivar); salen el tramo y sus marcaciones
        who = ir.employee.display_name if ir.employee_id else "la tablet de la línea"
        corrections.void_report(ir.report, f"Anulado desde la tablet por {who} (dentro de los 30 minutos)", user=user)
    else:
        ir.delete()
    bump("g", "b", f"l{ir.work_center_id}" if ir.work_center_id else "")


# ---------------------------------------------------------------- supernumerario
def sup_lines(emp: Employee) -> dict[int, set[str]]:
    """Líneas del supernumerario con su origen: «mes» (se las asignó el supervisor), «tablet» (las eligió él) y
    «supervisor» (las del supervisor de su ficha, por las operaciones que tiene a cargo)."""
    tags: dict[int, set[str]] = defaultdict(set)
    for wc_id, src in SupernumeraryLine.objects.filter(employee=emp).values_list("work_center_id", "source"):
        tags[wc_id].add(src)
    if emp.supervisor_id:
        for wc_id in floor._ops_qs().filter(supervisor_id=emp.supervisor_id).values_list("work_center_id", flat=True):
            if wc_id:
                tags[wc_id].add("supervisor")
    return tags


def line_panel(wc: WorkCenter, now=None, modes: Modes | None = None, max_ops: int = 4, tasks=None) -> dict:
    """Lo que necesita quien reporta por una línea: sus operaciones abiertas, el tramo a reportar y su personal.

    Se arma con unas pocas consultas para toda la línea (no unas por operación): se refresca cada vez que alguien de
    la línea marca, así que tiene que ser rápido."""
    from mes_core.models import Attendance

    now = now or timezone.now()
    modes = modes or Modes()
    t0 = day_start(now)
    tasks = (floor.line_tasks(wc) if tasks is None else tasks)[:max_ops]
    crews = {op.pk: sorted((ca.employee for ca in op.crew_assignments.all()), key=lambda e: e.full_name)
             for op in tasks}
    assigned = {op.pk: min((ca.assigned_at for ca in op.crew_assignments.all() if t0 <= ca.assigned_at <= now),
                           default=None) for op in tasks}
    people = {e.pk for crew in crews.values() for e in crew}
    reports = list(IntervalReport.objects.filter(work_center=wc, period_end__gte=t0, period_end__lte=now)
                   .exclude(kind=IntervalReport.Kind.OWN).values_list("operation_id", "period_end", "qty_good"))
    stops = list(Stop.objects.filter(Q(employee_id__in=people) | Q(employee__isnull=True, work_center=wc),
                                     start__lt=now).filter(Q(end__isnull=True) | Q(end__gt=t0 - timedelta(hours=12)))
                 .select_related("employee"))
    absent = set(Attendance.objects.filter(employee_id__in=people, day=timezone.localdate(now))
                 .exclude(status__in=Attendance.WORKING).values_list("employee_id", flat=True)) if people else set()
    mine = defaultdict(Decimal)
    for op_id, emp_id, s in (Mark.objects.filter(operation_id__in=[op.pk for op in tasks], kind=Mark.Kind.GOOD,
                                                 employee_id__in=people)
                             .values("operation_id", "employee_id").annotate(s=Sum("qty"))
                             .values_list("operation_id", "employee_id", "s")):
        mine[(op_id, emp_id)] = s
    open_stop = {s.employee_id: s for s in stops if s.end is None and s.employee_id}
    base = _shift_base(tasks[0], None, now) if tasks else None
    if base is None or base > now or base < t0 - timedelta(hours=12):
        base = t0
    line_last = max((r[1] for r in reports), default=None)
    refs = floor.ref_info(tasks)
    ops = []
    for op in tasks:
        mi = modes.of(op)
        crew = crews[op.pk]
        last = max((r[1] for r in reports if r[0] == op.pk), default=None) or line_last
        p = _period(last, base, assigned[op.pk], mi["interval"])
        weights = _worked({e.pk for e in crew}, stops, absent, p["start"], now) if crew else {}
        share = workload_share(op, len(crew))
        ops.append({**floor.op_json(op, refs), "mode": mi["mode"], "mode_source": mi["source"], "split": mi["split"],
                    "period": period_json(p), "share": share,
                    "reported_today": num(sum((r[2] for r in reports if r[0] == op.pk), Decimal(0))),
                    "crew_list": [{"id": e.pk, "name": e.display_name, "w": round(weights.get(e.pk, 0), 1),
                                   "units": num(mine.get((op.pk, e.pk)) or 0), "share": share,
                                   "stop": floor.stop_json(open_stop.get(e.pk))} for e in crew]})
    ls = next((s for s in stops if s.employee_id is None and s.work_center_id == wc.pk and s.end is None), None)
    return {"id": wc.pk, "code": wc.code, "name": wc.name, "site": wc.site.name, "ops": ops,
            "stop": floor.stop_json(ls)}


def recent_reports(*, employee=None, work_center=None, now=None, limit=30) -> list[dict]:
    now = now or timezone.now()
    qs = IntervalReport.objects.filter(period_end__gte=day_start(now)).select_related("operation__order", "work_center")
    qs = qs.filter(employee=employee) if employee is not None else qs.filter(work_center=work_center)
    return [{"id": r.pk, "op": r.operation.name, "order": r.operation.order.number,
             "line": r.work_center.code if r.work_center_id else "", "kind": r.kind, "kind_label": r.get_kind_display(),
             "start": iso(r.period_start), "end": iso(r.period_end), "good": num(r.qty_good),
             "scrap": num(r.qty_scrap), "people": r.people, "undo": now - r.created_at <= UNDO_WINDOW}
            for r in qs.order_by("-period_end")[:limit]]


def sup_state(emp: Employee, now=None, tags=None) -> dict:
    now = now or timezone.now()
    tags = sup_lines(emp) if tags is None else tags
    modes = Modes()
    lines = list(WorkCenter.objects.filter(pk__in=list(tags), active=True).select_related("site").order_by("code"))
    shift = floor.employee_shift(emp, timezone.localdate(now))
    sites = {wc.site_id for wc in lines} or ({shift.site_id} if shift else set())
    all_lines = WorkCenter.objects.filter(active=True).select_related("site").order_by("site__code", "code")
    if emp.site_id or sites:
        all_lines = all_lines.filter(site_id__in=sites | ({emp.site_id} if emp.site_id else set()))
    sup = emp.supervisor
    return {
        "kind": "supernumerario", "now": iso(now),
        "who": {"id": emp.pk, "name": emp.display_name, "doc": emp.document[-4:],
                "position": emp.position.capitalize() if emp.position else "",
                "supervisor": (sup.get_full_name() or sup.get_username()) if sup else "",
                "shift": f"{shift.name} {shift.start_time:%H:%M}–{shift.end_time:%H:%M}" if shift else ""},
        "lines": [dict(line_panel(wc, now, modes), tags=sorted(tags[wc.pk])) for wc in lines],
        "all_lines": [{"id": wc.pk, "code": wc.code, "name": wc.name, "site": wc.site.name,
                       "tags": sorted(tags.get(wc.pk, ()))} for wc in all_lines[:300]],
        "reports": recent_reports(employee=emp, now=now),
        "people": sup_people(emp, [wc.pk for wc in lines], now),
        "messages": floor.unread_messages(emp, now),
        "causes": list(floor.STOP_CAUSES),
    }


def _people_scope(emp: Employee, line_ids) -> tuple[list[Operation], list[Employee]]:
    """Operaciones abiertas de sus líneas y el personal a su cargo: las cuadrillas de esas operaciones más el equipo del
    supervisor de su ficha (aunque hoy no esté en ninguna operación)."""
    ops = list(floor._ops_qs().filter(work_center_id__in=list(line_ids)).prefetch_related("crew_assignments__employee"))
    team = list(Employee.objects.filter(supervisor_id=emp.supervisor_id, active=True).exclude(pk=emp.pk)
                .exclude(floor_access__role__in=("supernumerario", "patinador"))) if emp.supervisor_id else []
    return ops, team


def people_ids(emp: Employee) -> set[int]:
    ops, team = _people_scope(emp, sup_lines(emp))
    return {ca.employee_id for op in ops for ca in op.crew_assignments.all()} | {e.pk for e in team}


def sup_people(emp: Employee, line_ids, now=None) -> dict:
    """Pestaña «Personal»: indicadores de cada persona, asistencia confirmada del día, paros y mensajes enviados."""
    from mes_core.models import Attendance
    from .models import Message

    now = now or timezone.now()
    t0 = day_start(now)
    ops, team = _people_scope(emp, line_ids)
    data = floor.board_data(ops, Mark.objects.filter(at__gte=t0, operation__work_center_id__in=list(line_ids)), now,
                            extra=team)
    ids = [c["id"] for c in data["cards"]]
    atts = {a.employee_id: a for a in Attendance.objects.filter(employee_id__in=ids, day=timezone.localdate(now))
            .select_related("confirmed_by", "shift")}
    counts = defaultdict(int)
    for c in data["cards"]:
        a = atts.get(c["id"])
        c["att"] = {"status": a.status, "label": a.get_status_display(), "notes": a.notes,
                    "shift": a.shift.name if a.shift_id else "", "at": iso(a.confirmed_at),
                    "by": (a.confirmed_by.get_full_name() or a.confirmed_by.get_username()) if a.confirmed_by_id else ""
                    } if a else None
        counts[a.get_status_display() if a else "Sin confirmar"] += 1
    data["attendance"] = {"counts": sorted(counts.items(), key=lambda kv: kv[0] == "Sin confirmar"),
                          "pending": counts.get("Sin confirmar", 0)}
    data["sent"] = [dict(floor._msg_json(m), audience=m.audience) for m in Message.objects.filter(
        sent_by_employee=emp, created_at__gte=t0).select_related("sent_by_employee").order_by("-created_at")[:10]]
    data.pop("messages", None)
    return data


def set_tablet_line(emp: Employee, wc: WorkCenter, on: bool) -> None:
    if on:
        SupernumeraryLine.objects.get_or_create(employee=emp, work_center=wc, source=SupernumeraryLine.Source.TABLET)
    else:
        SupernumeraryLine.objects.filter(employee=emp, work_center=wc, source=SupernumeraryLine.Source.TABLET).delete()
    bump(f"e{emp.pk}")


def can_report_line(emp: Employee, op: Operation) -> bool:
    return bool(op.work_center_id) and op.work_center_id in sup_lines(emp)
