"""Bolsa global de personal.

Todos los operarios activos forman UNA bolsa para toda la empresa. De esa bolsa:

  * **Reparto** (``StaffAllocation``): cuántas personas trabaja cada bodega (y turno). Hay un reparto base y cambios
    por fecha. Es lo que usan la planeación, el programador y la capacidad como «operarios disponibles».
  * **Quién está dónde** (``StaffMove`` + cuadrillas): la ubicación de cada persona en un momento. Manda, en orden:
      1. la cuadrilla en la que está asignada en ese momento (la operación da bodega y línea),
      2. el traslado más reciente que cubre ese momento,
      3. su bodega habitual.
    Nadie es fijo de una bodega: un traslado temporal vence y la persona vuelve a donde estaba.
  * **Capacidad global**: personas y horas-hombre de toda la empresa por día, frente a lo programado.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import workcal
from .models import (
    Attendance, CrewAssignment, Employee, Operation, ShiftAttendance, Site, StaffAllocation, StaffMove, WorkCenter,
    WorkOrder,
)
from .scheduling import shift_window, site_shifts


def _lvl(pct: int | None) -> str:
    if pct is None:
        return "na"
    return "ok" if pct < 85 else "warn" if pct <= 100 else "bad"


def pool_size() -> int:
    """Personas de la bolsa global: los operarios activos de toda la empresa."""
    return Employee.objects.filter(active=True).count()


def absent_ids(day: date) -> dict[int, str]:
    """Operarios con ausencia confirmada ese día (no presente ni tarde) → motivo."""
    return {a.employee_id: a.get_status_display() for a in
            Attendance.objects.filter(day=day).exclude(status__in=Attendance.WORKING)}


# ---------------------------------------------------------------- reparto
SOURCE_LABEL = {"cambio": "Cambio por fecha", "base": "Reparto base", "turno": "Operarios del turno",
                "planta": "Total de la planta", "": "Sin dato (sin límite)"}


def shift_people(site: Site, shift, day: date) -> tuple[int, str, StaffAllocation | None]:
    """(personas, fuente, reparto) de ese turno ese día."""
    cal = workcal.get_calendar(site.pk)
    a = cal.allocation(shift, day) if cal.has_allocations else None
    if a is not None:
        return a.headcount, "cambio" if a.date_from else "base", a
    if shift.operators:
        return shift.operators, "turno", None
    if site.operators_total:
        return site.operators_total, "planta", None
    return 0, "", None


def day_split(day: date, sites=None) -> dict:
    """El reparto efectivo de ese día: por bodega y turno, cuánto de la bolsa queda repartido y cuánto libre."""
    sites = list(sites if sites is not None else Site.objects.filter(active=True).order_by("code"))
    cal_rows, total = [], 0
    for site in sites:
        cal = workcal.get_calendar(site.pk)
        shifts = site_shifts(site)
        rows = []
        for sh in shifts:
            off = cal.shift_off_reason(sh, day)
            n, src, alloc = shift_people(site, sh, day)
            rows.append({"shift": sh, "people": n, "source": src, "source_label": SOURCE_LABEL[src], "alloc": alloc,
                         "off": off})
            if not off:
                total += n
        cal_rows.append({"site": site, "shifts": rows, "people": sum(r["people"] for r in rows if not r["off"])})
    pool = pool_size()
    absent = len(absent_ids(day))
    return {"day": day, "sites": cal_rows, "pool": pool, "absent": absent, "allocated": total,
            "free": pool - absent - total}


@transaction.atomic
def save_base_split(values: dict[tuple[int, int | None], int | None], user=None) -> int:
    """Reparto base: {(bodega, turno o None): personas o None}. None borra (vuelve a los operarios del turno)."""
    changed = 0
    u = user if user is not None and getattr(user, "is_authenticated", False) else None
    for (site_id, shift_id), n in values.items():
        qs = StaffAllocation.objects.filter(site_id=site_id, shift_id=shift_id, date_from__isnull=True)
        cur = qs.first()
        if n is None:
            if cur:
                qs.delete()
                changed += 1
            continue
        if n < 0:
            raise ValidationError("Las personas no pueden ser negativas.")
        if cur is None:
            StaffAllocation.objects.create(site_id=site_id, shift_id=shift_id, headcount=n, created_by=u)
            changed += 1
        elif cur.headcount != n:
            cur.headcount, cur.created_by = n, u
            cur.save(update_fields=["headcount", "created_by", "updated_at"])
            changed += 1
    workcal.invalidate()
    return changed


# ---------------------------------------------------------------- quién está dónde
@dataclass
class Location:
    site_id: int | None
    work_center_id: int | None
    source: str            # cuadrilla | traslado | habitual | ""
    detail: str = ""
    until: datetime | None = None
    move_id: int | None = None


def locate(at: datetime, employees: list[Employee] | None = None) -> dict[int, Location]:
    """Dónde está cada operario en ese momento (cuadrilla → traslado → bodega habitual)."""
    emps = employees if employees is not None else list(Employee.objects.filter(active=True))
    ids = [e.pk for e in emps]
    out: dict[int, Location] = {}
    for e in emps:
        out[e.pk] = Location(e.site_id, None, "habitual" if e.site_id else "")
    moves = (StaffMove.objects.filter(employee_id__in=ids, start__lte=at)
             .filter(Q(end__isnull=True) | Q(end__gt=at)).order_by("employee_id", "-start", "-pk"))
    seen = set()
    for m in moves:
        if m.employee_id in seen:
            continue
        seen.add(m.employee_id)
        out[m.employee_id] = Location(m.site_id, m.work_center_id, "traslado", m.note, m.end, m.pk)
    crews = (CrewAssignment.objects.filter(employee_id__in=ids)
             .exclude(operation__status=Operation.Status.DONE)
             .exclude(operation__order__status=WorkOrder.Status.CANCELLED)
             .filter(Q(operation__planned_start__lte=at, operation__planned_end__gt=at)
                     | Q(operation__status=Operation.Status.IN_PROGRESS, operation__actual_start__lte=at))
             .select_related("operation__order", "operation__work_center"))
    for c in crews:
        op = c.operation
        out[c.employee_id] = Location(op.order.site_id, op.work_center_id, "cuadrilla",
                                      f"{op.order.number} · {op.name}", op.planned_end)
    return out


@transaction.atomic
def move(employee_ids: list[int], site: Site, start: datetime, end: datetime | None = None,
         work_center: WorkCenter | None = None, note: str = "", user=None) -> int:
    """Traslada personas a una bodega (y línea) desde `start` hasta `end` (None = hasta nuevo aviso).

    Hasta nuevo aviso: los traslados abiertos anteriores de esas personas se cierran en `start` (la persona cambia
    de bodega). Temporal: no se tocan; al vencer, la persona vuelve a donde estaba."""
    if not employee_ids:
        raise ValidationError("Elige al menos una persona.")
    if end is not None and end <= start:
        raise ValidationError("El fin del traslado debe ser posterior al inicio.")
    if work_center is not None and work_center.site_id != site.pk:
        raise ValidationError("La línea no es de esa bodega.")
    emps = list(Employee.objects.filter(pk__in=employee_ids, active=True))
    if len(emps) != len(set(employee_ids)):
        raise ValidationError("Alguna persona no existe o está inactiva.")
    u = user if user is not None and getattr(user, "is_authenticated", False) else None
    if end is None:
        StaffMove.objects.filter(employee_id__in=employee_ids, start__lt=start).filter(
            Q(end__isnull=True) | Q(end__gt=start)).update(end=start)
        StaffMove.objects.filter(employee_id__in=employee_ids, start__gte=start, end__isnull=True).delete()
    StaffMove.objects.bulk_create([StaffMove(employee=e, site=site, work_center=work_center, start=start, end=end,
                                             note=note[:200], created_by=u) for e in emps])
    return len(emps)


@transaction.atomic
def send_back(employee_ids: list[int], at: datetime) -> int:
    """Termina en `at` los traslados vigentes de esas personas (vuelven a su ubicación anterior o habitual)."""
    qs = StaffMove.objects.filter(employee_id__in=employee_ids, start__lte=at).filter(Q(end__isnull=True) | Q(end__gt=at))
    n = qs.count()
    qs.filter(start=at).delete()
    qs.update(end=at)
    return n


def location_board(at: datetime, q: str = "") -> dict:
    """Pantalla «¿Quién está dónde?»: personas por bodega en ese momento, frente al reparto."""
    tz = timezone.get_current_timezone()
    day = timezone.localtime(at, tz).date()
    emps = list(Employee.objects.filter(active=True).select_related("shift", "site").order_by("full_name"))
    locs = locate(at, emps)
    absent = absent_ids(day)
    sites = list(Site.objects.filter(active=True).order_by("code"))
    lines = {wc.pk: wc for wc in WorkCenter.objects.filter(site__in=sites)}
    cols = {s.pk: {"site": s, "people": [], "here": 0, "quota": None, "slot": "", "lines": [
        wc for wc in lines.values() if wc.site_id == s.pk and wc.active]} for s in sites}
    for s in sites:
        cal = workcal.get_calendar(s.pk)
        shifts = site_shifts(s)
        slot = cal.slot_at(shifts, at) if shifts or cal.extras else None
        if slot:
            cols[s.pk]["quota"] = slot.operators or s.operators_total or None
            cols[s.pk]["slot"] = slot.label
        elif shifts:
            cols[s.pk]["slot"] = "Fuera de turno"
    unplaced, away = [], []
    needle = (q or "").strip().lower()
    for e in emps:
        loc = locs[e.pk]
        row = {"e": e, "loc": loc, "line": lines.get(loc.work_center_id), "absent": absent.get(e.pk)}
        if needle and needle not in f"{e.full_name} {e.document} {e.position}".lower():
            continue
        if row["absent"]:
            away.append(row)
        elif loc.site_id in cols:
            cols[loc.site_id]["people"].append(row)
        else:
            unplaced.append(row)
    for c in cols.values():
        c["here"] = len(c["people"])
        c["gap"] = (c["here"] - c["quota"]) if c["quota"] is not None else None
        c["by_source"] = {k: sum(1 for r in c["people"] if r["loc"].source == k)
                          for k in ("cuadrilla", "traslado", "habitual")}
    placed = sum(c["here"] for c in cols.values())
    return {"at": at, "day": day, "columns": list(cols.values()), "unplaced": unplaced, "away": away,
            "pool": len(emps), "placed": placed, "n_absent": len(away), "q": q}


# ---------------------------------------------------------------- capacidad global
def global_capacity(start_day: date, days: int = 7) -> dict:
    """Personas y horas-hombre de toda la empresa por día y bodega, frente a lo programado."""
    from .capacity import compute_many

    tz = timezone.get_current_timezone()
    day_list = [start_day + timedelta(days=i) for i in range(days)]
    sites = list(Site.objects.filter(active=True).order_by("code"))
    pool = pool_size()
    absent_by_day = {d: 0 for d in day_list}
    for a in Attendance.objects.filter(day__in=day_list).exclude(status__in=Attendance.WORKING):
        absent_by_day[a.day] += 1
    rows, unlimited_sites = [], []
    tot = {d: {"people": 0, "absent": 0, "cap_h": 0.0, "req_h": 0.0} for d in day_list}
    for site in sites:
        shifts = site_shifts(site)
        if not shifts:
            continue
        lo = datetime.combine(day_list[0] - timedelta(days=1), datetime.min.time(), tzinfo=tz)
        hi = datetime.combine(day_list[-1] + timedelta(days=2), datetime.min.time(), tzinfo=tz)
        ops = list(Operation.objects.filter(order__site=site, planned_start__lt=hi, planned_end__gt=lo)
                   .exclude(order__status=WorkOrder.Status.CANCELLED)
                   .select_related("order__product", "order__customer", "work_center"))
        attendance = {(a.shift_id, a.day): a for a in
                      ShiftAttendance.objects.filter(shift__in=shifts, day__gte=day_list[0], day__lte=day_list[-1])}
        cells = compute_many(site, shifts, day_list, ops, attendance)
        cells_out, unlimited = [], False
        for d in day_list:
            caps = [cells[(s.pk, d)] for s in shifts]
            work = [c for c in caps if not c.off]
            unl = any(c.unlimited for c in work)
            unlimited = unlimited or unl
            people = sum(c.scheduled for c in work if not c.unlimited)
            absent = sum(c.absent for c in work)
            cap_h = sum(c.capacity_h for c in work if not c.unlimited)
            req_h = sum(c.required_h for c in caps)
            occ = req_h / cap_h if cap_h else (math.inf if req_h else None)
            cells_out.append({"day": d, "people": people, "absent": absent, "cap_h": cap_h, "req_h": req_h,
                              "free_h": cap_h - req_h, "off": not work, "unlimited": unl,
                              "occ_pct": None if occ is None else (999 if math.isinf(occ) else round(occ * 100)),
                              "lvl": _lvl(None if occ is None else (999 if math.isinf(occ) else round(occ * 100))),
                              "shifts": [{"shift": c.shift, "people": c.scheduled, "absent": c.absent,
                                          "cap_h": c.capacity_h, "req_h": c.required_h, "off": c.off,
                                          "unlimited": c.unlimited} for c in caps]})
            t = tot[d]
            t["people"] += people
            t["absent"] += absent
            t["cap_h"] += cap_h
            t["req_h"] += req_h
        if unlimited:
            unlimited_sites.append(site)
        rows.append({"site": site, "cells": cells_out,
                     "week_cap": sum(c["cap_h"] for c in cells_out), "week_req": sum(c["req_h"] for c in cells_out)})
    totals = []
    for d in day_list:
        t = tot[d]
        occ = t["req_h"] / t["cap_h"] if t["cap_h"] else (math.inf if t["req_h"] else None)
        pct = None if occ is None else (999 if math.isinf(occ) else round(occ * 100))
        totals.append({**t, "day": d, "pool": pool, "absent_people": absent_by_day[d],
                       "free_people": pool - absent_by_day[d] - t["people"], "free_h": t["cap_h"] - t["req_h"],
                       "occ_pct": pct, "lvl": _lvl(pct), "bar": min(100, pct or 0),
                       "people_pct": min(100, round(t["people"] / pool * 100)) if pool else 0})
    week_cap = sum(t["cap_h"] for t in totals)
    week_req = sum(t["req_h"] for t in totals)
    for r in rows:
        for c in r["cells"]:
            c["bar"] = min(100, c["occ_pct"] or 0)
    week_pct = round(week_req / week_cap * 100) if week_cap else None
    return {"days": day_list, "rows": rows, "totals": totals, "pool": pool, "week_cap": week_cap, "week_req": week_req,
            "week_pct": week_pct, "week_lvl": _lvl(week_pct), "unlimited_sites": unlimited_sites,
            "no_shift_sites": [s for s in sites if not site_shifts(s)]}


def shift_hours_label(shift, day: date) -> str:
    tz = timezone.get_current_timezone()
    a, b = shift_window(shift, day, tz)
    return f"{timezone.localtime(a):%H:%M}–{timezone.localtime(b):%H:%M}"


# ---------------------------------------------------------------- indicadores de la empresa (tablero principal)
def standard_shift_hours(sites) -> tuple[float, object | None]:
    """Jornada de referencia: las horas hábiles (sin descansos) del turno más común de la empresa."""
    from collections import Counter

    shifts = [s for site in sites for s in site_shifts(site)]
    if not shifts:
        return 8.0, None
    counts = Counter(round(s.effective_minutes) for s in shifts)
    minutes = max(counts, key=lambda m: (counts[m], m))
    return minutes / 60, next(s for s in shifts if round(s.effective_minutes) == minutes)


def company_kpis(day: date, orders: dict | None = None) -> dict:
    """Tablero de la empresa: personal (bolsa global), capacidad en horas y desempeño."""
    from .models import CalendarException

    sites = list(Site.objects.filter(active=True).order_by("code"))
    pool = pool_size()
    split = day_split(day, sites)
    absent = split["absent"]
    present = max(0, pool - absent)
    jornada, ref_shift = standard_shift_hours(sites)
    g = global_capacity(day, 7)
    today = g["totals"][0]
    potential_h = present * jornada                 # toda la bolsa presente trabajando una jornada
    allocated_h = today["cap_h"]                    # lo que dan las bodegas con su reparto de hoy
    req_h = today["req_h"]                          # lo programado hoy
    free_h = max(0, split["free"]) * jornada        # lo que aportan los libres para repartir
    working_days = [t for t in g["totals"] if t["cap_h"] or t["people"]]
    week_potential = sum(max(0, pool - t["absent_people"]) * jornada for t in working_days)
    occ = req_h / potential_h if potential_h else None
    orders = orders or {}
    backlog_hh = float(orders.get("unscheduled_hh") or 0)
    daily = potential_h or (week_potential / len(working_days) if working_days else 0)
    # Horas extra de los próximos 7 días en todas las bodegas
    ot_h = ot_hh = 0.0
    ot_n = 0
    end = day + timedelta(days=7)
    for exc in CalendarException.objects.filter(
            site__in=sites, kind__in=[CalendarException.Kind.OVERTIME, CalendarException.Kind.EXTRA_SHIFT],
            date__gte=day, date__lt=end).select_related("shift", "site"):
        if not (exc.start_time and exc.end_time):
            continue
        a = exc.start_time.hour * 60 + exc.start_time.minute
        b = exc.end_time.hour * 60 + exc.end_time.minute
        h = ((b - a) % 1440 or 1440) / 60
        people = exc.operators or (workcal.get_calendar(exc.site_id).people(exc.shift, exc.date) if exc.shift_id else 0)
        ot_h += h
        ot_hh += h * people
        ot_n += 1
    done = orders.get("done_30") or 0
    from . import indicators
    tz = timezone.get_current_timezone()
    since = datetime.combine(timezone.localdate() - timedelta(days=29), datetime.min.time(), tzinfo=tz)
    ind = indicators.summary(sites, since, timezone.now())
    oee = ind["oee"]  # rendimiento, productividad y calidad: lo mismo que mide el OEE (una sola fuente)
    eff = {"days": 30, "ops": bool(oee["good"] or oee["scrap"]), "efficiency_pct": oee["performance"],
           "quality_pct": oee["quality"], "units_per_hh": round(oee["good"] / oee["labor_h"], 1) if oee["labor_h"] >= 1 else None}
    return {
        "day": day, "pool": pool, "present": present, "absent": absent,
        "absent_pct": round(absent / pool * 100, 1) if pool else None,
        "allocated": split["allocated"], "free": split["free"],
        "allocated_pct": min(100, round(split["allocated"] / pool * 100)) if pool else 0,
        "jornada": jornada, "jornada_hm": f"{int(jornada)} h {round((jornada % 1) * 60):02d} min", "ref_shift": ref_shift,
        "potential_h": potential_h, "allocated_h": allocated_h, "free_h": free_h, "req_h": req_h,
        "occ_pct": None if occ is None else round(occ * 100), "occ_bar": min(100, round((occ or 0) * 100)),
        "week_potential": week_potential, "week_cap": g["week_cap"], "week_req": g["week_req"],
        "week_pct": round(g["week_req"] / week_potential * 100) if week_potential else None,
        "backlog_hh": backlog_hh, "backlog_days": round(backlog_hh / daily, 1) if daily else None,
        "ot_hours": ot_h, "ot_hh": ot_hh, "ot_n": ot_n,
        "eff": eff,
        "on_time_pct": round(orders["on_time_n"] / done * 100) if done else None, "done_30": done,
        "open_count": orders.get("open_count") or 0, "late": orders.get("late") or 0,
        "at_risk": orders.get("at_risk") or 0, "to_plan": orders.get("to_plan") or 0,
        "unlimited_sites": g["unlimited_sites"],
        "ind": ind,
    }
