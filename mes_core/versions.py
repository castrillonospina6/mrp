"""Versiones del plan, aprobación y cumplimiento (plan frente a lo real)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import services
from .models import Operation, PlanVersion, ProductionReport, ScheduleChange, Site, WorkOrder


def _iso(dt):
    return timezone.localtime(dt).isoformat() if dt else None


def _dt(value):
    return parse_datetime(value) if value else None


def current_plan(site: Site) -> list[dict]:
    """Operaciones programadas de la planta (abiertas, y las terminadas en los últimos 14 días)."""
    since = timezone.now() - timedelta(days=14)
    ops = (Operation.objects.filter(order__site=site, planned_start__isnull=False, planned_end__isnull=False)
           .exclude(order__status=WorkOrder.Status.CANCELLED)
           .filter(planned_end__gte=since)
           .select_related("order__product", "order__customer", "work_center")
           .order_by("planned_start"))
    rows = []
    for op in ops:
        o = op.order
        rows.append({
            "id": op.pk, "order_id": o.pk, "order": o.number, "product": o.product.name,
            "customer": o.customer.name if o.customer else "", "op": op.name, "seq": op.sequence,
            "line_id": op.work_center_id, "line": op.work_center.code if op.work_center else "",
            "start": _iso(op.planned_start), "end": _iso(op.planned_end), "qty": float(op.qty), "crew": op.crew,
            "man_hours": round(op.man_hours, 2), "due": _iso(o.due_date), "status": op.status,
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    by_order: dict[int, dict] = {}
    for r in rows:
        cur = by_order.setdefault(r["order_id"], {"order": r["order"], "end": r["end"], "due": r["due"]})
        if r["end"] and (cur["end"] is None or r["end"] > cur["end"]):
            cur["end"] = r["end"]
    late = [v["order"] for v in by_order.values() if v["due"] and v["end"] and v["end"] > v["due"]]
    ends = [r["end"] for r in rows if r["end"]]
    return {"ops": len(rows), "orders": len(by_order), "late": len(late), "late_orders": late[:20],
            "man_hours": round(sum(r["man_hours"] for r in rows), 1), "last_end": max(ends) if ends else None,
            "order_ends": {str(k): v["end"] for k, v in by_order.items()}}


@transaction.atomic
def snapshot(site: Site, *, name: str = "", note: str = "", user=None, propose: bool = False) -> PlanVersion:
    rows = current_plan(site)
    number = (PlanVersion.objects.filter(site=site).aggregate(n=Max("number"))["n"] or 0) + 1
    v = PlanVersion.objects.create(
        site=site, number=number, name=name[:120], note=note, data=rows, summary=summarize(rows),
        status=PlanVersion.Status.PROPOSED if propose else PlanVersion.Status.DRAFT,
        created_by=user if user and user.is_authenticated else None)
    ScheduleChange.objects.create(site=site, action=ScheduleChange.Action.VERSION,
                                  description=f"Versión v{number} {'propuesta para aprobación' if propose else 'guardada'}"
                                              + (f": {name}" if name else ""),
                                  user=user if user and user.is_authenticated else None)
    return v


def compare(a_rows: list[dict], b_rows: list[dict]) -> dict:
    """Diferencias de b frente a a (operaciones movidas, de línea, nuevas, retiradas y fin de cada pedido)."""
    a = {r["id"]: r for r in a_rows}
    b = {r["id"]: r for r in b_rows}
    moved, relined, added, removed = [], [], [], []
    for oid, rb in b.items():
        ra = a.get(oid)
        if not ra:
            added.append(rb)
            continue
        delta_h = ((_dt(rb["start"]) - _dt(ra["start"])).total_seconds() / 3600) if ra["start"] and rb["start"] else 0
        if ra["line_id"] != rb["line_id"]:
            relined.append({"a": ra, "b": rb, "delta_h": round(delta_h, 1)})
        elif abs(delta_h) >= 0.01 or ra["end"] != rb["end"]:
            moved.append({"a": ra, "b": rb, "delta_h": round(delta_h, 1)})
    for oid, ra in a.items():
        if oid not in b:
            removed.append(ra)
    sa, sb = summarize(a_rows), summarize(b_rows)
    orders = []
    for key in sorted(set(sa["order_ends"]) | set(sb["order_ends"]), key=int):
        ea, eb = sa["order_ends"].get(key), sb["order_ends"].get(key)
        if ea == eb:
            continue
        name = next((r["order"] for r in b_rows + a_rows if str(r["order_id"]) == key), key)
        delta = ((_dt(eb) - _dt(ea)).total_seconds() / 3600) if ea and eb else None
        orders.append({"order": name, "a": ea, "b": eb, "delta_h": round(delta, 1) if delta is not None else None})
    moved.sort(key=lambda m: -abs(m["delta_h"]))
    return {"moved": moved, "relined": relined, "added": added, "removed": removed, "orders": orders,
            "a": sa, "b": sb, "changes": len(moved) + len(relined) + len(added) + len(removed)}


def _log(site, text, user):
    ScheduleChange.objects.create(site=site, action=ScheduleChange.Action.VERSION, description=text[:255],
                                  user=user if user and user.is_authenticated else None)


@transaction.atomic
def propose(version: PlanVersion, user=None) -> PlanVersion:
    if version.status not in (PlanVersion.Status.DRAFT, PlanVersion.Status.REJECTED):
        raise ValidationError("Solo se puede proponer una versión guardada o rechazada.")
    version.status = PlanVersion.Status.PROPOSED
    version.save(update_fields=["status"])
    _log(version.site, f"Versión v{version.number} propuesta para aprobación", user)
    return version


@transaction.atomic
def approve(version: PlanVersion, user=None, note: str = "", release: bool = True) -> dict:
    """Aprueba la versión; las demás aprobadas pasan a «reemplazada». Envía sus pedidos planeados al equipo de
    insumos, que es quien los libera a planta."""
    if version.status != PlanVersion.Status.PROPOSED:
        raise ValidationError("Solo se aprueban versiones propuestas.")
    PlanVersion.objects.filter(site=version.site, status=PlanVersion.Status.APPROVED).update(
        status=PlanVersion.Status.SUPERSEDED)
    version.status = PlanVersion.Status.APPROVED
    version.decided_by = user if user and user.is_authenticated else None
    version.decided_at = timezone.now()
    version.decision_note = note
    version.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])
    released = []
    if release:
        order_ids = {r["order_id"] for r in version.data}
        from .supplies import send_to_supplies

        for order in WorkOrder.objects.filter(pk__in=order_ids, status=WorkOrder.Status.PLANNED):
            try:
                send_to_supplies(order, user)
                released.append(order.number)
            except ValidationError:
                pass
    _log(version.site, f"Versión v{version.number} aprobada" + (f"; {len(released)} pedidos enviados a insumos" if released else ""), user)
    return {"version": version, "released": released}


@transaction.atomic
def reject(version: PlanVersion, user=None, note: str = "") -> PlanVersion:
    if version.status != PlanVersion.Status.PROPOSED:
        raise ValidationError("Solo se rechazan versiones propuestas.")
    if not note.strip():
        raise ValidationError("Escribe el motivo del rechazo para que planeación lo corrija.")
    version.status = PlanVersion.Status.REJECTED
    version.decided_by = user if user and user.is_authenticated else None
    version.decided_at = timezone.now()
    version.decision_note = note
    version.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])
    _log(version.site, f"Versión v{version.number} rechazada: {note}", user)
    return version


def baseline_for(site: Site, start: datetime) -> tuple[PlanVersion | None, list[dict], str]:
    """Plan con el que se compara lo real: la última versión aprobada creada antes del periodo, o la última
    guardada; si no hay versiones, el plan actual."""
    v = (PlanVersion.objects.filter(site=site, status__in=[PlanVersion.Status.APPROVED, PlanVersion.Status.SUPERSEDED],
                                    created_at__lte=start).order_by("-created_at").first()
         or PlanVersion.objects.filter(site=site, created_at__lte=start).order_by("-created_at").first()
         or PlanVersion.objects.filter(site=site).order_by("-created_at").first())
    if v:
        return v, v.data, f"Versión v{v.number} ({v.get_status_display().lower()}, {timezone.localtime(v.created_at):%d/%m %H:%M})"
    return None, current_plan(site), "Plan actual (no hay versiones guardadas)"


def adherence(site: Site, start: datetime, end: datetime, version: PlanVersion | None = None) -> dict:
    """Plan frente a lo real en [start, end): cantidad planeada (proporcional al tiempo) frente a la reportada."""
    if version is not None:
        rows, label = version.data, f"Versión v{version.number}"
    else:
        version, rows, label = baseline_for(site, start)
    ops_real = {o.pk: o for o in Operation.objects.filter(pk__in=[r["id"] for r in rows]).select_related("order", "work_center")}
    reports = defaultdict(float)
    for rep in ProductionReport.objects.filter(operation__order__site=site, reported_at__gte=start, reported_at__lt=end):
        reports[rep.operation_id] += float(rep.qty_good + rep.qty_scrap)
    planned_ids = set()
    items = []
    for r in rows:
        s, e = _dt(r["start"]), _dt(r["end"])
        if not s or not e or e <= start or s >= end:
            continue
        span = (e - s).total_seconds() or 1
        frac = (min(e, end) - max(s, start)).total_seconds() / span
        planned = r["qty"] * max(0.0, min(1.0, frac))
        real = reports.get(r["id"], 0.0)
        planned_ids.add(r["id"])
        op = ops_real.get(r["id"])
        delay = None
        if op and op.actual_start:
            delay = round((op.actual_start - s).total_seconds() / 60)
        pct = (real / planned * 100) if planned > 0 else None
        if real <= 0 and (not op or not op.actual_start):
            state = "no_iniciada"
        elif pct is not None and pct >= 95:
            state = "cumplida"
        else:
            state = "parcial"
        items.append({"row": r, "planned": planned, "real": real, "pct": round(pct) if pct is not None else None,
                      "delay": delay, "state": state, "line": r["line"]})
    unplanned = []
    for op_id, qty in reports.items():
        if op_id in planned_ids:
            continue
        op = Operation.objects.filter(pk=op_id).select_related("order", "work_center").first()
        if op:
            unplanned.append({"op": op, "real": qty})
    total_planned = sum(i["planned"] for i in items)
    hit = sum(min(i["real"], i["planned"]) for i in items)
    by_line: dict[str, dict] = defaultdict(lambda: {"planned": 0.0, "real": 0.0, "n": 0})
    for i in items:
        b = by_line[i["line"] or "—"]
        b["planned"] += i["planned"]
        b["real"] += min(i["real"], i["planned"])
        b["n"] += 1
    lines = [{"line": k, **v, "pct": round(v["real"] / v["planned"] * 100) if v["planned"] else None}
             for k, v in sorted(by_line.items())]
    starts = [i["delay"] for i in items if i["delay"] is not None]
    items.sort(key=lambda i: (i["state"] == "cumplida", i["line"], i["row"]["start"]))
    return {"items": items, "unplanned": unplanned, "lines": lines, "label": label, "version": version,
            "adherence": round(hit / total_planned * 100) if total_planned else None,
            "planned": total_planned, "real": sum(i["real"] for i in items),
            "on_time_starts": sum(1 for d in starts if d <= 15), "started": len(starts),
            "counts": {k: sum(1 for i in items if i["state"] == k) for k in ("cumplida", "parcial", "no_iniciada")}}
