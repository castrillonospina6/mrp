"""Motor de calendario por turnos y operaciones del programador visual.

Conceptos:
  * Un ``Shift`` define una ventana diaria. Si cruza medianoche pertenece al día en que termina.
  * Las ventanas laborables de un centro = sus turnos (o todos los de la planta si no tiene).
  * ``fit`` consume minutos de trabajo solo dentro de esas ventanas.
  * Un *slot* es una columna del calendario: (día, turno) en vista semanal/diaria, o un día en la mensual.
"""

from __future__ import annotations

import calendar
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Downtime, Operation, PlanStage, ScheduleChange, Shift, WorkCenter, WorkOrder
from .workcal import get_calendar

Interval = tuple[datetime, datetime]
_REF_DAY = date(2000, 1, 10)


def local_tz():
    return timezone.get_current_timezone()


def _at(day: date, t: time, tz) -> datetime:
    return datetime.combine(day, t, tzinfo=tz)


# --- turnos y ventanas ---------------------------------------------------------------

def shift_window(shift: Shift, day: date, tz=None) -> Interval:
    tz = tz or local_tz()
    if shift.end_time == time(0):  # termina a medianoche: pertenece al día en que empieza (15:00–00:00)
        return _at(day, shift.start_time, tz), _at(day + timedelta(days=1), time(0), tz)
    if shift.end_time <= shift.start_time:
        return _at(day - timedelta(days=1), shift.start_time, tz), _at(day, shift.end_time, tz)
    return _at(day, shift.start_time, tz), _at(day, shift.end_time, tz)


def shift_work_windows(shift: Shift, day: date, tz=None) -> list[Interval]:
    """Ventana del turno menos desayuno y almuerzo (los descansos se ubican por su distancia al inicio)."""
    a, b = shift_window(shift, day, tz)
    pieces = [(a, b)]
    for brk_start, brk_end in shift.breaks:
        bs = a + timedelta(minutes=Shift.span_minutes(shift.start_time, brk_start) % (24 * 60))
        be = bs + timedelta(minutes=Shift.span_minutes(brk_start, brk_end))
        cut = []
        for x, y in pieces:
            if be <= x or bs >= y:
                cut.append((x, y))
                continue
            if x < bs:
                cut.append((x, bs))
            if be < y:
                cut.append((be, y))
        pieces = cut
    return pieces


class ShiftList(list):
    """Lista de turnos que recuerda su planta y su línea (para aplicar el calendario de excepciones)."""

    def __init__(self, items=(), site_id: int | None = None, wc_id: int | None = None):
        super().__init__(items)
        self.site_id = site_id
        self.wc_id = wc_id


def sort_shifts(shifts) -> list[Shift]:
    return sorted(shifts, key=lambda s: shift_window(s, _REF_DAY)[0])


def site_shifts(site) -> ShiftList:
    return ShiftList(sort_shifts(site.shifts.filter(active=True)), site_id=site.pk)


def center_shifts(wc: WorkCenter, site_shift_list: list[Shift] | None = None) -> ShiftList:
    own = [s for s in wc.shifts.all() if s.active]
    if own:
        return ShiftList(sort_shifts(own), site_id=wc.site_id, wc_id=wc.pk)
    base = site_shift_list if site_shift_list is not None else site_shifts(wc.site)
    return ShiftList(base, site_id=wc.site_id, wc_id=wc.pk)


def _site_of(shifts) -> int | None:
    sid = getattr(shifts, "site_id", None)
    if sid is None and shifts:
        sid = getattr(shifts[0], "site_id", None)
    return sid


def working_windows(shifts: list[Shift], start: datetime, end: datetime) -> list[Interval]:
    """Ventanas laborables (sin descansos, fusionadas y recortadas) dentro de [start, end). Sin turnos = 24 h.

    Aplica el calendario de la planta: días laborables, festivos, cierres, horas extra y turnos adicionales."""
    if start >= end:
        return []
    if not shifts:
        return [(start, end)]
    cal = get_calendar(_site_of(shifts))
    return cal.windows(shifts, start, end, getattr(shifts, "wc_id", None))


def fit(start: datetime, minutes: float, shifts: list[Shift], horizon_days: int = 370) -> Interval:
    """(inicio, fin) consumiendo ``minutes`` de trabajo solo dentro de los turnos."""
    if not shifts:
        return start, start + timedelta(minutes=minutes)
    remaining = timedelta(minutes=max(0.0, minutes))
    begin = None
    chunk = start
    limit = start + timedelta(days=horizon_days)
    while chunk < limit:
        chunk_end = chunk + timedelta(days=14)
        for a, b in working_windows(shifts, chunk, chunk_end):
            if begin is None:
                begin = a
            if remaining <= b - a:
                return begin, a + remaining
            remaining -= b - a
        chunk = chunk_end
    raise ValidationError("Los turnos configurados no tienen tiempo laborable.")


