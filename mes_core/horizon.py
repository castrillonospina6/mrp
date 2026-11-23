"""Plan a mediano plazo (4–8 semanas) y pronóstico de demanda.

Por semana:
  * Líneas (por tipo): horas-estación disponibles (calendario laboral × estaciones) frente a horas requeridas
    (lo programado + el backlog sin programar según su compromiso + el pronóstico).
  * Personal: horas-hombre disponibles (operarios × horas hábiles, con horas extra, turnos adicionales,
    festivos y ausencias) frente a horas-hombre requeridas.

Pronóstico por tipo de proceso con el histórico de pedidos:
  * Con más de un año de historia: misma semana del año pasado × tendencia (últimas 8 semanas frente a las
    mismas 8 del año anterior).
  * Con menos: promedio de las últimas 8 semanas.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from .capacity import compute_extra, compute_many, level
from .service_types import label_for
from .models import Operation, ShiftAttendance, Site, WorkCenter, WorkOrder
from .plugins import registry
from .scheduling import center_shifts, site_shifts, working_minutes
from . import workcal

MIN_HISTORY_WEEKS = 4


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _aware(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time(0)))


def _order_day(o: WorkOrder) -> date:
    return o.received_on or timezone.localtime(o.created_at).date()


def forecast(site: Site, weeks: int = 8, today: date | None = None) -> dict:
    """Demanda esperada (cantidad y horas) por tipo de proceso para las próximas semanas."""
    today = today or timezone.localdate()
    this_week = _monday(today)
    hist_start = this_week - timedelta(weeks=60)
    orders = list(WorkOrder.objects.filter(site=site).exclude(status=WorkOrder.Status.CANCELLED)
                  .filter(Q(received_on__gte=hist_start) | Q(received_on__isnull=True, created_at__gte=_aware(hist_start)))
                  .select_related("customer"))
    series: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    customers: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    first_seen: dict[str, date] = {}
    for o in orders:
        wk = _monday(_order_day(o))
        if wk >= this_week:
            continue  # la semana en curso aún no termina
        series[o.process_type][wk] += float(o.quantity)
        first_seen[o.process_type] = min(first_seen.get(o.process_type, wk), wk)
        if wk >= this_week - timedelta(weeks=12):
            customers[o.process_type][o.customer.name if o.customer else "Sin cliente"] += float(o.quantity)

    targets = [this_week + timedelta(weeks=i) for i in range(weeks)]
    rows = []
    for pt_key, s in series.items():
        span_weeks = (this_week - first_seen[pt_key]).days // 7
        if span_weeks < MIN_HISTORY_WEEKS:
            continue
        last8 = [s.get(this_week - timedelta(weeks=i), 0.0) for i in range(1, 9)]
        avg8 = sum(last8) / len(last8)
        seasonal = span_weeks >= 60
        prev8 = [s.get(this_week - timedelta(weeks=52 + i), 0.0) for i in range(1, 9)]
        trend = (sum(last8) / sum(prev8)) if seasonal and sum(prev8) > 0 else 1.0
        trend = max(0.5, min(2.0, trend))
        qty = []
        for t in targets:
            if seasonal:
                ly = s.get(t - timedelta(weeks=52), 0.0)
                qty.append(round(ly * trend if ly else avg8, 1))
            else:
                qty.append(round(avg8, 1))
        pt = registry.get_process_type(pt_key)
        per_unit_by_kind: dict[str, float] = defaultdict(float)
        setup_by_kind: dict[str, float] = defaultdict(float)
        if pt:
            for op in pt.operations:
                per_unit_by_kind[op.work_center_kind] += float(op.minutes_per_unit)
                setup_by_kind[op.work_center_kind] += float(op.setup_minutes)
        weekly_orders = max(1.0, sum(1 for o in orders if o.process_type == pt_key) / max(1, span_weeks))
        hours_by_kind = [{k: (per_unit_by_kind[k] * q + setup_by_kind[k] * weekly_orders) / 60 for k in per_unit_by_kind}
                         for q in qty]
        top = sorted(customers[pt_key].items(), key=lambda x: -x[1])[:3]
        total_c = sum(customers[pt_key].values()) or 1
        rows.append({
            "key": pt_key, "label": label_for(pt_key) or "Sin tipo de servicio", "seasonal": seasonal,
            "method": "Misma semana del año pasado × tendencia" if seasonal else "Promedio de las últimas 8 semanas",
            "trend": round((trend - 1) * 100), "avg8": round(avg8, 1), "qty": qty,
            "hours": [round(sum(h.values()), 1) for h in hours_by_kind], "hours_by_kind": hours_by_kind,
            "history": [s.get(this_week - timedelta(weeks=i), 0.0) for i in range(12, 0, -1)],
            "top_customers": [(n, round(v * 100 / total_c)) for n, v in top],
        })
    rows.sort(key=lambda r: -sum(r["qty"]))
    return {"weeks": targets, "rows": rows,
            "total_qty": [round(sum(r["qty"][i] for r in rows), 1) for i in range(weeks)],
            "total_hours": [round(sum(r["hours"][i] for r in rows), 1) for i in range(weeks)]}


def medium_term(site: Site, weeks: int = 8, include_forecast: bool = True, today: date | None = None) -> dict:
    today = today or timezone.localdate()
    start_week = _monday(today)
    week_starts = [start_week + timedelta(weeks=i) for i in range(weeks)]
    bounds = [(_aware(w), _aware(w + timedelta(days=7))) for w in week_starts]
    horizon_start, horizon_end = bounds[0][0], bounds[-1][1]

    shifts = site_shifts(site)
    centers = list(WorkCenter.objects.filter(site=site, active=True).prefetch_related("shifts").order_by("kind", "code"))
    kinds = sorted({wc.kind for wc in centers})
    wc_shifts = {wc.pk: center_shifts(wc, shifts) for wc in centers}

    ops = list(Operation.objects.filter(order__site=site, order__status__in=WorkOrder.OPEN_STATUSES)
               .exclude(status=Operation.Status.DONE).select_related("order", "work_center"))
    scheduled = [o for o in ops if o.planned_start and o.planned_end and o.planned_end > horizon_start
                 and o.planned_start < horizon_end]
    backlog = [o for o in ops if not o.planned_start]
    fc = forecast(site, weeks, today) if include_forecast else {"rows": []}

    # --- líneas (horas-estación) ---
    line_rows = []
    for kind in kinds:
        lines = [wc for wc in centers if wc.kind == kind]
        cells = []
        for i, (ws, we) in enumerate(bounds):
            cap = sum(working_minutes(wc_shifts[wc.pk], ws, we) / 60 * wc.capacity for wc in lines)
            req_sched = 0.0
            for o in scheduled:
                if o.work_center_id and o.work_center and o.work_center.kind == kind and o.planned_end > ws and o.planned_start < we:
                    req_sched += working_minutes(wc_shifts.get(o.work_center_id, shifts), max(o.planned_start, ws),
                                                 min(o.planned_end, we)) / 60
            req_backlog = 0.0
            for o in backlog:
                if o.work_center_kind != kind:
                    continue
                due = timezone.localtime(o.order.due_date).date() if o.order.due_date else today
                wk = max(_monday(due), start_week)
                if wk == week_starts[i]:
                    req_backlog += o.planned_minutes / 60
            req_fc = sum(r["hours_by_kind"][i].get(kind, 0.0) for r in fc["rows"])
            req = req_sched + req_backlog + req_fc
            occ = req / cap if cap else (math.inf if req else None)
            cells.append({"cap": cap, "sched": req_sched, "backlog": req_backlog, "forecast": req_fc, "req": req,
                          "occ_pct": None if occ is None else (999 if math.isinf(occ) else round(occ * 100)),
                          "level": level(occ)})
        line_rows.append({"kind": kind, "lines": lines, "cells": cells})

    # --- personal (horas-hombre) ---
    attendance = {(a.shift_id, a.day): a for a in ShiftAttendance.objects.filter(
        shift__in=shifts, day__gte=week_starts[0], day__lt=week_starts[-1] + timedelta(days=7))}
    cal = workcal.get_calendar(site.pk)
    people_cells = []
    for i, w in enumerate(week_starts):
        days = [w + timedelta(days=k) for k in range(7)]
        cap_hh = req_hh = 0.0
        extra_days = 0
        for c in compute_many(site, shifts, days, scheduled, attendance).values():
            cap_hh += c.capacity_h
            req_hh += c.required_h
        for d in days:
            ex = cal.extra_shifts_on(d, any_line=True)
            if ex:
                c = compute_extra(site, d, ex, scheduled)
                cap_hh += c.capacity_h
                req_hh += c.required_h
                extra_days += 1
        bl_hh = 0.0
        for o in backlog:
            due = timezone.localtime(o.order.due_date).date() if o.order.due_date else today
            if max(_monday(due), start_week) == w:
                bl_hh += o.man_hours
        fc_hh = sum(r["hours"][i] for r in fc["rows"])
        req_total = req_hh + bl_hh + fc_hh
        occ = req_total / cap_hh if cap_hh else (math.inf if req_total else None)
        people_cells.append({"cap": cap_hh, "sched": req_hh, "backlog": bl_hh, "forecast": fc_hh, "req": req_total,
                             "balance": cap_hh - req_total, "extra_days": extra_days,
                             "occ_pct": None if occ is None else (999 if math.isinf(occ) else round(occ * 100)),
                             "level": level(occ)})

    # --- recomendaciones ---
    recs = []
    first = shifts[0] if shifts else None
    person_week_h = (first.effective_minutes / 60 * sum(1 for k in range(7) if first.works_on_weekday(k))) if first else 40
    saturday_hh = (first.effective_minutes / 60 * (site.operators_total
                                                     or workcal.get_calendar(site.pk).people(first, timezone.localdate())
                                                     or 0)) if first else 0
    for w, c in zip(week_starts, people_cells):
        if c["balance"] < -0.5:
            deficit = -c["balance"]
            temps = math.ceil(deficit / person_week_h) if person_week_h else 0
            txt = f"Semana del {w:%d/%m}: faltan {deficit:,.0f} HH → {temps} temporales toda la semana"
            if saturday_hh:
                txt += f" o {math.ceil(deficit / saturday_hh)} turno(s) adicional(es) de {first.name.lower()}"
            recs.append({"week": w, "level": "over", "text": txt + "."})
    for row in line_rows:
        for w, c in zip(week_starts, row["cells"]):
            if c["level"] == "over":
                recs.append({"week": w, "level": "tight",
                             "text": f"Semana del {w:%d/%m}: las líneas de {row['kind']} quedan al {c['occ_pct']}% "
                                     f"({c['req']:,.0f} h de {c['cap']:,.0f} h). Considera horas extra o un turno adicional para esas líneas."})
    recs.sort(key=lambda r: r["week"])
    if not recs:
        recs.append({"week": None, "level": "ok", "text": "Con la capacidad actual se cubre la carga y el pronóstico de las próximas semanas."})

    return {"weeks": week_starts, "line_rows": line_rows, "people": people_cells, "forecast": fc,
            "recommendations": recs, "backlog_ops": len(backlog), "person_week_h": person_week_h}
