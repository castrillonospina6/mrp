"""Planeación por etapas: del pedido de ventas a operaciones programadas.

Para cada etapa el planeador indica la línea, el tiempo estándar y cómo planear; el sistema calcula:

  minutos/unidad (t)   = valor (min/unidad) | 1/valor (unidades/min) | 60/valor (unidades/hora)
  horas de trabajo (W) = t × unidades / 60
  alistamiento (A)     = minutos de alistamiento / 60 × nº de lotes
  operarios (n)        según el modo:
      1 turno          n = ⌈W / (Hturno − A)⌉
      N días           n = ⌈W / (N × turnos/día × Hturno − A)⌉
      N operarios      n = N
      compromiso       n = ⌈W / (Hasignadas − A)⌉, donde las horas hábiles hasta el compromiso que dejan
                       las etapas de modo fijo se reparten entre las etapas "compromiso" según su trabajo
  duración (h)         = A + W / n          (horas hábiles: turnos sin desayuno ni almuerzo)
  horas-hombre         = W + A × n
  turnos               = duración / Hturno ;  días = turnos / turnos por día

Hturno = horas hábiles promedio por turno de la línea (duración − descansos).

Rendimiento (%): la gente no trabaja siempre al 100 % del estándar. El tiempo real por unidad es
t ÷ rendimiento (de la etapa; si no tiene, el medido de la línea; si no, el de la planta).

Horas extra de la etapa: alargar un turno ese día o un turno adicional completo, solo para la línea de la etapa.
Se calculan dentro de una transacción que se deshace (vista previa y sugerencias) y se guardan como excepciones del
calendario ligadas al pedido al «Guardar y programar».
Relación de cada etapa con la anterior:
  * En cola: empieza cuando termina el grupo anterior. Con lotes, el lote i espera al lote i de la anterior si
    ambas tienen el mismo número de lotes (flujo por lotes); si no, espera a todo el grupo anterior.
  * A la par (flujo): trabaja al tiempo con la anterior; arranca cuando la anterior lleva `link_lag` unidades
    (1 si no se indica) y no termina antes que la anterior (si es más rápida, arranca más tarde para no quedarse
    esperando producto).
  * Al mismo tiempo (paralelo): arranca con la etapa que abre el grupo, sin depender de la anterior.
Una etapa «en cola» abre un grupo nuevo; las «a la par» y «al mismo tiempo» se suman al grupo de la anterior.

Re-planear lo pendiente (órdenes que ya empezaron): lo terminado y lo que está en proceso se conserva; solo se
vuelve a programar lo pendiente de cada etapa, después de lo que ya se hizo en las etapas de las que depende.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from . import workcal
from .models import CalendarException, Downtime, Operation, PlanningRules, PlanStage, ScheduleChange, WorkCenter, WorkOrder
from .plugins import registry
from .scheduling import (
    SALDO_LOT, OperatorLedger, base_shifts, center_shifts, daily_work_hours, effective_hours_per_shift, fit_avoiding,
    fit_ending_after,
    operators_available, refresh_order_dates, shift_window, site_shifts, stage_groups, working_minutes,
)

KEEP = object()  # la etapa no dice nada del supervisor: se conserva el que tenía
DOW = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]

MAX_LOTS = 98


def minutes_per_unit(value: float, unit: str) -> float:
    if value < 0:
        raise ValidationError("El tiempo estándar no puede ser negativo.")
    if value == 0:
        return 0.0  # etapa de solo alistamiento (p. ej. liberación de ola, despacho)
    if unit == PlanStage.StdUnit.MIN_PER_UNIT:
        return value
    if unit == PlanStage.StdUnit.UNITS_PER_MIN:
        return 1 / value
    if unit == PlanStage.StdUnit.UNITS_PER_HOUR:
        return 60 / value
    raise ValidationError(f"Unidad de tiempo estándar desconocida: {unit}")


@dataclass
class StageInput:
    name: str
    line: WorkCenter
    quantity: float
    std_value: float
    std_unit: str
    setup_minutes: float
    mode: str
    mode_param: int | None
    qty_mode: str
    qty_param: float | None
    efficiency: float = 1.0          # fracción (0,85 = 85 %)
    efficiency_source: str = "planta"
    efficiency_explicit: int | None = None
    supervisor: object = KEEP        # KEEP, None (automático) o id de usuario
    overtime: list = field(default_factory=list)
    link: str = "cola"               # relación con la etapa anterior (PlanStage.Link)
    link_lag: float | None = None    # «a la par»: unidades de la anterior para arrancar
    report_mode: str = ""            # cómo reporta la planta (PlanStage.ReportMode); vacío = lo de la línea / el proceso
    split_mode: str = "tiempo"       # reparto del total de la línea entre el personal (PlanStage.SplitMode)
    # Re-planear lo pendiente
    stage_id: int | None = None
    sequence: int | None = None
    done: bool = False               # ya no le queda nada pendiente
    done_qty: float = 0.0
    kept: list = field(default_factory=list)       # ventanas de lo conservado (terminado o en proceso)
    not_before: datetime | None = None  # fin de lo ya hecho en las etapas de las que depende
    busy_until: datetime | None = None  # fin de lo que la etapa ya tiene hecho o en curso
    lot_base: int = 0                   # último número de lote ya usado


@dataclass
class StageResult:
    mpu: float = 0.0       # minutos reales por unidad (estándar ÷ rendimiento)
    std_mpu: float = 0.0   # minutos por unidad del estándar
    lots: list[float] = field(default_factory=list)
    pending: float = 0.0
    work_hours: float = 0.0
    setup_hours: float = 0.0
    crew: int = 1
    duration_hours: float = 0.0
    man_hours: float = 0.0
    shift_hours: float = 0.0
    shifts_per_day: int = 1
    day_hours: float = 0.0  # horas hábiles por día de la línea (unión de sus turnos, traslapes incluidos)
    operators_available: int | None = None
    schedule: list[tuple[datetime, datetime]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Bolsa de operarios
    free_at_start: int | None = None   # libres cuando arranca la etapa (antes de asignar su cuadrilla)
    free_min: int | None = None        # menor cantidad de libres mientras dura (antes de asignarla)
    release_at: datetime | None = None
    free_after: int | None = None      # libres cuando termina y libera su cuadrilla
    waited_hours: float = 0.0


def _num(raw, label, *, required=True, integer=False, positive=True):
    if raw in (None, ""):
        if required:
            raise ValidationError(f"falta {label}")
        return None
    try:
        value = int(raw) if integer else float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        raise ValidationError(f"{label} no es un número válido") from None
    if positive and value <= 0:
        raise ValidationError(f"{label} debe ser mayor que cero")
    return value


def _parse_overtime(order: WorkOrder, items) -> list[dict]:
    out = []
    shift_ids = set(order.site.shifts.values_list("pk", flat=True))
    for it in items or []:
        kind = it.get("kind") or "ot"
        try:
            d = date.fromisoformat(str(it.get("date")))
            a = time.fromisoformat(str(it.get("start")))
            b = time.fromisoformat(str(it.get("end")))
        except ValueError:
            raise ValidationError("horas extra con fecha u hora inválida") from None
        if a == b:
            raise ValidationError("las horas extra deben tener inicio y fin distintos")
        shift = it.get("shift")
        if kind == "ot":
            if not shift or int(shift) not in shift_ids:
                raise ValidationError("las horas extra deben indicar un turno de la planta")
            shift = int(shift)
        elif kind != "extra":
            raise ValidationError("tipo de horas extra inválido")
        out.append({"kind": kind, "date": d.isoformat(), "shift": shift if kind == "ot" else None,
                    "start": a.strftime("%H:%M"), "end": b.strftime("%H:%M")})
    return out


def overtime_hours(item: dict) -> float:
    a, b = time.fromisoformat(item["start"]), time.fromisoformat(item["end"])
    m = (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)
    return (m if m > 0 else m + 24 * 60) / 60


def can_replan(order: WorkOrder) -> bool:
    """La orden ya empezó pero sigue abierta y tiene etapas planeadas: se puede re-planear lo pendiente."""
    return (is_locked(order) and order.status in WorkOrder.OPEN_STATUSES and order.plan_stages.exists()
            and order.operations.filter(status=Operation.Status.PENDING, stage__isnull=False).exists())


def replan_context(order: WorkOrder) -> list[dict]:
    """Por etapa guardada: lo que ya se hizo o está en curso (se conserva) y lo que falta por programar."""
    now = timezone.now()
    ops = list(order.operations.all())
    out = []
    for ps in order.plan_stages.order_by("sequence"):
        mine = [o for o in ops if o.stage_id == ps.pk]
        kept = [o for o in mine if o.status != Operation.Status.PENDING]
        pend = [o for o in mine if o.status == Operation.Status.PENDING]
        qty = lambda o: float(o.quantity if o.quantity is not None else order.quantity)  # noqa: E731
        windows = []
        for o in kept:
            s0 = o.actual_start or o.planned_start or now
            if o.status == Operation.Status.DONE:
                e0 = o.actual_end or o.planned_end or s0
            else:  # en proceso: ocupa hasta su fin planeado (o hasta ahora si ya se pasó)
                e0 = max(o.planned_end or now, now)
            windows.append((s0, max(s0, e0)))
        windows.sort()
        out.append({
            "stage": ps, "pending": sum(qty(o) for o in pend), "pending_ops": len(pend),
            "kept": windows, "kept_end": max((e for _, e in windows), default=None),
            "done_qty": sum(float(o.qty_good or 0) for o in kept),
            "kept_qty": sum(qty(o) for o in kept),
            "in_progress": sum(1 for o in kept if o.status == Operation.Status.IN_PROGRESS),
            "finished": sum(1 for o in kept if o.status == Operation.Status.DONE),
            "lot_base": max((o.sequence % 100 for o in kept if o.sequence % 100 != SALDO_LOT), default=0),
            "saldo_taken": any(o.sequence % 100 == SALDO_LOT for o in kept),
        })
    return out


def replan_info(order: WorkOrder) -> list[dict]:
    """Resumen para la pantalla de planeación en modo «re-planear lo pendiente»."""
    return [{"pending": c["pending"], "kept_qty": c["kept_qty"], "done_qty": c["done_qty"],
             "in_progress": c["in_progress"], "finished": c["finished"],
             "kept_end": _iso(c["kept_end"])} for c in replan_context(order)]


def parse_stages(order: WorkOrder, raw: list[dict], replan: bool = False) -> list[StageInput]:
    if not raw:
        raise ValidationError("Agrega al menos una etapa.")
    ctx = None
    if replan:
        ctx = replan_context(order)
        if len(raw) != len(ctx):
            raise ValidationError("Al re-planear lo pendiente no se agregan ni se quitan etapas.")
        raw = [{**r, "name": c["stage"].name, "quantity": c["pending"] or 1} for r, c in zip(raw, ctx)]
    lines = {wc.pk: wc for wc in WorkCenter.objects.filter(site=order.site, active=True).prefetch_related("shifts")}
    rules = PlanningRules.objects.filter(site=order.site).first()
    default_eff = rules.default_efficiency if rules else 100
    from .staffing import supervisors
    valid_sups = None
    stages, errors = [], []
    for i, r in enumerate(raw, start=1):
        try:
            name = (r.get("name") or "").strip()
            if not name:
                raise ValidationError("falta el nombre")
            line = lines.get(int(r.get("line") or 0))
            if line is None:
                raise ValidationError(f"«{name}» no tiene línea: elígela en la lista (marcada en naranja)" if not r.get("line")
                                      else f"«{name}»: la línea elegida no está activa o no es de la planta {order.site.code}")
            mode = r.get("mode") or PlanStage.Mode.SHIFT
            qty_mode = r.get("qty_mode") or PlanStage.QtyMode.ALL
            if mode not in PlanStage.Mode.values or qty_mode not in PlanStage.QtyMode.values:
                raise ValidationError("modo de planeación inválido")
            needs_param = mode in (PlanStage.Mode.DAYS, PlanStage.Mode.CREW)
            stages.append(StageInput(
                name=name[:120], line=line,
                quantity=_num(r.get("quantity"), "las unidades"),
                std_value=_num(r.get("std_value"), "el tiempo estándar", positive=False),
                std_unit=r.get("std_unit") or PlanStage.StdUnit.MIN_PER_UNIT,
                setup_minutes=_num(r.get("setup_minutes") or 0, "el alistamiento", positive=False) or 0.0,
                mode=mode,
                mode_param=_num(r.get("mode_param"), "días u operarios", required=needs_param, integer=True)
                if needs_param else None,
                qty_mode=qty_mode,
                qty_param=_num(r.get("qty_param"), "unidades del parcial / tamaño del lote",
                               required=qty_mode != PlanStage.QtyMode.ALL) if qty_mode != PlanStage.QtyMode.ALL else None,
            ))
            if stages[-1].setup_minutes < 0 or stages[-1].std_value < 0:
                raise ValidationError("el tiempo estándar y el alistamiento no pueden ser negativos")
            if stages[-1].std_value == 0 and stages[-1].setup_minutes == 0:
                raise ValidationError("indica el tiempo estándar (o al menos el alistamiento)")
            st = stages[-1]
            eff_raw = r.get("efficiency")
            if eff_raw not in (None, ""):
                eff = _num(eff_raw, "el rendimiento", integer=False)
                if not 10 <= eff <= 150:
                    raise ValidationError("el rendimiento va de 10 % a 150 %")
                st.efficiency, st.efficiency_source, st.efficiency_explicit = eff / 100, "etapa", round(eff)
            elif line.efficiency:
                st.efficiency, st.efficiency_source = line.efficiency / 100, "línea"
            else:
                st.efficiency, st.efficiency_source = default_eff / 100, "planta"
            if "supervisor" in r:
                sup = r.get("supervisor")
                if sup in (None, "", "auto"):
                    st.supervisor = None
                else:
                    if valid_sups is None:
                        valid_sups = set(supervisors().values_list("pk", flat=True))
                    if int(sup) not in valid_sups:
                        raise ValidationError("el supervisor elegido no tiene el rol de supervisor")
                    st.supervisor = int(sup)
            st.overtime = _parse_overtime(order, r.get("overtime"))
            link = r.get("link") or PlanStage.Link.QUEUE
            if link not in PlanStage.Link.values:
                raise ValidationError("relación con la etapa anterior inválida")
            st.link = PlanStage.Link.QUEUE if i == 1 else link
            if st.link == PlanStage.Link.FLOW:
                st.link_lag = _num(r.get("link_lag"), "las unidades para arrancar a la par", required=False)
            st.report_mode = r.get("report_mode") or ""
            st.split_mode = r.get("split_mode") or PlanStage.SplitMode.TIME
            if st.report_mode and st.report_mode not in PlanStage.ReportMode.values:
                raise ValidationError("forma de reportar inválida")
            if st.split_mode not in PlanStage.SplitMode.values:
                raise ValidationError("reparto inválido")
        except ValidationError as exc:
            errors.append(f"Etapa {i}: {'; '.join(exc.messages)}")
    if errors:
        raise ValidationError(errors)
    if ctx is not None:
        for st, c in zip(stages, ctx):
            st.stage_id, st.sequence = c["stage"].pk, c["stage"].sequence
            st.done = c["pending"] <= 0
            st.done_qty, st.kept, st.lot_base = c["done_qty"], c["kept"], c["lot_base"]
            st.busy_until = c["kept_end"]
        # lo ya hecho en los grupos anteriores fija el piso de cada etapa
        groups = stage_groups([st.link for st in stages])
        floor = None
        for g in groups:
            for i in g:
                stages[i].not_before = floor
            ends = [stages[i].busy_until for i in g if stages[i].busy_until]
            if ends:
                floor = max([floor, *ends]) if floor else max(ends)
    return stages


def _split(st: StageInput) -> tuple[list[float], float]:
    q = st.quantity
    if st.qty_mode == PlanStage.QtyMode.PARTIAL:
        if st.qty_param > q:
            raise ValidationError(f"«{st.name}»: el parcial ({st.qty_param:g}) supera las unidades ({q:g}).")
        return [st.qty_param], q - st.qty_param
    if st.qty_mode == PlanStage.QtyMode.LOTS:
        n = math.ceil(q / st.qty_param)
        if n > MAX_LOTS:
            raise ValidationError(f"«{st.name}»: {n} lotes son demasiados (máximo {MAX_LOTS}); aumenta el tamaño del lote.")
        return [st.qty_param] * (n - 1) + [q - st.qty_param * (n - 1)], 0.0
    return [q], 0.0


def calculate(order: WorkOrder, stages: list[StageInput], start: datetime,
              respect_operators: bool = True) -> tuple[list[StageResult], dict]:
    site_sh = site_shifts(order.site)
    results: list[StageResult] = []
    for st in stages:
        shifts = center_shifts(st.line, site_sh)
        regular = base_shifts(shifts)  # «un turno» y el tope de operarios se miden con los turnos regulares
        std_mpu = minutes_per_unit(st.std_value, st.std_unit)
        r = StageResult(
            std_mpu=round(std_mpu, 6),
            mpu=round(std_mpu / (st.efficiency or 1), 6),
            shift_hours=effective_hours_per_shift(regular),
            shifts_per_day=len(regular) or 1,
            day_hours=daily_work_hours(shifts),
            operators_available=operators_available(regular, order.site.operators_total, timezone.localtime(start).date()),
        )
        r.lots, r.pending = ([], 0.0) if st.done else _split(st)
        r.work_hours = r.mpu * sum(r.lots) / 60
        r.setup_hours = st.setup_minutes / 60 * len(r.lots)
        results.append(r)

    def crew_for(r: StageResult, available_hours: float, why: str) -> int:
        usable = available_hours - r.setup_hours
        if usable <= 0:
            r.warnings.append(f"No alcanza: {why} da {available_hours:.1f} h hábiles y solo el alistamiento toma "
                              f"{r.setup_hours:.1f} h.")
            return max(1, r.operators_available or 1)
        return max(1, math.ceil(r.work_hours / usable - 1e-9))

    # 1) Modos de cuadrilla fija o de ventana propia.
    for st, r in zip(stages, results):
        if st.done:
            continue
        if st.mode == PlanStage.Mode.SHIFT:
            r.crew = crew_for(r, r.shift_hours, "un turno")
        elif st.mode == PlanStage.Mode.DAYS:
            r.crew = crew_for(r, st.mode_param * r.day_hours, f"{st.mode_param} día(s)")
        elif st.mode == PlanStage.Mode.CREW:
            r.crew = st.mode_param

    # 2) Modo "compromiso": reparte las horas hábiles que quedan hasta la fecha compromiso.
    #    Las etapas de un mismo grupo (a la par o al mismo tiempo) comparten la ventana; los grupos van en cola.
    is_deadline = lambda i: stages[i].mode == PlanStage.Mode.DEADLINE and not stages[i].done  # noqa: E731
    deadline_stages = [i for i in range(len(stages)) if is_deadline(i)]
    available_until_due = None
    if order.due_date:
        available_until_due = working_minutes(site_sh, start, order.due_date) / 60
    if deadline_stages:
        if available_until_due is None:
            raise ValidationError("El modo «Terminar antes del compromiso» necesita que el pedido tenga fecha compromiso.")
        groups = stage_groups([st.link for st in stages])
        fixed = sum(max((results[i].setup_hours + results[i].work_hours / results[i].crew for i in g), default=0.0)
                    for g in groups if not any(is_deadline(i) for i in g))
        remaining = available_until_due - fixed
        weight = {gi: max(results[i].work_hours + results[i].setup_hours for i in g if is_deadline(i))
                  for gi, g in enumerate(groups) if any(is_deadline(i) for i in g)}
        total_work = sum(weight.values()) or 1
        for gi, g in enumerate(groups):
            if gi not in weight:
                continue
            share = remaining * weight[gi] / total_work
            for i in g:
                if is_deadline(i):
                    results[i].crew = crew_for(results[i], share, "el tiempo hasta el compromiso")

    for st, r in zip(stages, results):
        if st.done:
            continue
        r.duration_hours = r.setup_hours + r.work_hours / r.crew
        r.man_hours = r.work_hours + r.setup_hours * r.crew
        if r.operators_available and r.crew > r.operators_available:
            r.warnings.append(f"Requiere {r.crew} operarios y el turno tiene {r.operators_available} disponibles.")
        if r.pending:
            r.warnings.append(f"Quedan {r.pending:g} unidades como saldo en el backlog.")

    # 3) Programación en el calendario (turnos, descansos, mantenimientos y bolsa de operarios).
    blocked: dict[int, list] = {}
    for d in Downtime.objects.filter(work_center__in=[st.line for st in stages], end__gt=start):
        blocked.setdefault(d.work_center_id, []).append((d.start, d.end))
    # Operarios ya comprometidos por OTRAS órdenes (las operaciones de esta se reemplazan al guardar).
    ledger = OperatorLedger(order.site, site_sh, exclude_order_id=order.pk, since=start)

    def place(t0, minutes, shifts, blk, crew, cap):
        """Primer hueco desde t0 con turno libre y, si se pide, con `crew` operarios libres todo el tiempo."""
        s, e = fit_avoiding(t0, minutes, shifts, blk)
        if not respect_operators or ledger.unlimited or (cap is not None and crew > cap):
            return s, e
        t = t0
        for _ in range(300):
            free, when = ledger.min_free(s, e)
            if free is None or free >= crew:
                return s, e
            nxt = ledger.next_release(when)  # esperar a que termine alguna cuadrilla
            if nxt is None:
                return s, e
            t = nxt
            s, e = fit_avoiding(t, minutes, shifts, blk)
        return s, e

    def flow_bounds(prev, st, r, shifts, blk):
        """«A la par»: por lote, (cuándo la anterior ya entregó lo necesario para arrancar, cuándo entrega lo último)."""
        pst, pr, psh, pblk = prev
        lag = st.link_lag or 1.0
        total = sum(pr.lots)

        def produced_at(units):  # momento en que la etapa anterior lleva `units` unidades
            units = min(units, total)
            cum = 0.0
            for q, (s0, e0) in zip(pr.lots, pr.schedule):
                if cum + q >= units - 1e-9:
                    need = units - cum
                    if need >= q - 1e-9:
                        return e0
                    mins = pst.setup_minutes + pr.mpu * need / pr.crew
                    return fit_avoiding(s0, mins, psh, pblk)[1] if mins > 0 else s0
                cum += q
            return pr.schedule[-1][1]

        out, cum = [], 0.0
        for q in r.lots:
            out.append((produced_at(cum + lag), produced_at(cum + q)))
            cum += q
        return out

    prev_group: list[list[datetime]] = []  # fin de los lotes de cada etapa del grupo anterior
    group: list[list[datetime]] = []
    group_lines: list[tuple[int, str]] = []
    prev = None
    for idx, (st, r) in enumerate(zip(stages, results)):
        shifts = center_shifts(st.line, site_sh)
        blk = blocked.get(st.line.pk, [])
        link = st.link if idx > 0 else PlanStage.Link.QUEUE
        if link == PlanStage.Link.QUEUE and idx > 0:
            prev_group, group, group_lines = group, [], []
        if st.done:  # re-planear: la etapa ya terminó (o solo tiene trabajo en curso)
            at = st.busy_until or start
            r.schedule = list(st.kept) or [(at, at)]
            r.release_at = r.schedule[-1][1]
            group.append([r.schedule[-1][1]])
            prev = None
            continue
        same = next((n for pk, n in group_lines if pk == st.line.pk), None)
        if same:
            r.warnings.append(f"Comparte la línea {st.line.code} con «{same}» al mismo tiempo: verifica que la línea "
                              f"tenga módulos para las dos.")
        group_lines.append((st.line.pk, st.name))
        n_lots = len(r.lots)

        def dep_for(i):
            if not prev_group:
                return start
            if len(prev_group) == 1 and len(prev_group[0]) == n_lots:
                return prev_group[0][i]
            return max(e for g in prev_group for e in g)

        flow = flow_bounds(prev, st, r, shifts, blk) if link == PlanStage.Link.FLOW and prev else None
        cursor = max(start, st.busy_until) if st.busy_until else start
        ends = []
        for i, q in enumerate(r.lots):
            dep = dep_for(i)
            if st.not_before and st.not_before > dep:
                dep = st.not_before
            minutes = st.setup_minutes + r.mpu * q / r.crew
            earliest = max(dep, cursor)
            if flow:
                earliest = max(earliest, flow[i][0])
            if flow:  # a la par: no termina antes de recibir la última unidad de la anterior → arranca más tarde
                earliest = fit_ending_after(earliest, minutes, shifts, blk, flow[i][1])[0]
            s_now, _ = fit_avoiding(earliest, minutes, shifts, blk)
            s, e = place(earliest, minutes, shifts, blk, r.crew, r.operators_available)
            r.waited_hours += (s - s_now).total_seconds() / 3600
            free, _ = ledger.min_free(s, e)
            if i == 0:
                r.free_at_start = ledger.free_at(s)
            if free is not None:
                r.free_min = free if r.free_min is None else min(r.free_min, free)
            r.schedule.append((s, e))
            ledger.add(s, e, r.crew, f"{order.number} · {st.name}")
            cursor = e
            ends.append(e)
        group.append(ends)
        prev = (st, r, shifts, blk)
        r.release_at = r.schedule[-1][1]
        r.free_after = ledger.free_at(r.release_at)
        if r.waited_hours > 0.01:
            r.warnings.append(f"Espera {r.waited_hours:.1f} h a que se liberen operarios; arranca "
                              f"{timezone.localtime(r.schedule[0][0]):%a %d/%m %H:%M}.")
        if r.free_min is not None and r.free_min < r.crew:
            r.warnings.append(f"En algún momento solo hay {max(0, r.free_min)} operarios libres para una cuadrilla de "
                              f"{r.crew}: la planta queda sobreasignada.")

    end = max((e for r in results for _, e in r.schedule), default=start)
    active = [r for st, r in zip(stages, results) if not st.done] or results
    first_start = min((r.schedule[0][0] for r in active), default=start)
    first = min(active, key=lambda r: r.schedule[0][0])
    operators = None
    if not ledger.unlimited:
        available = ledger.available_at(first_start)
        operators = {
            "at": first_start, "available": available, "free": first.free_at_start,
            "used_by_others": available - first.free_at_start if first.free_at_start is not None else None,
            # liberaciones durante la planeación y en las 24 h siguientes: para saber cuándo reutilizar gente
            "releases": ledger.releases(first_start, end + timedelta(hours=24), limit=12),
        }
    summary = {
        "start": start, "end": end, "due": order.due_date,
        "available_hours": available_until_due,
        "man_hours": sum(r.man_hours for r in results),
        "max_crew": _peak_crew(stages, results),
        "slack_hours": (order.due_date - end).total_seconds() / 3600 if order.due_date else None,
        "operators": operators,
    }
    return results, summary


def _peak_crew(stages: list[StageInput], results: list[StageResult]) -> int:
    """Máximo de operarios trabajando a la vez en el pedido (las etapas en paralelo se suman)."""
    events = []
    for st, r in zip(stages, results):
        if st.done:
            continue
        for s0, e0 in r.schedule:
            if e0 > s0:
                events += [(s0, 1, r.crew), (e0, 0, -r.crew)]  # en el mismo instante primero se libera
    peak = cur = 0
    for _, _, d in sorted(events):
        cur += d
        peak = max(peak, cur)
    return peak or max((r.crew for st, r in zip(stages, results) if not st.done), default=0)


def _iso(dt):
    return timezone.localtime(dt).isoformat() if dt else None


class _Rollback(Exception):
    def __init__(self, value):
        self.value = value


def _apply_overtime(order: WorkOrder, stages: list[StageInput], user=None) -> None:
    """Crea (en la transacción en curso) las horas extra de cada etapa, solo para su línea."""
    CalendarException.objects.filter(order=order).delete()
    u = user if user is not None and user.is_authenticated else None
    for st in stages:
        for it in st.overtime:
            extra = it["kind"] == "extra"
            exc = CalendarException(
                site=order.site, order=order, stage_name=st.name[:120], date=date.fromisoformat(it["date"]),
                kind=CalendarException.Kind.EXTRA_SHIFT if extra else CalendarException.Kind.OVERTIME,
                name=f"{'Turno adicional' if extra else 'Horas extra'} {order.number or ''} · {st.name}"[:120],
                shift_id=it["shift"], start_time=time.fromisoformat(it["start"]), end_time=time.fromisoformat(it["end"]),
                created_by=u)
            exc.full_clean(exclude=["work_centers", "order"])
            exc.save()
            exc.work_centers.set([st.line])
    workcal.invalidate(order.site_id)


def _simulate(order: WorkOrder, stages: list[StageInput], start: datetime, respect: bool, extra=None):
    """Calcula con las horas extra de las etapas aplicadas temporalmente (se deshace al terminar)."""
    try:
        with transaction.atomic():
            _apply_overtime(order, stages)
            results, summary = calculate(order, stages, start, respect)
            more = extra(results, summary) if extra else None
            raise _Rollback((results, summary, more))
    except _Rollback as rb:
        return rb.value
    finally:
        workcal.invalidate(order.site_id)


def _local(dt: datetime) -> datetime:
    return timezone.localtime(dt)


def extension_points(order: WorkOrder, line: WorkCenter, start: datetime, until: datetime, limit: int = 10) -> list[dict]:
    """Finales de jornada de la línea entre start y until donde se puede alargar el turno (hay hueco después)."""
    site_sh = site_shifts(order.site)
    shifts = center_shifts(line, site_sh)
    if not shifts or until <= start:
        return []
    cal = workcal.get_calendar(order.site_id)
    span_end = until + timedelta(days=2)
    blocks = cal.windows(shifts, start - timedelta(hours=12), span_end, line.pk, with_breaks=True)
    pts = []
    for i, (a, b) in enumerate(blocks):
        if b <= start or b > until + timedelta(hours=1):
            continue
        nxt = blocks[i + 1][0] if i + 1 < len(blocks) else span_end
        gap = (nxt - b).total_seconds() / 3600
        if gap < 0.25:
            continue
        owner, owner_day, note = None, None, ""
        loc = _local(b)
        for sh in base_shifts(shifts):
            for d in (loc.date() - timedelta(days=1), loc.date(), loc.date() + timedelta(days=1)):
                if shift_window(sh, d)[1] == b:
                    owner, owner_day = sh, _local(shift_window(sh, d)[0]).date()
        if owner is None:  # el bloque ya termina con horas extra: se sigue alargando el mismo turno
            x = next((x for x in cal.extras if x.end == b and x.kind == "horas_extra" and x.shift_id), None)
            owner = next((sh for sh in shifts if x and sh.pk == x.shift_id), None)
            if x:
                owner_day, note = _local(x.start).date(), " (ya con horas extra)"
        if owner is None:
            continue
        pts.append({"date": loc.date().isoformat(), "shift": owner.pk, "shift_name": owner.name, "start": loc.strftime("%H:%M"),
                    "max_hours": round(min(gap, 6.0), 2), "at": loc.isoformat(),
                    "label": f"{owner.name} del {DOW[owner_day.weekday()]} {owner_day:%d/%m}{note}, después de las {loc:%H:%M}"})
    return pts[:limit]


def _gap_shifts(order: WorkOrder, line: WorkCenter, start: datetime, until: datetime, limit: int = 3) -> list[dict]:
    """Turnos adicionales posibles: huecos sin trabajo (noche, fin de semana, festivo) antes de `until`."""
    site_sh = site_shifts(order.site)
    shifts = center_shifts(line, site_sh)
    regular = base_shifts(shifts)
    if not regular or until <= start:
        return []
    cal = workcal.get_calendar(order.site_id)
    blocks = cal.windows(shifts, start, until, line.pk, with_breaks=True)
    model = min(regular, key=lambda s: s.start_time)  # el primer turno del día como modelo
    dur = timedelta(minutes=model.duration_minutes)
    out = []
    cursor = start
    gaps = []
    for a, b in blocks + [(until, until)]:
        if a > cursor:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    for g0, g1 in gaps:
        d = _local(g0).date()
        while d <= _local(g1).date() and len(out) < limit:  # un día entero libre: turno con el horario habitual
            a, b = shift_window(model, d)
            if a >= g0 and b <= g1 and b <= until:
                out.append({"kind": "extra", "date": _local(a).date().isoformat(), "shift": None,
                            "start": _local(a).strftime("%H:%M"), "end": _local(b).strftime("%H:%M"), "at": _local(a).isoformat(),
                            "label": f"Turno adicional {DOW[_local(a).weekday()]} {_local(a):%d/%m} {_local(a):%H:%M}–{_local(b):%H:%M}"})
            d += timedelta(days=1)
        if len(out) >= limit:
            break
        if not any(o["at"] >= _local(g0).isoformat() and o["at"] < _local(g1).isoformat() for o in out) and g1 - g0 >= dur \
                and g0 + dur <= until:
            a, b = _local(g0), _local(g0 + dur)  # p. ej. la noche entre dos días de trabajo
            out.append({"kind": "extra", "date": a.date().isoformat(), "shift": None, "start": a.strftime("%H:%M"),
                        "end": b.strftime("%H:%M"), "at": a.isoformat(),
                        "label": f"Turno adicional {DOW[a.weekday()]} {a:%d/%m} {a:%H:%M}–{b:%H:%M}"})
    return out[:limit]


def _rule_supervisors(order: WorkOrder, st: StageInput, r: StageResult) -> list[str]:
    from .staffing import line_rules, shift_day_of, user_label
    rules = line_rules(order.site)
    if not rules:
        return []
    shifts = site_shifts(order.site)
    names = []
    for s, _ in r.schedule:
        sh, _ = shift_day_of(order.site, s, shifts)
        sup = rules.get((st.line.pk, sh.pk)) if sh else None
        label = f"{user_label(sup)} ({st.line.code} · {sh.name})" if sup else (f"sin regla para {st.line.code} · {sh.name}" if sh else "")
        if label and label not in names:
            names.append(label)
    return names


def preview(order: WorkOrder, raw: list[dict], start: datetime, respect_operators: bool = True,
            replan: bool = False) -> dict:
    stages = parse_stages(order, raw, replan)

    def extras(results, summary):
        out = []
        for st, r in zip(stages, results):
            if st.done:
                out.append({"points": [], "rules": None, "shifts_before_due": None})
                continue
            s0, e0 = r.schedule[0][0], r.schedule[-1][1]
            horizon = max(e0, order.due_date or e0)
            before_due = None
            if order.due_date and r.shift_hours:
                before_due = working_minutes(center_shifts(st.line, site_shifts(order.site)), s0, order.due_date) / 60 / r.shift_hours
            out.append({"points": extension_points(order, st.line, s0, horizon), "rules": _rule_supervisors(order, st, r),
                        "shifts_before_due": round(max(0.0, before_due), 2) if before_due is not None else None})
        return out

    results, summary, more = _simulate(order, stages, start, respect_operators, extras)
    ops = summary["operators"]
    return {
        "stages": [{
            "mpu": r.mpu, "std_mpu": r.std_mpu, "efficiency": round(st.efficiency * 100), "efficiency_source": st.efficiency_source,
            "supervisor_rule": more[i]["rules"], "points": more[i]["points"], "shifts_before_due": more[i]["shifts_before_due"],
            "overtime_hours": round(sum(overtime_hours(x) for x in st.overtime), 2),
            "overtime_man_hours": round(sum(overtime_hours(x) for x in st.overtime) * r.crew, 1),
            "units_now": sum(r.lots), "pending": r.pending, "lots": len(r.lots),
            "work_hours": round(r.work_hours, 2), "man_hours": round(r.man_hours, 2), "crew": r.crew,
            "duration_hours": round(r.duration_hours, 2),
            "shifts": round(r.duration_hours / r.shift_hours, 2) if r.shift_hours else None,
            "days": round(r.duration_hours / r.day_hours, 2) if r.day_hours else None,
            "shift_hours": round(r.shift_hours, 2), "shifts_per_day": r.shifts_per_day,
            "operators_available": r.operators_available,
            "start": _iso(r.schedule[0][0]), "end": _iso(r.schedule[-1][1]),
            "schedule": [{"qty": q, "start": _iso(s), "end": _iso(e)} for q, (s, e) in zip(r.lots, r.schedule)],
            "warnings": r.warnings,
            "free_at_start": r.free_at_start, "free_min": r.free_min,
            "remaining": r.free_min - r.crew if r.free_min is not None else None,
            "release_at": _iso(r.release_at), "free_after": r.free_after,
            "waited_hours": round(r.waited_hours, 2),
            "link": st.link, "link_lag": st.link_lag,
            "done": st.done, "done_qty": st.done_qty, "kept_end": _iso(st.busy_until),
        } for i, (st, r) in enumerate(zip(stages, results))],
        "replan": replan,
        "summary": {
            **{k: _iso(v) if isinstance(v, datetime) else v for k, v in summary.items()},
            "man_hours": round(summary["man_hours"], 2),
            "slack_hours": round(summary["slack_hours"], 2) if summary["slack_hours"] is not None else None,
            "available_hours": round(summary["available_hours"], 2) if summary["available_hours"] is not None else None,
            "takt": takt_analysis(order, stages, results, summary),
            "operators": {
                **{k: v for k, v in ops.items() if k not in ("at", "releases")}, "at": _iso(ops["at"]),
                "releases": [{**x, "at": _iso(x["at"])} for x in ops["releases"]],
            } if ops else None,
        },
    }


def takt_analysis(order: WorkOrder, stages: list[StageInput], results: list[StageResult], summary: dict) -> dict:
    """Takt time frente al tiempo estándar y al tiempo de ciclo del proceso.

      takt (min/pieza)          = minutos hábiles desde el inicio hasta el compromiso ÷ unidades del pedido
      tiempo estándar de 1 pieza = Σ minutos por unidad del estándar de todas las etapas (contenido de trabajo)
      tiempo real de 1 pieza     = Σ minutos por unidad reales (estándar ÷ rendimiento)
      ciclo de una etapa         = minutos reales por unidad ÷ operarios de la etapa (cada cuánto sale una pieza)
      ciclo del proceso          = el ciclo de la etapa más lenta (cuello de botella)
      operarios mínimos          = ⌈tiempo real de 1 pieza ÷ takt⌉ (para seguir el ritmo con trabajo balanceado)
    El alistamiento no entra en el ciclo por pieza; se informa aparte porque consume tiempo disponible."""
    replan = any(st.stage_id for st in stages)
    demand = (max((st.quantity for st in stages if not st.done), default=0.0) if replan
              else float(order.quantity or 0))
    available_min = (summary.get("available_hours") or 0) * 60
    setup_min = sum(r.setup_hours for r in results) * 60
    takt = available_min / demand if demand > 0 and available_min > 0 else None
    takt_net = (available_min - setup_min) / demand if takt and available_min > setup_min else None
    rows = []
    for st, r in zip(stages, results):
        if st.done:
            rows.append({"name": st.name, "line": st.line.code, "std_mpu": 0, "real_mpu": 0, "crew": 0, "ct": 0,
                         "setup_min": 0, "ratio": None, "ok": True, "pph": None, "setup_only": True, "done": True})
            continue
        ct = r.mpu / r.crew if r.crew else r.mpu
        rows.append({
            "name": st.name, "line": st.line.code, "std_mpu": round(r.std_mpu, 4), "real_mpu": round(r.mpu, 4),
            "crew": r.crew, "ct": round(ct, 4), "setup_min": round(r.setup_hours * 60, 1),
            "ratio": round(ct / takt, 3) if takt else None, "ok": (ct <= takt + 1e-9) if takt else None,
            "pph": round(60 / ct, 1) if ct > 0 else None,
            "setup_only": r.std_mpu == 0,
        })
    work = [x for x in rows if not x["setup_only"]]
    std_piece = sum(x["std_mpu"] for x in rows)
    real_piece = sum(x["real_mpu"] for x in rows)
    bottleneck = max(range(len(rows)), key=lambda i: rows[i]["ct"]) if work else None
    process_ct = rows[bottleneck]["ct"] if bottleneck is not None else 0.0
    crew_used = sum(r.crew for st, r in zip(stages, results) if not st.done)
    min_ops = math.ceil(real_piece / takt - 1e-9) if takt and real_piece else None
    return {
        "demand": demand, "unit": order.product.unit, "available_min": round(available_min, 1),
        "takt": round(takt, 4) if takt else None, "takt_net": round(takt_net, 4) if takt_net else None,
        "setup_min": round(setup_min, 1),
        "std_piece": round(std_piece, 4), "real_piece": round(real_piece, 4),
        "process_ct": round(process_ct, 4), "bottleneck": bottleneck,
        "meets": (process_ct <= takt + 1e-9) if takt else None,
        "pph_needed": round(60 / takt, 1) if takt else None,
        "pph_bottleneck": round(60 / process_ct, 1) if process_ct else None,
        "min_operators": min_ops, "crew_used": crew_used,
        "std_vs_takt": round(std_piece / takt, 2) if takt else None,
        "stages": rows,
    }


def is_locked(order: WorkOrder) -> bool:
    """Una orden que ya empezó a producirse no se re-planea por etapas (se ajusta desde el programador)."""
    return order.operations.exclude(status=Operation.Status.PENDING).exists() or order.status not in (
        WorkOrder.Status.TO_PLAN, WorkOrder.Status.DRAFT, WorkOrder.Status.PLANNED, WorkOrder.Status.SUPPLIES,
        WorkOrder.Status.RELEASED)


@transaction.atomic
def save_plan(order: WorkOrder, raw: list[dict], start: datetime, user=None,
              respect_operators: bool = True, replan: bool = False, reopen: bool = False) -> WorkOrder:
    """Guarda las etapas y crea sus operaciones programadas.

    replan=True (orden que ya empezó): conserva lo terminado y lo que está en proceso, y vuelve a programar solo lo
    pendiente de cada etapa; no se agregan ni se quitan etapas.

    reopen=True («Editar todo»): se puede cambiar absolutamente todo aunque la orden ya haya empezado o esté
    terminada. Se borran sus operaciones con lo reportado en ellas (reportes, cuadrillas y marcaciones de piso) y el
    pedido vuelve a «Liberada» si ya estaba en planta."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    lost = None
    if replan:
        if not can_replan(order):
            raise ValidationError("No hay nada pendiente que re-planear en esta orden (o ya está cerrada).")
    elif reopen:
        from .corrections import progress_summary

        lost = progress_summary(order) if is_locked(order) else None
    elif is_locked(order):
        raise ValidationError("La orden ya empezó: usa «Editar todo» o «Re-planear lo pendiente».")
    stages = parse_stages(order, raw, replan)
    _apply_overtime(order, stages, user)  # horas extra de las etapas: reemplazan las anteriores del pedido
    results, summary = calculate(order, stages, start, respect_operators)
    now = timezone.now()
    can_assign = user is None or not user.is_authenticated or user.has_perm("mes_core.assign_supervisor")

    # El supervisor responsable se conserva por etapa; las cuadrillas se rearman (cambian las horas).
    keep_sup = {}
    # (los puestos por regla de línea y turno no se copian: se recalculan solos con el nuevo horario)
    for o in order.operations.select_related("stage").filter(supervisor__isnull=False, supervisor_auto=False):
        keep_sup.setdefault(o.stage.name if o.stage_id else o.name.split(" · ")[0],
                            (o.supervisor_id, o.supervisor_assigned_by_id, o.supervisor_assigned_at))
    if replan:
        stage_ids = [st.stage_id for st in stages]
        doomed = order.operations.filter(status=Operation.Status.PENDING, stage_id__in=stage_ids)
        had_crew = doomed.filter(crew_assignments__isnull=False).exists()
        doomed.delete()
        existing = {ps.pk: ps for ps in order.plan_stages.all()}
    else:
        had_crew = order.operations.filter(crew_assignments__isnull=False).exists()
        order.operations.all().delete()
        order.plan_stages.all().delete()
    for idx, (st, r) in enumerate(zip(stages, results), start=1):
        fields = dict(
            name=st.name, work_center=st.line, std_value=Decimal(str(st.std_value)), std_unit=st.std_unit,
            setup_minutes=Decimal(str(st.setup_minutes)), mode=st.mode, mode_param=st.mode_param, qty_mode=st.qty_mode,
            qty_param=Decimal(str(st.qty_param)) if st.qty_param is not None else None,
            man_hours=Decimal(f"{r.man_hours:.2f}"), crew=r.crew, duration_hours=Decimal(f"{r.duration_hours:.2f}"),
            efficiency=st.efficiency_explicit, link=st.link,
            link_lag=Decimal(str(st.link_lag)) if st.link_lag is not None else None,
            report_mode=st.report_mode, split_mode=st.split_mode,
        )
        if replan:
            ps = existing[st.stage_id]
            if st.done:  # solo cambia la relación y cómo reporta la planta; lo demás describe lo que ya se hizo
                fields = {k: fields[k] for k in ("link", "link_lag", "report_mode", "split_mode")}
            for k, v in fields.items():
                setattr(ps, k, v)
            ps.save(update_fields=list(fields))
            seq = ps.sequence
        else:
            ps = PlanStage.objects.create(order=order, sequence=idx, quantity=Decimal(str(st.quantity)), **fields)
            seq = idx
        sup_id, sup_by, sup_at = keep_sup.get(st.name, (None, None, None))
        if st.supervisor is not KEEP and can_assign:
            if st.supervisor is None:  # automático: lo pone la regla de línea y turno al crear cada operación
                sup_id = sup_by = sup_at = None
            else:
                prev = keep_sup.get(st.name)
                same = prev and prev[0] == st.supervisor
                sup_id = st.supervisor
                sup_by = prev[1] if same else (user.pk if user is not None and user.is_authenticated else None)
                sup_at = prev[2] if same else now
            ps.supervisor_id = st.supervisor
            ps.save(update_fields=["supervisor"])
        if st.done:
            continue
        common = dict(order=order, stage=ps, work_center_kind=st.line.kind, work_center=st.line, crew=r.crew,
                      setup_minutes=Decimal(str(st.setup_minutes)), minutes_per_unit=Decimal(f"{r.mpu:.6f}"),
                      supervisor_id=sup_id, supervisor_assigned_by_id=sup_by, supervisor_assigned_at=sup_at)
        n, base = len(r.lots), st.lot_base
        if base + n > MAX_LOTS:
            raise ValidationError(f"«{st.name}»: con lo ya hecho quedarían {base + n} lotes (máximo {MAX_LOTS}); "
                                  f"aumenta el tamaño del lote.")
        for lot, (q, (s, e)) in enumerate(zip(r.lots, r.schedule), start=1):
            if base:
                suffix = f" · lote {base + lot}" if n > 1 else " · pendiente"
            else:
                suffix = f" · lote {lot}/{n}" if n > 1 else (" · parcial" if r.pending else "")
            Operation.objects.create(sequence=seq * 100 + base + lot, name=f"{st.name}{suffix}"[:120],
                                     quantity=Decimal(str(q)), planned_start=s, planned_end=e, **common)
        if r.pending:
            if order.operations.filter(sequence=seq * 100 + SALDO_LOT).exists():
                raise ValidationError(f"«{st.name}»: el saldo ya está en proceso; termínalo antes de dejar otro saldo.")
            Operation.objects.create(sequence=seq * 100 + SALDO_LOT, name=f"{st.name} · saldo"[:120],
                                     quantity=Decimal(str(r.pending)), **common)

    if order.status == WorkOrder.Status.TO_PLAN:
        order.status = WorkOrder.Status.DRAFT
        order.save(update_fields=["status"])
    if reopen and lost is not None:  # lo reportado se borró con las operaciones: el pedido arranca de cero
        order.quantity_done = Decimal("0")
        if order.status in (WorkOrder.Status.IN_PROGRESS, WorkOrder.Status.DONE):
            order.status = WorkOrder.Status.RELEASED
        elif order.status == WorkOrder.Status.CANCELLED:  # se reactiva (al programarse queda planificada)
            order.status = WorkOrder.Status.DRAFT
        order.save(update_fields=["quantity_done", "status"])
    refresh_order_dates(order)
    end = timezone.localtime(summary["end"])
    active = [(st, r) for st, r in zip(stages, results) if not st.done]
    head = (f"{order.number}: re-planeó lo pendiente de {len(active)} etapa(s)" if replan
            else f"{order.number}: reabrió y re-planeó todo ({len(stages)} etapa(s); se borraron {lost['reports']} "
                 f"reporte(s), {lost['good']:g} buenas)" if lost and lost["reports"]
            else f"{order.number}: reabrió y re-planeó todo ({len(stages)} etapa(s))" if lost
            else f"{order.number}: {len(stages)} etapa(s)")
    ScheduleChange.objects.create(
        site=order.site, action=ScheduleChange.Action.PLAN,
        description=f"{head}, {summary['man_hours']:.1f} HH, "
                    f"hasta {summary['max_crew']} operarios, fin {end:%d/%m %H:%M}"
                    + (" · cuadrillas por rearmar" if had_crew else ""),
        user=user if user and user.is_authenticated else None,
    )
    return order