def fit_avoiding(start: datetime, minutes: float, shifts: list[Shift], blocked: list[Interval]) -> Interval:
    """Como ``fit`` pero sin pisar paros de mantenimiento (se corre hasta después del paro)."""
    s, e = fit(start, minutes, shifts)
    for _ in range(200):
        clash = next(((a, b) for a, b in blocked if a < e and b > s), None)
        if clash is None:
            return s, e
        s, e = fit(clash[1], minutes, shifts)
    return s, e


def fit_ending_after(start: datetime, minutes: float, shifts: list[Shift], blocked: list[Interval],
                     min_end: datetime | None) -> Interval:
    """El hueco más temprano desde ``start`` que no termine antes de ``min_end`` (etapas «a la par»: no pueden
    terminar antes de recibir la última unidad de la anterior). Busca el inicio por bisección, al minuto."""
    s, e = fit_avoiding(start, minutes, shifts, blocked)
    if min_end is None or e >= min_end:
        return s, e
    at = lambda n: start + timedelta(minutes=n)  # noqa: E731
    lo, hi = 0, max(1, math.ceil((min_end - start).total_seconds() / 60))
    if fit_avoiding(at(hi), minutes, shifts, blocked)[1] < min_end:  # no debería pasar: el fin crece con el inicio
        return fit_avoiding(at(hi), minutes, shifts, blocked)
    while hi - lo > 1:  # el menor n (minutos después de start) con el que se termina en o después de min_end
        mid = (lo + hi) // 2
        if fit_avoiding(at(mid), minutes, shifts, blocked)[1] >= min_end:
            hi = mid
        else:
            lo = mid
    return fit_avoiding(at(hi), minutes, shifts, blocked)


def working_minutes(shifts: list[Shift], start: datetime, end: datetime) -> float:
    return sum((b - a).total_seconds() for a, b in working_windows(shifts, start, end)) / 60


def remaining_minutes(op: Operation) -> float:
    qty = op.qty
    if op.status == Operation.Status.IN_PROGRESS and qty:
        done_ratio = min(1.0, float(op.qty_good / qty))
        # el alistamiento ya se hizo; el trabajo se reparte entre la cuadrilla
        return float(op.minutes_per_unit * qty) / max(1, op.crew) * (1 - done_ratio)
    return op.planned_minutes


def effective_hours_per_shift(shifts: list[Shift]) -> float:
    """Horas hábiles promedio por turno (sin descansos). Sin turnos = 24 h."""
    if not shifts:
        return 24.0
    return sum(s.effective_minutes for s in shifts) / len(shifts) / 60


def base_shifts(shifts: list[Shift]) -> list[Shift]:
    """Turnos regulares (sin los de traslape); si solo hay traslapes, todos."""
    return [s for s in shifts if not getattr(s, "is_overlap", False)] or list(shifts)


def daily_work_hours(shifts: list[Shift]) -> float:
    """Horas hábiles de un día típico con estos turnos. Es la UNIÓN de sus ventanas: un traslape que se cruza con
    otro turno solo suma las horas que este no cubre (p. ej. el almuerzo de la mañana). Sin turnos = 24 h."""
    if not shifts:
        return 24.0
    from .workcal import merge

    tz = local_tz()
    a = _at(_REF_DAY, time(0), tz)
    b = a + timedelta(days=1)
    wins = [w for s in shifts for d in (_REF_DAY - timedelta(days=1), _REF_DAY, _REF_DAY + timedelta(days=1))
            for w in shift_work_windows(s, d, tz)]
    return sum(max(0.0, (min(y, b) - max(x, a)).total_seconds()) for x, y in merge(wins)) / 3600


def operators_available(shifts: list[Shift], site_total: int = 0, day: date | None = None) -> int | None:
    """Operarios disponibles por turno (el menor de los turnos) el día indicado, según el reparto de la bolsa global
    de personal. Un turno sin número usa el total de la planta. None = sin límite."""
    if shifts:
        cal = get_calendar(_site_of(shifts))
        d = day or timezone.localdate()
        limits = [cal.people(s, d) or site_total for s in shifts]
    else:
        limits = [site_total]
    limits = [x for x in limits if x]
    return min(limits) if limits else None


