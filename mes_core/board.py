"""Arma los datos (JSON) del programador visual."""

from __future__ import annotations

from datetime import date

from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from .models import Customer, Downtime, Operation, ScheduleChange, Site, WorkCenter, WorkOrder
from . import planning, scheduling, workcal
from .scheduling import (
    OperatorLedger,
    build_slots, center_shifts, detect_conflicts, operator_overloads, shift_anchor, site_shifts, view_days,
    working_minutes, working_windows,
)

DOW = ["LUN", "MAR", "MIÉ", "JUE", "VIE", "SÁB", "DOM"]
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _iso(dt):
    return timezone.localtime(dt).isoformat() if dt else None


def _block_state(op: Operation) -> str:
    if op.status == Operation.Status.DONE:
        return "done"
    if op.status == Operation.Status.IN_PROGRESS:
        return "running"
    if op.order.status in (WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS):
        return "released"
    return "planned"


def _order_late(order: WorkOrder, now) -> bool:
    if not order.due_date or order.status not in WorkOrder.OPEN_STATUSES:
        return False
    return order.due_date < now or bool(order.planned_end and order.planned_end > order.due_date)


def _op_payload(op: Operation, now) -> dict:
    order = op.order
    customer = order.customer
    return {
        "id": op.pk,
        "order_id": order.pk,
        "order": order.number,
        "order_url": order.get_absolute_url(),
        "op": op.name,
        "seq": op.sequence,
        "kind": op.work_center_kind,
        "sku": order.product.sku,
        "product": order.product.name,
        "qty": float(op.qty),
        "done": float(op.qty_good + op.qty_scrap),
        "changeover": float(op.changeover_minutes or 0),
        "split_of": op.split_of_id,
        "crew": op.crew,
        "man_hours": round(op.man_hours, 2),
        "unit": order.product.unit,
        "priority": order.priority,
        "customer": customer.name if customer else "",
        "tier": customer.tier if customer else "",
        "due": _iso(order.due_date),
        "late": _order_late(order, now),
        "minutes": round(op.planned_minutes),
        "status": op.status,
        "status_label": op.get_status_display(),
        "order_status": order.get_status_display(),
        "state": _block_state(op),
        "start": _iso(op.planned_start),
        "end": _iso(op.planned_end),
        "line": op.work_center_id,
        "stage_id": op.stage_id,
        "stage_name": op.stage.name if op.stage_id else "",
        "stage_first": bool(op.stage_id and op.stage.sequence == 1),
        "link": op.stage.link if op.stage_id else "",
        "link_lag": float(op.stage.link_lag) if op.stage_id and op.stage.link_lag is not None else None,
    }