def initial_stages(order: WorkOrder) -> list[dict]:
    """Etapas guardadas, o la ruta estándar del tipo de servicio como punto de partida."""
    saved = list(order.plan_stages.select_related("work_center").order_by("sequence"))
    if saved:
        manual = {}
        for o in order.operations.filter(supervisor__isnull=False, supervisor_auto=False).select_related("stage"):
            manual.setdefault(o.stage_id, o.supervisor_id)
        ot: dict[str, list] = {}
        for exc in CalendarException.objects.filter(order=order).order_by("date", "start_time"):
            ot.setdefault(exc.stage_name, []).append({
                "kind": "ot" if exc.kind == CalendarException.Kind.OVERTIME else "extra", "date": exc.date.isoformat(),
                "shift": exc.shift_id, "start": exc.start_time.strftime("%H:%M"), "end": exc.end_time.strftime("%H:%M")})
        return [{
            "name": s.name, "line": s.work_center_id, "quantity": float(s.quantity), "std_value": float(s.std_value),
            "std_unit": s.std_unit, "setup_minutes": float(s.setup_minutes), "mode": s.mode, "mode_param": s.mode_param,
            "qty_mode": s.qty_mode, "qty_param": float(s.qty_param) if s.qty_param is not None else None,
            "supervisor": s.supervisor_id or manual.get(s.pk) or "",
            "efficiency": s.efficiency or "", "overtime": ot.get(s.name, []),
            "link": s.link, "link_lag": float(s.link_lag) if s.link_lag is not None else "",
            "report_mode": s.report_mode, "split_mode": s.split_mode,
        } for s in saved]
    pt = registry.get_process_type(order.process_type)
    if pt is None:
        return []
    lines = list(WorkCenter.objects.filter(site=order.site, active=True))
    stages = []
    for tpl in pt.operations:
        # La línea de su mismo tipo; si la planta tiene una sola línea, esa. Si no, queda sin línea y la pantalla lo marca.
        line = next((wc for wc in lines if wc.kind == tpl.work_center_kind), None) or (
            lines[0] if len(lines) == 1 else None)
        stages.append({
            "name": tpl.name, "line": line.pk if line else None, "quantity": float(order.quantity),
            "std_value": tpl.minutes_per_unit, "std_unit": PlanStage.StdUnit.MIN_PER_UNIT,
            "setup_minutes": tpl.setup_minutes, "mode": PlanStage.Mode.SHIFT, "mode_param": None,
            "qty_mode": PlanStage.QtyMode.ALL, "qty_param": None, "supervisor": "", "efficiency": "", "overtime": [],
            "link": PlanStage.Link.QUEUE, "link_lag": "", "report_mode": "", "split_mode": PlanStage.SplitMode.TIME,
        })
    return stages