class OperatorLedger:
    """Bolsa de operarios de una planta a lo largo del tiempo.

    Disponibles en t = operarios del turno que cubre t (o el total de la planta si el turno no tiene número).
    En uso en t    = suma de cuadrillas de las operaciones programadas que están activas en t.
    Libres en t    = disponibles − en uso. Cuando una operación termina, su cuadrilla se libera.
    """

    def __init__(self, site, shifts: list[Shift] | None = None, *, exclude_order_id=None, since: datetime | None = None):
        self.shifts = shifts if shifts is not None else site_shifts(site)
        self.cal = get_calendar(site.pk)
        self.total = site.operators_total or 0
        self.unlimited = not self.total and not any(s.operators for s in self.shifts) \
            and not any(x.operators for x in self.cal.extras) and not self.cal.has_allocations
        qs = (Operation.objects.filter(order__site=site, planned_start__isnull=False, planned_end__isnull=False)
              .exclude(status=Operation.Status.DONE).exclude(order__status=WorkOrder.Status.CANCELLED)
              .select_related("order"))
        if exclude_order_id:
            qs = qs.exclude(order_id=exclude_order_id)
        if since:
            qs = qs.filter(planned_end__gt=since)
        self.items: list[tuple[datetime, datetime, int, str]] = [
            (o.planned_start, o.planned_end, max(1, o.crew), f"{o.order.number} · {o.name}") for o in qs]

    def add(self, start: datetime, end: datetime, crew: int, label: str) -> None:
        self.items.append((start, end, max(1, crew), label))

    def _shift_at(self, t: datetime):
        return self.cal.slot_at(self.shifts, t)

    def available_at(self, t: datetime) -> int | None:
        if self.unlimited:
            return None
        sh = self._shift_at(t) if (self.shifts or self.cal.extras) else None
        return (sh.operators or self.total) if sh else self.total

    def used_at(self, t: datetime) -> int:
        return sum(c for s, e, c, _ in self.items if s <= t < e)

    def free_at(self, t: datetime) -> int | None:
        avail = self.available_at(t)
        return None if avail is None else avail - self.used_at(t)

    def is_working(self, t: datetime) -> bool:
        return (not self.shifts and not self.cal.extras) or self._shift_at(t) is not None

    def min_free(self, start: datetime, end: datetime) -> tuple[int | None, datetime | None]:
        """Menor cantidad de operarios libres dentro de [start, end) (solo en horario de turno) y cuándo ocurre."""
        if self.unlimited or start >= end:
            return None, None
        points = {start} | {x for a, b, _, _ in self.items for x in (a, b) if start < x < end}
        points |= self.cal.boundaries(self.shifts, start, end)  # los cambios de turno cambian la disponibilidad
        best = (None, None)
        for p in sorted(points):
            if not self.is_working(p):
                continue
            free = self.free_at(p)
            if best[0] is None or free < best[0]:
                best = (free, p)
        return best

    def next_release(self, t: datetime) -> datetime | None:
        """Próximo momento después de t en que termina alguna operación activa en t."""
        ends = [e for s, e, _, _ in self.items if s <= t < e]
        return min(ends) if ends else None

    def releases(self, after: datetime, until: datetime, limit: int = 8) -> list[dict]:
        """Liberaciones de cuadrillas entre after y until, agrupadas por hora, con los libres que quedan."""
        grouped: dict[datetime, list] = {}
        for s, e, c, label in self.items:
            if after < e <= until:
                grouped.setdefault(e, []).append((c, label))
        out = []
        for t in sorted(grouped)[:limit]:
            out.append({"at": t, "released": sum(c for c, _ in grouped[t]),
                        "labels": [lbl for _, lbl in grouped[t]], "free_after": self.free_at(t)})
        return out


# --- columnas del calendario ---------------------------------------------------------

@dataclass
class Slot:
    start: datetime
    end: datetime
    day: date
    label: str
    color: str
    off: str = ""        # motivo si no se trabaja (no laboral, festivo, cierre)
    note: str = ""       # horas extra / turno adicional
    extra: bool = False


VIEWS = ("semana", "dia", "mes")


