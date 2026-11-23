"""Capacidad de planta: personal, horas disponibles vs. horas requeridas, recursos y carga por línea.

Definiciones (por turno y día):
  Programados          = asistencia.programados, o los operarios del turno, o el total de la planta
  Personal disponible  = programados − ausencias
  Capacidad disponible = personal disponible × horas hábiles del turno (sin desayuno ni almuerzo)
  Horas requeridas     = Σ cuadrilla × horas hábiles de cada operación programada dentro del turno
  Capacidad ocupada    = horas requeridas ÷ capacidad disponible
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.db.models import F, Max, Sum
from django.utils import timezone

from . import workcal
from .models import Employee, Operation, ProductionReport, Shift, ShiftAttendance, Site, WorkCenter, WorkOrder
from .scheduling import center_shifts, local_tz, shift_window, shift_work_windows, site_shifts

DOW = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
DOW_LONG = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]
WEEK_DAYS = 7


def day_label(d: date) -> str:
    return f"{DOW_LONG[d.weekday()]} {d.day} de {MONTHS[d.month - 1]}"


def _overlap_minutes(windows, a: datetime, b: datetime) -> float:
    return sum(max(0.0, (min(y, b) - max(x, a)).total_seconds() / 60) for x, y in windows)


def level(occ: float | None) -> str:
    """Semáforo de ocupación: libre < 85 % ≤ justo ≤ 100 % < sobrecarga."""
    if occ is None:
        return "na"
    if occ > 1.0001:
        return "over"
    if occ >= 0.85:
        return "tight"
    return "ok"


@dataclass
class ShiftCapacity:
    shift: Shift
    day: date
    start: datetime
    end: datetime
    eff_hours: float
    scheduled: int
    absent: int
    unlimited: bool
    notes: str = ""
    override: int | None = None
    off: str = ""                 # motivo si el turno no trabaja ese día (no laboral, festivo, cierre)
    day_hours: float | None = None  # horas hábiles reales del turno ese día (0 si no trabaja)
    ot_hours: float = 0.0         # horas extra del turno ese día
    ot_people_hours: float = 0.0  # horas-hombre que aportan las horas extra
    is_extra: bool = False
    required_h: float = 0.0
    by_order: dict[int, float] = field(default_factory=dict)
    by_line_hh: dict[int, float] = field(default_factory=dict)
    by_line_busy: dict[int, float] = field(default_factory=dict)
    windows: list = field(default_factory=list, repr=False)  # ventanas hábiles del turno ese día

    @property
    def available(self) -> int:
        return max(0, self.scheduled - self.absent)

    @property
    def capacity_h(self) -> float:
        hours = self.eff_hours if self.day_hours is None else self.day_hours
        return self.available * hours + self.ot_people_hours

    @property
    def occupancy(self) -> float | None:
        if self.unlimited:
            return None
        if self.capacity_h <= 0:
            return None if self.required_h <= 0 else math.inf
        return self.required_h / self.capacity_h

    @property
    def balance_h(self) -> float:
        return self.capacity_h - self.required_h

    @property
    def level(self) -> str:
        return level(self.occupancy)

    @property
    def occ_pct(self) -> int | None:
        occ = self.occupancy
        if occ is None:
            return None
        return 999 if math.isinf(occ) else round(occ * 100)


def _people(site: Site, shift: Shift, att: ShiftAttendance | None, day: date | None = None) -> tuple[int, int, bool, str]:
    if att and att.scheduled is not None:
        scheduled = att.scheduled
    else:  # el reparto de la bolsa global de personal (o los operarios del turno / total de la planta)
        scheduled = workcal.get_calendar(site.pk).people(shift, day or timezone.localdate()) or site.operators_total
    unlimited = not scheduled and not (att and att.scheduled is not None)
    return scheduled, (att.absent if att else 0), unlimited, (att.notes if att else "")


def shift_for(shifts: list[Shift], now: datetime) -> tuple[Shift | None, date | None]:
    """Turno en curso (o el próximo) y el día al que pertenece."""
    tz = local_tz()
    base = timezone.localtime(now, tz).date()
    upcoming = None
    for d in (base - timedelta(days=1), base, base + timedelta(days=1), base + timedelta(days=2)):
        for s in shifts:
            a, b = shift_window(s, d, tz)
            if a <= now < b:
                return s, d
            if a > now and (upcoming is None or a < upcoming[2]):
                upcoming = (s, d, a)
    return (upcoming[0], upcoming[1]) if upcoming else (None, None)


def _ops_between(site: Site, start: datetime, end: datetime) -> list[Operation]:
    return list(
        Operation.objects.filter(order__site=site, planned_start__lt=end, planned_end__gt=start)
        .exclude(order__status=WorkOrder.Status.CANCELLED)
        .select_related("order__product", "order__customer", "work_center")
    )


class ExtraShift:
    """Turnos adicionales de un día, mostrados como una fila más en la capacidad."""

    is_extra = True
    color = "#6b7280"
    operators = 0

    def __init__(self, day: date, extras):
        self.day = day
        self.extras = extras
        self.pk = f"x{day:%Y%m%d}"
        self.name = " + ".join(dict.fromkeys(x.label for x in extras)) or "Turno adicional"
        self.start_time = min(timezone.localtime(x.start).time() for x in extras)
        self.end_time = max(timezone.localtime(x.end).time() for x in extras)
        self.effective_minutes = sum((x.end - x.start).total_seconds() / 60 for x in extras)

    def works_on_weekday(self, weekday: int) -> bool:
        return True


def compute_extra(site: Site, day: date, extras, ops: list[Operation]) -> ShiftCapacity:
    pseudo = ExtraShift(day, extras)
    windows = [(x.start, x.end) for x in extras]
    people = sum(x.operators or site.operators_total for x in extras)
    hours = sum((x.end - x.start).total_seconds() / 3600 for x in extras)
    cap = ShiftCapacity(shift=pseudo, day=day, start=min(w[0] for w in windows), end=max(w[1] for w in windows),
                        eff_hours=hours, scheduled=people, absent=0, unlimited=not people, is_extra=True)
    cap.day_hours = hours
    _load(cap, windows, ops)
    return cap


def _shared_minutes(windows, a: datetime, b: datetime, peers, weight: float) -> float:
    """Minutos de [a, b) dentro de ``windows`` ponderados por la parte de la gente que pone este turno cuando otros
    turnos (traslape) trabajan a la misma hora. ``peers`` = [(ventanas, personas), …] de los otros turnos."""
    total = 0.0
    for x, y in windows:
        s, e = max(x, a), min(y, b)
        if e <= s:
            continue
        cuts = {s, e} | {p for wins, _ in peers for w in wins for p in w if s < p < e}
        pts = sorted(cuts)
        for p, q in zip(pts, pts[1:]):
            mid = p + (q - p) / 2
            others = sum(w for wins, w in peers if any(u <= mid < v for u, v in wins))
            total += (q - p).total_seconds() / 60 * (weight / (weight + others) if others else 1.0)
    return total


def _load(cap: ShiftCapacity, windows, ops, peers=None) -> None:
    """Horas-hombre requeridas en el turno. Si otros turnos se cruzan (``peers``), la carga de esas horas se reparte
    entre ellos según su gente, para no contarla dos veces."""
    start, end = min(w[0] for w in windows), max(w[1] for w in windows)
    weight = cap.available or 1
    for op in ops:
        if not (op.planned_start < end and op.planned_end > start):
            continue
        minutes = _overlap_minutes(windows, op.planned_start, op.planned_end)
        if minutes <= 0:
            continue
        shared = _shared_minutes(windows, op.planned_start, op.planned_end, peers, weight) if peers else minutes
        hh = shared * max(1, op.crew) / 60
        cap.required_h += hh
        cap.by_order[op.order_id] = cap.by_order.get(op.order_id, 0.0) + hh
        if op.work_center_id:
            cap.by_line_hh[op.work_center_id] = cap.by_line_hh.get(op.work_center_id, 0.0) + hh
            cap.by_line_busy[op.work_center_id] = cap.by_line_busy.get(op.work_center_id, 0.0) + minutes / 60


def compute_many(site: Site, shifts, days, ops: list[Operation], attendance: dict) -> dict:
    """Capacidad de cada turno × día. Cuando dos turnos se cruzan (traslape), reparte la carga de las horas comunes."""
    cells = {(s.pk, d): compute(site, s, d, ops, attendance, load=False) for s in shifts for d in days}
    for (sid, d), cap in cells.items():
        if not cap.windows:
            continue
        peers = [(c.windows, c.available or 1) for (sid2, d2), c in cells.items()
                 if (sid2, d2) != (sid, d) and c.windows and abs((d2 - d).days) <= 1
                 and any(u < y and v > x for u, v in c.windows for x, y in cap.windows)]
        _load(cap, cap.windows, ops, peers or None)
    return cells


def compute(site: Site, shift: Shift, day: date, ops: list[Operation], attendance: dict,
            load: bool = True) -> ShiftCapacity:
    tz = local_tz()
    cal = workcal.get_calendar(site.pk)
    start, end = shift_window(shift, day, tz)
    off = cal.shift_off_reason(shift, day)
    windows = [] if off else shift_work_windows(shift, day, tz)
    scheduled, absent, unlimited, notes = _people(site, shift, attendance.get((shift.pk, day)), day)
    cap = ShiftCapacity(shift=shift, day=day, start=start, end=end, eff_hours=shift.effective_minutes / 60,
                        scheduled=scheduled, absent=absent, unlimited=unlimited, notes=notes, off=off)
    cap.day_hours = sum((b - a).total_seconds() for a, b in windows) / 3600
    overtime = [x for x in cal.extras if x.kind == "horas_extra" and x.shift_id == shift.pk and x.day == day]
    for x in overtime:
        h = (x.end - x.start).total_seconds() / 3600
        cap.ot_hours += h
        cap.ot_people_hours += h * (x.operators or cap.available)
        windows = windows + [(x.start, x.end)]
        if unlimited and x.operators:
            cap.unlimited = False
    att = attendance.get((shift.pk, day))
    cap.override = att.scheduled if att else None
    cap.windows = windows
    if windows and load:
        _load(cap, windows, ops)
    return cap


def dashboard_data(site: Site, day: date | None = None, shift: Shift | None = None, now: datetime | None = None) -> dict:
    now = now or timezone.now()
    tz = local_tz()
    shifts = site_shifts(site)
    if not shifts:
        return {"site": site, "shifts": [], "no_shifts": True, **_orders_block(site, now)}
    cur_shift, cur_day = shift_for(shifts, now)
    shift = shift or cur_shift or shifts[0]
    day = day or cur_day or timezone.localdate()

    days = [day + timedelta(days=i) for i in range(WEEK_DAYS)]
    first = min(shift_window(s, days[0], tz)[0] for s in shifts)
    last = max(shift_window(s, days[-1], tz)[1] for s in shifts)
    ops = _ops_between(site, first, last)
    attendance = {(a.shift_id, a.day): a for a in
                  ShiftAttendance.objects.filter(shift__in=shifts, day__gte=days[0], day__lte=days[-1])}

    week = compute_many(site, shifts, days, ops, attendance)
    cal = workcal.get_calendar(site.pk)
    extra_cells = {}
    for d in days:
        ex = cal.extra_shifts_on(d, any_line=True)
        if ex:
            extra_cells[d] = compute_extra(site, d, ex, ops)
    sel = week[(shift.pk, day)]

    # Carga por línea en el turno elegido
    lines = []
    for wc in WorkCenter.objects.filter(site=site, active=True).prefetch_related("shifts").order_by("code"):
        works = any(s.pk == shift.pk for s in center_shifts(wc, shifts))
        line_hours = wc.capacity * ((sel.day_hours or 0) + sel.ot_hours) if works else 0
        busy = sel.by_line_busy.get(wc.pk, 0.0)
        occ = busy / line_hours if line_hours else (math.inf if busy else None)
        lines.append({
            "wc": wc, "works": works, "hh": sel.by_line_hh.get(wc.pk, 0.0), "busy": busy, "line_hours": line_hours,
            "occ_pct": None if occ is None else (999 if math.isinf(occ) else round(occ * 100)), "level": level(occ),
            "bar": 0 if occ is None else min(100, round(occ * 100)) if not math.isinf(occ) else 100,
        })
    lines.sort(key=lambda x: -(x["occ_pct"] or -1))

    # Recursos (mercancía / insumos) ponderados por las horas de cada orden en el turno
    orders = {o.pk: o for o in WorkOrder.objects.filter(pk__in=sel.by_order).select_related("product", "customer")}
    weight = sum(sel.by_order.values())
    goods = sum(h * orders[o].goods_pct for o, h in sel.by_order.items()) / weight if weight else None
    supplies = sum(h * orders[o].supplies_pct for o, h in sel.by_order.items()) / weight if weight else None
    resource_rows = sorted(
        ({"order": orders[o], "hours": h} for o, h in sel.by_order.items()),
        key=lambda r: (min(r["order"].goods_pct, r["order"].supplies_pct) >= 100, -r["hours"]))

    # Sugerencias para decidir
    suggestions = []
    if sel.off:
        suggestions.append(f"{sel.shift.name} no trabaja este día ({sel.off}). Para producir, programa horas extra o un turno adicional en el Calendario laboral.")
    if sel.balance_h < -0.05 and not sel.unlimited and not sel.off:
        need = math.ceil(-sel.balance_h / sel.eff_hours) if sel.eff_hours else 0
        suggestions.append(f"Faltan {fmt_h(-sel.balance_h)}: equivale a {need} operario{'s' if need != 1 else ''} más en este turno.")
        if sel.absent:
            cover = min(sel.absent, need)
            suggestions.append(f"Cubrir {cover} de las {sel.absent} ausencias recupera {fmt_h(cover * sel.eff_hours)}.")
        slack = [c for c in week.values() if c.start >= sel.end and not c.unlimited and c.balance_h > 0.5]
        slack.sort(key=lambda c: c.start)
        for c in slack[:3]:
            suggestions.append(f"Mover carga a {c.shift.name} {DOW[c.day.weekday()]} {c.day:%d/%m}: holgura de {fmt_h(c.balance_h)}.")
        if not slack:
            suggestions.append("No hay turnos con holgura en los próximos 7 días: considera horas extra o un turno adicional.")
    if goods is not None and goods < 100:
        n = sum(1 for r in resource_rows if r["order"].goods_pct < 100)
        suggestions.append(f"{n} {'orden' if n == 1 else 'órdenes'} del turno sin toda la mercancía: confírmala antes de arrancar.")
    if supplies is not None and supplies < 100:
        n = sum(1 for r in resource_rows if r["order"].supplies_pct < 100)
        suggestions.append(f"{n} {'orden' if n == 1 else 'órdenes'} del turno con insumos incompletos.")

    grid = [{"shift": s, "cells": [week[(s.pk, d)] for d in days]} for s in shifts]
    if extra_cells:
        grid.append({"shift": ExtraShift(days[0], [x for c in extra_cells.values() for x in c.shift.extras]),
                     "extra": True, "cells": [extra_cells.get(d) for d in days]})
    all_cells = list(week.values()) + list(extra_cells.values())
    week_cap = sum(c.capacity_h for c in all_cells)
    week_req = sum(c.required_h for c in all_cells)

    return {
        "site": site,
        "shifts": shifts,
        "shift": shift,
        "day": day,
        "day_label": day_label(day),
        "sel": sel,
        "lines": lines,
        "goods_pct": None if goods is None else round(goods),
        "supplies_pct": None if supplies is None else round(supplies),
        "resource_rows": resource_rows,
        "suggestions": suggestions,
        "days": [{"date": d, "label": f"{DOW[d.weekday()]} {d:%d/%m}", "today": d == timezone.localdate()} for d in days],
        "grid": grid,
        "week_cap": week_cap,
        "week_req": week_req,
        "week_pct": round(week_req / week_cap * 100) if week_cap else None,
        "prev_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1),
        **_orders_block(site, now),
    }


def _orders_block(site: Site, now: datetime) -> dict:
    open_orders = WorkOrder.objects.filter(site=site, status__in=WorkOrder.OPEN_STATUSES)
    unscheduled = list(Operation.objects.filter(order__in=open_orders, planned_start__isnull=True)
                       .exclude(status=Operation.Status.DONE).select_related("order"))
    since = now - timedelta(days=30)
    finished = (WorkOrder.objects.filter(site=site, status=WorkOrder.Status.DONE, due_date__isnull=False)
                .annotate(done_at=Max("operations__actual_end")).filter(done_at__gte=since))
    total_done = finished.count()
    on_time = sum(1 for o in finished if o.done_at <= o.due_date)
    today = timezone.localtime(now).date()
    tz = local_tz()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
    rep = ProductionReport.objects.filter(operation__order__site=site, reported_at__gte=day_start).aggregate(
        good=Sum("qty_good"), scrap=Sum("qty_scrap"))
    good, scrap = float(rep["good"] or 0), float(rep["scrap"] or 0)
    return {
        "on_time_n": on_time,
        "to_plan": open_orders.filter(status=WorkOrder.Status.TO_PLAN).count(),
        "unscheduled_ops": len(unscheduled),
        "unscheduled_hh": sum(op.man_hours for op in unscheduled),
        "late": open_orders.filter(due_date__lt=now).count(),
        "at_risk": open_orders.filter(due_date__gte=now, planned_end__gt=F("due_date")).count(),
        "open_count": open_orders.count(),
        "on_time_pct": round(on_time / total_done * 100) if total_done else None,
        "done_30": total_done,
        "today_good": good,
        "today_scrap": scrap,
        "today_quality": round(good / (good + scrap) * 100) if good + scrap else None,
    }


ORDER_SUMS = ("to_plan", "unscheduled_ops", "unscheduled_hh", "late", "at_risk", "open_count", "done_30", "on_time_n",
              "today_good", "today_scrap")


def all_sites_data(sites, day: date | None = None, now: datetime | None = None) -> dict:
    """«Todas las bodegas»: la capacidad instalada completa, bodega por bodega (cada una en su turno actual o en el
    del día elegido) y sumada, más la carga de TODAS las líneas y los pedidos de todas las plantas."""
    now = now or timezone.now()
    rows, lines, idle = [], [], []
    tot = {k: 0 for k in ("available", "scheduled", "absent", "capacity_h", "required_h", "week_cap", "week_req",
                          "stations", "n_lines")}
    orders = {k: 0 for k in ORDER_SUMS}
    unlimited_any = False
    for site in sites:
        data = dashboard_data(site, day=day, now=now)
        for k in ORDER_SUMS:
            orders[k] += data.get(k) or 0
        wcs = list(WorkCenter.objects.filter(site=site, active=True))
        if data.get("no_shifts"):
            if wcs:
                rows.append({"site": site, "no_shifts": True, "n_lines": len(wcs),
                             "stations": sum(w.capacity for w in wcs), **{k: data.get(k) for k in ("open_count", "late")}})
            else:
                idle.append(site)
            continue
        sel = data["sel"]
        row = {"site": site, "shift": data["shift"], "day": data["day"], "day_label": data["day_label"], "sel": sel,
               "n_lines": len(wcs), "stations": sum(w.capacity for w in wcs), "week_cap": data["week_cap"],
               "week_req": data["week_req"], "week_pct": data["week_pct"], "open_count": data["open_count"],
               "late": data["late"], "bar": min(100, sel.occ_pct) if sel.occ_pct is not None else 0}
        rows.append(row)
        unlimited_any = unlimited_any or sel.unlimited
        tot["n_lines"] += row["n_lines"]
        tot["stations"] += row["stations"]
        if not sel.unlimited:
            tot["available"] += sel.available
            tot["scheduled"] += sel.scheduled
            tot["capacity_h"] += sel.capacity_h
        tot["absent"] += sel.absent
        tot["required_h"] += sel.required_h
        tot["week_cap"] += data["week_cap"]
        tot["week_req"] += data["week_req"]
        for ln in data["lines"]:
            lines.append({**ln, "site": site, "shift": data["shift"]})
    for r in rows:
        if r.get("no_shifts"):
            tot["n_lines"] += r["n_lines"]
            tot["stations"] += r["stations"]
    lines.sort(key=lambda x: -(x["occ_pct"] or -1))
    cap, req = tot["capacity_h"], tot["required_h"]
    occ = req / cap if cap else (math.inf if req else None)
    done = orders["done_30"]
    return {
        "all_rows": rows, "all_lines": lines, "idle_sites": idle, "tot": tot, "unlimited_any": unlimited_any,
        "tot_occ_pct": None if occ is None else (999 if math.isinf(occ) else round(occ * 100)), "tot_level": level(occ),
        "tot_bar": 0 if occ is None else (100 if math.isinf(occ) else min(100, round(occ * 100))),
        "tot_week_pct": round(tot["week_req"] / tot["week_cap"] * 100) if tot["week_cap"] else None,
        "staff_active": Employee.objects.filter(active=True).count(),
        **orders,
        "on_time_pct": round(orders["on_time_n"] / done * 100) if done else None,
        "today_quality": (round(orders["today_good"] / (orders["today_good"] + orders["today_scrap"]) * 100)
                          if orders["today_good"] + orders["today_scrap"] else None),
        "day_label": day_label(day or timezone.localdate()),
        "day": day or timezone.localdate(),
        "prev_day": (day or timezone.localdate()) - timedelta(days=1),
        "next_day": (day or timezone.localdate()) + timedelta(days=1),
    }


def fmt_h(hours: float) -> str:
    value = f"{hours:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if value.endswith(",0"):
        value = value[:-2]
    return f"{value} h"