# ---------------------------------------------------------------- horas extra sugeridas para cumplir
def _es(x: float, d: int = 1) -> str:
    """Número con coma decimal (1,1)."""
    return f"{x:.{d}f}".replace(".", ",")


def _g(x: float) -> str:
    return f"{x:g}".replace(".", ",")


def _quarter(h: float) -> float:
    return math.ceil(h * 4 - 1e-9) / 4


def _add_hours(hhmm: str, hours: float) -> str:
    t = datetime.combine(date(2000, 1, 1), time.fromisoformat(hhmm)) + timedelta(hours=hours)
    return t.strftime("%H:%M")


def _ot_window(item: dict) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    d = date.fromisoformat(item["date"])
    a, b = time.fromisoformat(item["start"]), time.fromisoformat(item["end"])
    s0 = datetime.combine(d, a, tzinfo=tz)
    e0 = datetime.combine(d + timedelta(days=1) if b <= a else d, b, tzinfo=tz)
    return s0, e0


def overtime_options(order: WorkOrder, raw: list[dict], start: datetime, respect_operators: bool = True,
                     replan: bool = False) -> dict:
    """Opciones de horas extra para terminar antes del compromiso, cada una verificada recalculando el plan.

    Se trabaja sobre todo el pedido: las horas extra se ponen en las líneas de las etapas (las etapas van en
    cadena, alargar una sola no adelanta a las siguientes). Opciones:
      * Alargar solo el último turno antes del compromiso (p. ej. 2,1 turnos → un turno de 1,1 y uno normal).
      * Repartir: la misma cantidad al final de cada turno hasta el compromiso (menos cansancio por persona).
      * Turno adicional completo en un hueco sin trabajo (noche, fin de semana, festivo).
    Se busca la menor cantidad (en cuartos de hora), se quitan las horas extra que no se usan y al final se suma el
    margen por el menor rendimiento en horas extra (la gente cansada rinde menos)."""
    stages = parse_stages(order, raw, replan)
    if not order.due_date:
        return {"needed": False, "error": "El pedido no tiene fecha compromiso: no hay contra qué medir."}
    rules = PlanningRules.objects.filter(site=order.site).first()
    ot_factor = (rules.overtime_efficiency if rules else 90) / 100
    due = order.due_date
    site_sh = site_shifts(order.site)

    def run(extra_items):
        trial = []
        for i, st in enumerate(stages):
            mine = [{k: v for k, v in it.items() if k not in ("stage", "label", "at")} for it in extra_items if it["stage"] == i]
            if mine:
                st = copy.copy(st)
                st.overtime = st.overtime + mine
            trial.append(st)
        results, summary, _ = _simulate(order, trial, start, respect_operators)
        return results, summary

    results, summary = run([])
    end = summary["end"]
    base_slack = (due - end).total_seconds() / 3600
    head = {"due": _iso(due), "end": _iso(end), "slack_hours": round(base_slack, 2), "ot_efficiency": round(ot_factor * 100)}
    if end <= due:
        return {**head, "needed": False, "options": []}
    plan_start = max(start, min(r.schedule[0][0] for st, r in zip(stages, results) if not st.done))

    # Contexto: la etapa más larga (turnos que necesita frente a los que caben antes del compromiso)
    big = max(range(len(stages)), key=lambda i: results[i].duration_hours)
    rb, sb = results[big], stages[big]
    ctx = ""
    if rb.shift_hours:
        avail = working_minutes(center_shifts(sb.line, site_sh), rb.schedule[0][0], due) / 60 / rb.shift_hours
        ctx = (f"«{sb.name}» necesita {_es(rb.duration_hours / rb.shift_hours)} turnos y antes del compromiso caben "
               f"{_es(max(0.0, avail))}. ")
    ctx += f"El pedido termina {_es(-base_slack)} h tarde."

    # Puntos para alargar, por línea (una línea con varias etapas se asigna a su primera etapa)
    by_line: dict[int, tuple[int, list[dict]]] = {}
    for i, st in enumerate(stages):
        if st.line.pk in by_line or st.done:
            continue
        pts = [p for p in extension_points(order, st.line, plan_start, due, limit=30)
               if datetime.fromisoformat(p["at"]) < due]
        by_line[st.line.pk] = (i, pts)

    def items_for(points_by_line, h):
        out = []
        for _, (i, pts) in points_by_line.items():
            for p in pts:
                take = min(h, p["max_hours"], 6.0)
                if take < 0.25:
                    continue
                out.append({"stage": i, "kind": "ot", "date": p["date"], "shift": p["shift"], "start": p["start"],
                            "end": _add_hours(p["start"], take), "label": p["label"]})
        return out

    def prune(items, res):
        """Quita las horas extra que ninguna etapa de esa línea usa y recorta cada tramo hasta donde se usa."""
        keep = []
        for it in items:
            w0, w1 = _ot_window(it)
            line = stages[it["stage"]].line.pk
            ends = [min(e0, w1) for j, st in enumerate(stages) if st.line.pk == line and not st.done
                    for s0, e0 in res[j].schedule if s0 < w1 and e0 > w0]
            if not ends:
                continue
            if it["kind"] == "ot":
                used = _quarter((max(ends) - w0).total_seconds() / 3600)
                it = {**it, "end": _add_hours(it["start"], max(0.25, min(used, overtime_hours(it))))}
            keep.append(it)
        return keep

    options, seen = [], set()
    grid = [0.25 * k for k in range(1, 25)]  # hasta 6 h

    def finish(kind, title_fn, items, detail):
        """Recorta lo que no se usa, suma el margen por cansancio y verifica."""
        if not items:
            return
        res, summ = run(items)
        if summ["end"] > due:
            return
        pruned = prune(items, res)
        if pruned and pruned != items:
            r2, s2 = run(pruned)
            if s2["end"] <= due:
                items, res, summ = pruned, r2, s2
        if ot_factor < 1:  # margen por cansancio: cada tramo se alarga en proporción
            tired = []
            for it in items:
                h = overtime_hours(it)
                need = max(0.25, h - max(0.0, (due - summ["end"]).total_seconds() / 3600) / len(items))
                h2 = _quarter(need / ot_factor)
                cap = next((p["max_hours"] for _, (_, pts) in by_line.items() for p in pts
                            if p["date"] == it["date"] and p["start"] == it["start"] and p["shift"] == it.get("shift")), 6.0)
                tired.append({**it, "end": _add_hours(it["start"], min(max(h, h2), cap, 6.0) if it["kind"] == "ot" else h)})
            r3, s3 = run(tired)
            if s3["end"] <= due:
                items, res, summ = tired, r3, s3
        key = tuple(sorted((it["stage"], it["date"], it["start"], it["end"]) for it in items))
        if key in seen:
            return
        seen.add(key)
        hours = sum(overtime_hours(it) for it in items)
        mh = sum(overtime_hours(it) * res[it["stage"]].crew for it in items)
        slack = (due - summ["end"]).total_seconds() / 3600
        note = f" Incluye el margen por rendimiento en horas extra ({round(ot_factor * 100)} %)." if ot_factor < 1 and kind != "turno" else ""
        options.append({"kind": kind, "title": title_fn(items), "detail": detail + note, "items": items,
                        "stage": items[0]["stage"], "stage_name": stages[items[0]["stage"]].name,
                        "hours": round(hours, 2), "man_hours": round(mh, 1), "crew": max(res[it["stage"]].crew for it in items),
                        "lines": len({stages[it["stage"]].line.pk for it in items}),
                        "end": _iso(summ["end"]), "slack_hours": round(slack, 2), "meets": True})

    def search(points_by_line):
        """Menor h (en cuartos de hora) con la que el pedido cumple."""
        lo, hi, best = 0, len(grid) - 1, None
        if not items_for(points_by_line, grid[hi]):
            return None
        _, summ = run(items_for(points_by_line, grid[hi]))
        if summ["end"] > due:
            return None
        while lo <= hi:
            mid = (lo + hi) // 2
            _, summ = run(items_for(points_by_line, grid[mid]))
            if summ["end"] <= due:
                best, hi = grid[mid], mid - 1
            else:
                lo = mid + 1
        return best

    def one_title(items):
        if len(items) == 1:
            it = items[0]
            return f"Alargar 1 turno: {it['label'].split(',')[0]} hasta las {it['end']} (+{_g(overtime_hours(it))} h)"
        n_lines = len({stages[it["stage"]].line.pk for it in items})
        return (f"Alargar el último turno antes del compromiso en {n_lines} línea{'s' if n_lines > 1 else ''} "
                f"({', '.join(f'{stages[i['stage']].line.code} +{_g(overtime_hours(i))} h' for i in items)})")

    # A) solo el último turno de cada línea antes del compromiso
    last_pts = {k: (i, pts[-1:]) for k, (i, pts) in by_line.items() if pts}
    h = search(last_pts) if last_pts else None
    if h:
        finish("alargar", one_title, items_for(last_pts, h),
               f"{ctx} Un turno queda más largo y los demás normales.")
    # B) repartir en todos los turnos hasta el compromiso
    if any(len(pts) > 1 for _, (_, pts) in by_line.items()):
        h = search(by_line)
        if h:
            finish("repartir", lambda its: f"Repartir: hasta +{_g(max(overtime_hours(i) for i in its))} h al final de {len(its)} turnos",
                   items_for(by_line, h), f"{ctx} Menos cansancio por persona que alargar un solo turno.")
    # C) turno adicional completo en las líneas del pedido
    gaps = _gap_shifts(order, next(st for st in stages if not st.done).line, plan_start, due, limit=3)
    for g in gaps:
        items = [{"stage": i, "kind": "extra", "date": g["date"], "shift": None, "start": g["start"], "end": g["end"],
                  "label": g["label"]} for _, (i, _) in by_line.items()]
        res, summ = run(items)
        if summ["end"] <= due:
            finish("turno", lambda its: f"{its[0]['label']}" + (f" en {len(its)} líneas" if len(its) > 1 else ""),
                   items, f"{ctx} Turnos normales completos más un turno extra entero (queda holgura).")
            break

    options.sort(key=lambda o: (o["hours"], -o["slack_hours"]))
    if not options:
        msg = ("Ni con 6 h extra por turno ni con un turno adicional se alcanza el compromiso: sube operarios, "
               "adelanta el inicio o negocia la fecha.")
        maint = (Downtime.objects.filter(work_center__in=[st.line for st in stages], start__lt=due, end__gt=plan_start)
                 .select_related("work_center").order_by("start").first())
        if maint:
            msg += (f" Ojo: {maint.work_center.code} tiene «{maint.reason}» el {DOW[_local(maint.start).weekday()]} "
                    f"{_local(maint.start):%d/%m %H:%M}–{_local(maint.end):%H:%M}; una operación no atraviesa un mantenimiento.")
        return {**head, "needed": True, "late_hours": round(-base_slack, 2), "options": [], "best": None, "message": msg}
    return {**head, "needed": True, "late_hours": round(-base_slack, 2), "options": options, "best": 0}