def view_days(view: str, anchor: date) -> list[date]:
    if view == "dia":
        return [anchor]
    if view == "mes":
        first = anchor.replace(day=1)
        return [first + timedelta(days=i) for i in range(calendar.monthrange(first.year, first.month)[1])]
    monday = anchor - timedelta(days=anchor.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def build_slots(shifts: list[Shift], view: str, days: list[date], site_id: int | None = None) -> list[Slot]:
    tz = local_tz()
    cal = get_calendar(site_id if site_id is not None else _site_of(shifts))
    slots: list[Slot] = []
    for d in days:
        if view == "mes" or not shifts:
            if shifts:
                wins = [shift_window(s, d, tz) for s in shifts]
                a, b = min(w[0] for w in wins), max(w[1] for w in wins)
            else:
                a = _at(d, time(0), tz)
                b = a + timedelta(days=1)
            working = cal.windows(shifts, _at(d, time(0), tz), _at(d + timedelta(days=1), time(0), tz)) if shifts else [(a, b)]
            reason = ""
            if not working:
                h = cal.holiday(d)
                reason = f"Festivo · {h}" if h else "No laboral"
            notes = [x.label for x in cal.extras if x.day == d]
            slots.append(Slot(a, b, d, str(d.day), "", reason, " · ".join(notes)))
        else:
            day_slots = []
            for s in shifts:
                a, b = shift_window(s, d, tz)
                reason = cal.shift_off_reason(s, d)
                note = ""
                for x in cal.overtime_for(s, d):
                    a, b = min(a, x.start), max(b, x.end)
                    hours = (x.end - x.start).total_seconds() / 3600
                    note = f"+{hours:g} h extra"
                    reason = ""  # las horas extra se trabajan aunque el día no sea laboral
                day_slots.append(Slot(a, b, d, s.name, s.color, reason, note))
            for x in cal.extra_shifts_on(d, any_line=True):
                day_slots.append(Slot(x.start, x.end, d, x.label, "#6b7280", "", "Turno adicional", True))
            day_slots.sort(key=lambda sl: sl.start)
            slots.extend(day_slots)
    return slots


def shift_anchor(view: str, anchor: date, step: int) -> date:
    if view == "dia":
        return anchor + timedelta(days=step)
    if view == "mes":
        month = anchor.month - 1 + step
        year = anchor.year + month // 12
        return date(year, month % 12 + 1, 1)
    return anchor + timedelta(days=7 * step)


# --- conflictos ----------------------------------------------------------------------

def detect_conflicts(ops: list[Operation], downtimes: list[Downtime]) -> dict[int, set[str]]:
    """``solape`` (supera la capacidad de la línea), ``secuencia`` (empieza antes de que termine
    la operación anterior de la misma orden) y ``mantenimiento`` (pisa un paro)."""
    conflicts: dict[int, set[str]] = defaultdict(set)
    scheduled = [o for o in ops if o.planned_start and o.planned_end and o.status != Operation.Status.DONE]

    by_wc: dict[int, list[Operation]] = defaultdict(list)
    for o in scheduled:
        if o.work_center_id:
            by_wc[o.work_center_id].append(o)
    for items in by_wc.values():
        capacity = items[0].work_center.capacity
        events = sorted(
            [(o.planned_start, 1, i) for i, o in enumerate(items)] + [(o.planned_end, 0, i) for i, o in enumerate(items)]
        )
        active: set[int] = set()
        for _, kind, i in events:
            if kind == 0:
                active.discard(i)
                continue
            active.add(i)
            if len(active) > capacity:
                for j in active:
                    conflicts[items[j].pk].add("solape")

    dt_by_wc: dict[int, list[Downtime]] = defaultdict(list)
    for d in downtimes:
        dt_by_wc[d.work_center_id].append(d)
    for o in scheduled:
        if any(d.start < o.planned_end and d.end > o.planned_start for d in dt_by_wc.get(o.work_center_id, [])):
            conflicts[o.pk].add("mantenimiento")

    by_order: dict[int, list[Operation]] = defaultdict(list)
    for o in ops:
        by_order[o.order_id].append(o)
    for items in by_order.values():
        for o in items:
            if not o.planned_start or o.status == Operation.Status.DONE:
                continue
            ends = [p.planned_end for p in predecessors(o, items) if p.planned_end]
            if ends and o.planned_start < max(ends):
                conflicts[o.pk].add("secuencia")
            flow = flow_reference(o, items)
            if flow:  # a la par: no puede arrancar antes que la etapa anterior
                starts = [p.planned_start for p in flow[0] if p.planned_start]
                if starts and o.planned_start < min(starts):
                    conflicts[o.pk].add("secuencia")
    return conflicts


def operator_overloads(ops: list[Operation], shifts: list[Shift], site_total: int = 0) -> set[int]:
    """Operaciones que, sumando sus cuadrillas, superan los operarios disponibles del turno (toda la planta)."""
    cal = get_calendar(_site_of(shifts))
    if (not site_total and not any(s.operators for s in shifts) and not any(x.operators for x in cal.extras)
            and not cal.has_allocations):
        return set()

    def limit_at(t: datetime) -> int | None:
        slot = cal.slot_at(shifts, t) if (shifts or cal.extras) else None
        if slot:
            return slot.operators or site_total or None
        return (site_total or None) if not shifts else None  # fuera de turno no se trabaja

    items = [o for o in ops if o.planned_start and o.planned_end and o.status != Operation.Status.DONE]
    events = sorted([(o.planned_start, 1, i) for i, o in enumerate(items)]
                    + [(o.planned_end, 0, i) for i, o in enumerate(items)])
    active: set[int] = set()
    load, over = 0, set()
    for idx, (t, kind, i) in enumerate(events):
        if kind == 0:
            active.discard(i)
            load -= items[i].crew
        else:
            active.add(i)
            load += items[i].crew
        nxt = events[idx + 1][0] if idx + 1 < len(events) else None
        if not active or nxt is None or nxt <= t:
            continue
        for probe in (t, t + (nxt - t) / 2):
            limit = limit_at(probe)
            if limit and load > limit:
                over |= {items[j].pk for j in active}
                break
    return over


SALDO_LOT = 99  # sufijo de secuencia de la operación "saldo" de un parcial


def stage_groups(links: list[str]) -> list[list[int]]:
    """Índices de etapas agrupadas: una etapa «en cola» abre un grupo; «a la par» y «al mismo tiempo» se suman."""
    groups: list[list[int]] = []
    for i, link in enumerate(links):
        if i == 0 or link == "cola":
            groups.append([i])
        else:
            groups[-1].append(i)
    return groups


def _ensure_links(items: list[Operation]) -> None:
    """Carga (una sola consulta) la relación de la etapa de cada operación en op._link / op._link_lag."""
    missing = [x for x in items if x.stage_id and not hasattr(x, "_link")]
    if not missing:
        return
    cached = {x.stage_id: x.stage for x in missing if "stage" in x._state.fields_cache and x.stage}
    need = {x.stage_id for x in missing} - set(cached)
    info = {pk: (link, lag) for pk, link, lag in
            PlanStage.objects.filter(pk__in=need).values_list("pk", "link", "link_lag")} if need else {}
    info.update({pk: (st.link, st.link_lag) for pk, st in cached.items()})
    for x in missing:
        x._link, x._link_lag = info.get(x.stage_id, ("cola", None))


def _stage_layout(items: list[Operation]):
    """Operaciones de etapas planeadas por nº de etapa y grupos de nº de etapa según su relación."""
    staged = [x for x in items if x.stage_id is not None and x.sequence % 100 != SALDO_LOT]
    _ensure_links(staged)
    by_no: dict[int, list[Operation]] = defaultdict(list)
    for x in staged:
        by_no[x.sequence // 100].append(x)
    nos = sorted(by_no)
    groups = [[nos[i] for i in g] for g in stage_groups([by_no[n][0]._link for n in nos])]
    return by_no, nos, groups


def predecessors(op: Operation, items: list[Operation]) -> list[Operation]:
    """Operaciones que deben terminar antes de ``op``.

    Rutas clásicas (sin etapa): la operación anterior por secuencia.
    Etapas planeadas (secuencia = etapa*100 + lote): espera al grupo de etapas anterior. Una etapa «en cola» abre
    un grupo; «a la par» y «al mismo tiempo» comparten el grupo de la anterior (no la esperan a ella, sino a lo
    mismo que la etapa que abre el grupo). Si el grupo anterior es una sola etapa con el mismo número de lotes,
    el lote i espera al lote i (flujo por lotes); si no, espera a todo el grupo anterior.
    """
    if op.split_of_id or any(x.split_of_id for x in items):
        roots = [x for x in items if not x.split_of_id]
        root = next((x for x in roots if x.pk == op.split_of_id), op) if op.split_of_id else op
        preds = predecessors(root, roots)
        ids = {p.pk for p in preds}
        return preds + [x for x in items if x.split_of_id in ids]
    if op.stage_id is None:
        prev = [x for x in items if x.stage_id is None and x.sequence < op.sequence]
        return [max(prev, key=lambda x: x.sequence)] if prev else []
    stage_no, lot = divmod(op.sequence, 100)
    by_no, nos, groups = _stage_layout(items)
    gi = next((k for k, g in enumerate(groups) if stage_no in g), None)
    if gi is not None:
        prev = groups[gi - 1] if gi > 0 else None
    else:  # p. ej. un saldo cuya etapa no tiene otros lotes
        prev = next((g for g in reversed(groups) if max(g) < stage_no), None)
    if not prev:
        return []
    prev_ops = [x for n in prev for x in by_no[n]]
    mine = by_no.get(stage_no, [])
    if lot != SALDO_LOT and len(prev) == 1 and len(prev_ops) == len(mine):
        return [x for x in prev_ops if x.sequence % 100 == lot]
    return prev_ops


def flow_reference(op: Operation, items: list[Operation]):
    """Etapa «a la par»: (operaciones de la etapa anterior, unidades para arrancar). None si no aplica."""
    if op.stage_id is None or op.split_of_id:
        return None
    _ensure_links([op])
    if op._link != "flujo":
        return None
    by_no, nos, _ = _stage_layout(items)
    earlier = [n for n in nos if n < op.sequence // 100]
    if not earlier:
        return None
    return by_no[earlier[-1]], float(op._link_lag or 1)


# --- mutaciones desde el tablero -----------------------------------------------------

def refresh_order_dates(order: WorkOrder) -> None:
    ops = list(order.operations.all())
    planned = [o for o in ops if o.planned_start]
    order.planned_start = min((o.planned_start for o in planned), default=None)
    order.planned_end = max((o.planned_end for o in planned), default=None)
    pending = [o for o in ops if o.status != Operation.Status.DONE]
    if order.status in (WorkOrder.Status.DRAFT, WorkOrder.Status.TO_PLAN) and pending and all(o.planned_start for o in pending):
        order.status = WorkOrder.Status.PLANNED
    elif order.status == WorkOrder.Status.PLANNED and not planned:
        order.status = WorkOrder.Status.DRAFT
    order.save(update_fields=["planned_start", "planned_end", "status"])


def _log(site, action, description, user=None):
    ScheduleChange.objects.create(site=site, action=action, description=description[:255],
                                  user=user if user and user.is_authenticated else None)


class FrozenPlanError(ValidationError):
    """La acción toca el horizonte congelado del plan: requiere confirmación (force=True)."""


def frozen_until(site_id: int) -> datetime | None:
    from .models import PlanningRules
    hours = PlanningRules.objects.filter(site_id=site_id).values_list("frozen_hours", flat=True).first()
    return timezone.now() + timedelta(hours=hours) if hours else None


def check_frozen(op: Operation, new_start: datetime | None, force: bool) -> bool:
    """True si la acción toca el plan congelado (y se forzó). Sin force → FrozenPlanError."""
    limit = frozen_until(op.order.site_id)
    if not limit:
        return False
    hits = (op.planned_start and op.planned_start < limit) or (new_start and new_start < limit)
    if hits and not force:
        hours = round((limit - timezone.now()).total_seconds() / 3600)
        raise FrozenPlanError(
            f"Este cambio toca el plan congelado (próximas {hours} h). ¿Quieres hacerlo de todos modos?")
    return bool(hits)


def _editable(op: Operation) -> None:
    if op.status == Operation.Status.DONE:
        raise ValidationError("La operación ya está terminada.")
    if op.order.status not in WorkOrder.OPEN_STATUSES:
        raise ValidationError(f"La orden está {op.order.get_status_display().lower()}.")


def earliest_free(wc: WorkCenter, slot_start: datetime, slot_end: datetime, exclude_pk: int) -> datetime:
    """Primer momento del slot con una estación libre (para apilar operaciones dentro del turno)."""
    busy = (
        Operation.objects.filter(work_center=wc, planned_start__lt=slot_end, planned_end__gt=slot_start)
        .exclude(pk=exclude_pk).exclude(status=Operation.Status.DONE).order_by("planned_start")
    )
    stations = [slot_start] * wc.capacity
    for o in busy:
        i = stations.index(min(stations))
        stations[i] = max(stations[i], o.planned_end)
    t = min(stations)
    return t if t < slot_end else slot_start


@transaction.atomic
def place_operation(op: Operation, wc: WorkCenter, slot_start: datetime, slot_end: datetime, user=None,
                    force: bool = False) -> Operation:
    op = Operation.objects.select_for_update().select_related("order", "order__site").get(pk=op.pk)
    _editable(op)
    _check_line(op, wc)
    check_frozen(op, slot_start, force)

    was_scheduled = op.planned_start is not None
    start = earliest_free(wc, slot_start, slot_end, op.pk)
    op.planned_start, op.planned_end = fit(start, remaining_minutes(op), center_shifts(wc))
    op.work_center = wc
    op.save(update_fields=["work_center", "planned_start", "planned_end"])
    refresh_order_dates(op.order)
    local = timezone.localtime(op.planned_start)
    _log(op.order.site, ScheduleChange.Action.MOVE if was_scheduled else ScheduleChange.Action.PLACE,
         f"{op.order.number} · {op.name} → {wc.code} {local:%d/%m %H:%M}", user)
    return op


def _check_line(op: Operation, wc: WorkCenter) -> None:
    if wc.site_id != op.order.site_id:
        raise ValidationError("La línea pertenece a otra planta.")
    if not wc.active:
        raise ValidationError("La línea está inactiva.")
    if op.work_center_kind and wc.kind != op.work_center_kind:
        raise ValidationError(f"«{op.name}» requiere una línea de tipo '{op.work_center_kind}' y {wc.code} es '{wc.kind}'.")


@transaction.atomic
def update_operation(op: Operation, *, wc: WorkCenter, start: datetime, end: datetime, user=None,
                     force: bool = False) -> Operation:
    """Edición explícita desde el panel: línea, inicio y fin exactos (sin recalcular)."""
    op = Operation.objects.select_for_update().select_related("order", "order__site").get(pk=op.pk)
    _editable(op)
    _check_line(op, wc)
    if end <= start:
        raise ValidationError("El fin planificado debe ser posterior al inicio.")
    if op.planned_start != start or op.planned_end != end or op.work_center_id != wc.pk:
        check_frozen(op, start, force)
    was_scheduled = op.planned_start is not None
    op.work_center, op.planned_start, op.planned_end = wc, start, end
    op.save(update_fields=["work_center", "planned_start", "planned_end"])
    refresh_order_dates(op.order)
    s, e = timezone.localtime(start), timezone.localtime(end)
    _log(op.order.site, ScheduleChange.Action.EDIT if was_scheduled else ScheduleChange.Action.PLACE,
         f"{op.order.number} · {op.name} → {wc.code} {s:%d/%m %H:%M}–{e:%d/%m %H:%M}", user)
    return op


@transaction.atomic
def update_order_terms(order: WorkOrder, *, due_date=None, priority=None, user=None) -> WorkOrder:
    """Cambia fecha compromiso y/o prioridad de la orden (``None`` = no tocar)."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    changes, fields = [], []
    if due_date is not None and due_date != order.due_date:
        order.due_date = due_date
        fields.append("due_date")
        changes.append(f"compromiso {timezone.localtime(due_date):%d/%m}")
    if priority is not None and int(priority) != order.priority:
        if not 1 <= int(priority) <= 5:
            raise ValidationError("La prioridad va de 1 a 5.")
        order.priority = int(priority)
        fields.append("priority")
        changes.append(f"prioridad {order.priority}")
    if fields:
        order.save(update_fields=fields)
        _log(order.site, ScheduleChange.Action.EDIT, f"{order.number}: " + ", ".join(changes), user)
    return order


@transaction.atomic
def resize_operation(op: Operation, new_end: datetime, user=None, force: bool = False) -> Operation:
    op = Operation.objects.select_for_update().select_related("order", "order__site").get(pk=op.pk)
    _editable(op)
    check_frozen(op, None, force)
    if not op.planned_start:
        raise ValidationError("La operación no está programada.")
    if new_end <= op.planned_start:
        raise ValidationError("El fin debe quedar después del inicio.")
    op.planned_end = new_end
    op.save(update_fields=["planned_end"])
    refresh_order_dates(op.order)
    _log(op.order.site, ScheduleChange.Action.RESIZE,
         f"{op.order.number} · {op.name} termina {timezone.localtime(new_end):%d/%m %H:%M}", user)
    return op


@transaction.atomic
def unschedule_operation(op: Operation, user=None, force: bool = False) -> Operation:
    op = Operation.objects.select_for_update().select_related("order", "order__site").get(pk=op.pk)
    _editable(op)
    check_frozen(op, None, force)
    if op.status == Operation.Status.IN_PROGRESS:
        raise ValidationError("No se puede devolver al backlog una operación en proceso.")
    op.planned_start = op.planned_end = None
    op.save(update_fields=["planned_start", "planned_end"])
    refresh_order_dates(op.order)
    _log(op.order.site, ScheduleChange.Action.UNSCHEDULE, f"{op.order.number} · {op.name} devuelta al backlog", user)
    return op


def create_downtime(*, work_center: WorkCenter, start: datetime, end: datetime, reason: str, user=None) -> Downtime:
    dt = Downtime(work_center=work_center, start=start, end=end, reason=reason,
                  created_by=user if user and user.is_authenticated else None)
    dt.full_clean()
    dt.save()
    _log(work_center.site, ScheduleChange.Action.DOWNTIME,
         f"{work_center.code}: {reason} {timezone.localtime(start):%d/%m %H:%M}–{timezone.localtime(end):%d/%m %H:%M}", user)
    return dt


def delete_downtime(dt: Downtime, user=None) -> None:
    _log(dt.work_center.site, ScheduleChange.Action.DOWNTIME, f"{dt.work_center.code}: eliminado «{dt.reason}»", user)
    dt.delete()


# --- dividir una operación entre varias líneas ----------------------------------------

@transaction.atomic
def split_operation(op: Operation, parts: list[tuple[WorkCenter, "Decimal"]], user=None, force: bool = False) -> list[Operation]:
    """Reparte la cantidad de una operación entre varias líneas compatibles que trabajan en paralelo.

    ``parts`` = [(línea, cantidad), …]; la primera parte es la operación original. Cada parte se programa en su
    línea desde el inicio actual de la operación (o ahora) en el primer hueco libre."""
    from decimal import Decimal

    op = Operation.objects.select_for_update().select_related("order", "order__site").get(pk=op.pk)
    _editable(op)
    if op.status != Operation.Status.PENDING:
        raise ValidationError("Solo se pueden dividir operaciones que no han empezado.")
    if op.split_of_id:
        raise ValidationError("Esta operación ya es una parte; divide la operación original.")
    if len(parts) < 2:
        raise ValidationError("Indica al menos dos líneas para dividir.")
    total = sum((Decimal(str(q)) for _, q in parts), Decimal("0"))
    if any(Decimal(str(q)) <= 0 for _, q in parts):
        raise ValidationError("Cada parte debe tener una cantidad mayor que cero.")
    existing = list(op.parts.all())
    full = op.qty + sum((p.qty for p in existing), Decimal("0"))
    if abs(total - full) > Decimal("0.001"):
        raise ValidationError(f"Las partes suman {total:g} y la operación tiene {full:g}.")
    for wc, _ in parts:
        _check_line(op, wc)
    check_frozen(op, None, force)
    for p in existing:  # se rehace la división
        p.delete()
    start = op.planned_start or timezone.now()
    max_seq = max(op.order.operations.values_list("sequence", flat=True))
    base_name = op.name.split(" · parte ")[0]
    n = len(parts)
    created = []
    for i, (wc, qty) in enumerate(parts):
        if i == 0:
            part = op
            part.quantity = Decimal(str(qty))
        else:
            max_seq += 1
            part = Operation(order=op.order, stage=op.stage, quantity=Decimal(str(qty)), crew=op.crew, sequence=max_seq,
                             work_center_kind=op.work_center_kind, setup_minutes=op.setup_minutes,
                             minutes_per_unit=op.minutes_per_unit, split_of=op, supervisor_id=op.supervisor_id,
                             supervisor_assigned_by_id=op.supervisor_assigned_by_id,
                             supervisor_assigned_at=op.supervisor_assigned_at, supervisor_auto=op.supervisor_auto)
        part.name = f"{base_name} · parte {i + 1}/{n}"[:120]
        part.work_center = wc
        part.save()
        s0 = earliest_free(wc, start, start + timedelta(days=60), part.pk)
        part.planned_start, part.planned_end = fit(s0, remaining_minutes(part), center_shifts(wc))
        part.save(update_fields=["work_center", "planned_start", "planned_end", "name", "quantity"])
        created.append(part)
    refresh_order_dates(op.order)
    _log(op.order.site, ScheduleChange.Action.SPLIT,
         f"{op.order.number} · {base_name} dividida en {n} líneas: " + ", ".join(p.work_center.code for p in created), user)
    return created


@transaction.atomic
def merge_operation(op: Operation, user=None) -> Operation:
    """Vuelve a juntar en una sola operación las partes de una operación dividida."""
    root = Operation.objects.select_for_update().select_related("order", "order__site").get(pk=op.split_of_id or op.pk)
    _editable(root)
    parts = list(root.parts.all())
    if not parts:
        raise ValidationError("La operación no está dividida.")
    if any(p.status != Operation.Status.PENDING for p in parts) or root.status != Operation.Status.PENDING:
        raise ValidationError("Alguna parte ya empezó; no se puede unir.")
    root.quantity = root.qty + sum((p.qty for p in parts), 0)
    root.name = root.name.split(" · parte ")[0]
    for p in parts:
        p.delete()
    root.save(update_fields=["quantity", "name"])
    if root.planned_start and root.work_center_id:
        root.planned_end = fit(root.planned_start, remaining_minutes(root), center_shifts(root.work_center))[1]
        root.save(update_fields=["planned_end"])
    refresh_order_dates(root.order)
    _log(root.order.site, ScheduleChange.Action.SPLIT, f"{root.order.number} · {root.name}: partes unidas", user)
    return root