def board_data(site: Site, view: str, anchor: date, line_id: int | None = None) -> dict:
    now = timezone.now()
    today = timezone.localdate()
    shifts = site_shifts(site)
    days = view_days(view, anchor)
    slots = build_slots(shifts, view, days, site.pk)
    win_start, win_end = slots[0].start, slots[-1].end

    all_lines = list(WorkCenter.objects.filter(site=site, active=True).prefetch_related("shifts"))
    lines = [wc for wc in all_lines if line_id is None or wc.pk == line_id]
    line_ids = [wc.pk for wc in lines]

    base_ops = Operation.objects.select_related("order", "order__product", "order__customer", "work_center", "stage")
    window_ops = list(
        base_ops.filter(
            order__site=site, work_center_id__in=line_ids,
            planned_start__lt=win_end, planned_end__gt=win_start,
        ).exclude(order__status=WorkOrder.Status.CANCELLED)
    )
    # Para validar secuencias se necesitan todas las operaciones de esas órdenes.
    order_ids = {o.order_id for o in window_ops}
    related_ops = list(base_ops.filter(order_id__in=order_ids))
    downtimes = list(Downtime.objects.filter(work_center_id__in=line_ids, start__lt=win_end, end__gt=win_start))
    conflicts = detect_conflicts(related_ops, downtimes)  # related_ops ⊇ window_ops
    # Operarios: se suman las cuadrillas de TODA la planta (no solo las líneas filtradas).
    site_ops = list(Operation.objects.filter(order__site=site, planned_start__lt=win_end, planned_end__gt=win_start)
                    .exclude(order__status=WorkOrder.Status.CANCELLED))
    for pk in operator_overloads(site_ops, shifts, site.operators_total):
        conflicts.setdefault(pk, set()).add("operarios")

    blocks = []
    for op in window_ops:
        payload = _op_payload(op, now)
        payload["conflicts"] = sorted(conflicts.get(op.pk, ()))
        blocks.append(payload)

    lines_payload = []
    for wc in lines:
        wc_shifts = center_shifts(wc, shifts)
        available = working_minutes(wc_shifts, win_start, win_end) * wc.capacity
        busy = sum(
            working_minutes(wc_shifts, max(o.planned_start, win_start), min(o.planned_end, win_end))
            for o in window_ops if o.work_center_id == wc.pk
        )
        lines_payload.append({
            "id": wc.pk, "code": wc.code, "name": wc.name, "kind": wc.kind, "capacity": wc.capacity,
            "load": round(busy * 100 / available) if available else 0,
            "running": any(o.work_center_id == wc.pk and o.status == Operation.Status.IN_PROGRESS for o in window_ops),
        })

    # Fila "Operarios": por columna, el mínimo de libres (disponibles − cuadrillas activas) de toda la planta.
    ledger = OperatorLedger(site, shifts, since=win_start)
    operators_row = None
    if not ledger.unlimited:
        operators_row = []
        for sl in slots:
            points = {sl.start} | {x for a, b, _, _ in ledger.items for x in (a, b) if sl.start < x < sl.end}
            vals = [(ledger.available_at(p), ledger.used_at(p)) for p in sorted(points) if ledger.is_working(p)]
            if not vals:
                operators_row.append(None)
                continue
            operators_row.append({"available": min(a for a, _ in vals), "used": max(u for _, u in vals),
                                  "free": min(a - u for a, u in vals)})

    backlog_ops = (
        base_ops.filter(order__site=site, order__status__in=WorkOrder.OPEN_STATUSES, planned_start__isnull=True)
        .exclude(status=Operation.Status.DONE)
        .order_by("order__priority", "order__due_date", "order_id", "sequence")
    )
    rules = planning.get_rules(site)
    backlog_list = planning.ordered_backlog(site, list(backlog_ops[:300])) if rules else list(backlog_ops[:300])
    backlog = []
    for op in backlog_list:
        payload = _op_payload(op, now)
        payload["score"] = planning.priority_score(op.order, rules, now)["score"] if rules else None
        backlog.append(payload)
    split_roots = set(Operation.objects.filter(split_of__isnull=False, order__site=site)
                      .values_list("split_of_id", flat=True))
    for b in blocks:
        b["has_parts"] = b["id"] in split_roots
    cal = workcal.get_calendar(site.pk)
    frozen = scheduling.frozen_until(site.pk)
    line_windows = {}
    for wc in lines:
        wins = working_windows(center_shifts(wc, shifts), win_start, win_end) if shifts else []
        line_windows[wc.pk] = [[_iso(a), _iso(b)] for a, b in wins]

    # Ruta completa de cada orden visible (chips "Ruta de la orden" en el panel de edición).
    route_ids = {b["order_id"] for b in blocks} | {b["order_id"] for b in backlog}
    routes: dict[int, list] = {}
    for op in Operation.objects.filter(order_id__in=route_ids).select_related("work_center").order_by("order_id", "sequence"):
        routes.setdefault(op.order_id, []).append({
            "id": op.pk, "seq": op.sequence, "name": op.name, "status": op.status,
            "line": op.work_center.code if op.work_center else "", "start": _iso(op.planned_start),
        })

    overdue = [
        o for o in WorkOrder.objects.filter(site=site, status__in=WorkOrder.OPEN_STATUSES, due_date__isnull=False)
        .filter(Q(due_date__lt=now) | Q(planned_end__gt=F("due_date"))).select_related("customer")
    ]
    overdue.sort(key=lambda o: (-Customer.TIER_RANK.get(o.customer.tier if o.customer else "", 0), o.due_date))
    high_tier = sum(1 for o in overdue if o.customer and o.customer.tier in (Customer.Tier.GOLD, Customer.Tier.VIP))

    day_headers, col = [], 0
    for d in days:
        n = sum(1 for s in slots if s.day == d)
        day_headers.append({
            "date": d.isoformat(), "dow": DOW[d.weekday()], "day": d.day, "month": MONTHS[d.month - 1],
            "today": d == today, "col": col, "span": n,
        })
        col += n

    if view == "mes":
        range_label = f"{MONTHS[anchor.month - 1].capitalize()} {anchor.year}"
    elif view == "dia":
        range_label = f"{DOW[anchor.weekday()].capitalize()} {anchor:%d/%m/%Y}"
    else:
        range_label = f"{days[0]:%d/%m} – {days[-1]:%d/%m/%Y}"

    changes = ScheduleChange.objects.filter(site=site).select_related("user")[:50]

    return {
        "site": {"id": site.pk, "code": site.code, "name": site.name},
        "sites": [{"id": s.pk, "label": str(s)} for s in Site.objects.filter(active=True)],
        "view": view,
        "anchor": anchor.isoformat(),
        "prev": shift_anchor(view, anchor, -1).isoformat(),
        "next": shift_anchor(view, anchor, 1).isoformat(),
        "today": today.isoformat(),
        "range_label": range_label,
        "now": _iso(now),
        "tz": timezone.get_current_timezone_name(),
        "shifts": [{"id": s.pk, "name": s.name, "color": s.color, "start": f"{s.start_time:%H:%M}", "end": f"{s.end_time:%H:%M}"} for s in shifts],
        "days": day_headers,
        "operators": {"total": site.operators_total, "slots": operators_row} if operators_row is not None else None,
        "frozen_until": _iso(frozen),
        "holidays": [{"date": d.isoformat(), "name": cal.holiday(d)} for d in days if cal.holiday(d)],
        "line_windows": line_windows,
        "slots": [{"start": _iso(s.start), "end": _iso(s.end), "label": s.label, "color": s.color,
                   "off": s.off, "note": s.note, "extra": s.extra,
                   "today": s.day == today} for s in slots],
        "lines": lines_payload,
        "all_lines": [{"id": wc.pk, "label": f"{wc.code} · {wc.name}", "code": wc.code, "name": wc.name, "kind": wc.kind,
                       "capacity": wc.capacity, "shifts": [x.pk for x in wc.shifts.all() if x.active]} for wc in all_lines],
        "routes": routes,
        "line_filter": line_id,
        "blocks": blocks,
        "downtimes": [{"id": d.pk, "line": d.work_center_id, "reason": d.reason,
                       "start": _iso(d.start), "end": _iso(d.end)} for d in downtimes],
        "backlog": backlog,
        "overdue": {
            "count": len(overdue), "high_tier": high_tier,
            "items": [{"order": o.number, "url": o.get_absolute_url(),
                       "customer": o.customer.name if o.customer else "",
                       "tier": o.customer.tier if o.customer else ""} for o in overdue[:5]],
        },
        "running": Operation.objects.filter(order__site=site, status=Operation.Status.IN_PROGRESS).count(),
        "changes": [{"at": _iso(c.created_at), "action": c.get_action_display(), "text": c.description,
                     "user": c.user.get_username() if c.user else ""} for c in changes],
        "new_order_url": reverse("mes_core:order_create"),
        "to_plan": {"count": WorkOrder.objects.filter(site=site, status=WorkOrder.Status.TO_PLAN).count(),
                    "url": reverse("mes_core:planning_list")},
    }