# ---------------------------------------------------------------- rendimiento medido
def measured_efficiency(wc: WorkCenter, days: int = 30) -> dict | None:
    """Rendimiento real de la línea en los últimos días: minutos estándar producidos ÷ minutos-hombre trabajados.

    Usa las operaciones con inicio real y producción reportada; el estándar sale de la etapa planeada (sin rendimiento)
    o, si no hay etapa, de los minutos por unidad de la operación. None si hay menos de 1 hora-hombre de datos."""
    since = timezone.now() - timedelta(days=days)
    ops = (Operation.objects.filter(work_center=wc, actual_start__gte=since)
           .exclude(qty_good=0, qty_scrap=0).select_related("stage"))
    shifts = center_shifts(wc)
    earned = spent = 0.0
    n = 0
    for op in ops:
        if op.stage_id and op.stage.std_value:
            std = minutes_per_unit(float(op.stage.std_value), op.stage.std_unit)
        else:
            std = float(op.minutes_per_unit)
        if std <= 0:
            continue
        stop = op.actual_end or timezone.now()
        worked = working_minutes(shifts, op.actual_start, stop) * max(1, op.crew)
        if worked <= 0:
            continue
        earned += float(op.qty_good + op.qty_scrap) * std
        spent += worked
        n += 1
    if spent < 60:
        return None
    return {"pct": round(min(150.0, earned / spent * 100)), "ops": n, "days": days}
