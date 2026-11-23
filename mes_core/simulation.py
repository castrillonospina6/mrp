"""Simulación «¿qué pasa si…?», promesa de entrega y sugerencias de horas extra.

Todo se calcula dentro de una transacción que se deshace al final: los datos reales nunca cambian
hasta que el usuario pulsa «Aplicar».
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time

from . import planning, services, workcal
from .models import CalendarException, Downtime, Product, Shift, Site, WorkCenter, WorkOrder
from .scheduling import site_shifts

CHANGE_KINDS = {
    "pedido": "Entra un pedido nuevo",
    "linea_parada": "Una línea se para",
    "horas_extra": "Horas extra en un turno",
    "turno_adicional": "Turno adicional",
    "festivo": "Día no laboral / festivo",
    "prioridad": "Cambiar prioridad o compromiso de un pedido",
}


class _Rollback(Exception):
    def __init__(self, value):
        self.value = value


def _aware(d: date, t: time) -> datetime:
    return timezone.make_aware(datetime.combine(d, t))


DOW_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _d(value) -> str:
    d = parse_date(value) if isinstance(value, str) else value
    return f"{DOW_ES[d.weekday()]} {d:%d/%m/%Y}" if d else str(value)


def _dt_txt(value) -> str:
    try:
        dt = datetime.fromisoformat(value)
        return f"{DOW_ES[dt.weekday()][:3]} {dt:%d/%m %H:%M}"
    except (TypeError, ValueError):
        return str(value)


def describe(change: dict) -> str:
    k = change.get("kind")
    if k == "pedido":
        return f"Pedido nuevo: {float(change.get('quantity') or 0):g} × {change.get('product') or 'producto'} ({change.get('process_label', change.get('process_type'))}), prioridad {change.get('priority', 1)}" + (
            f", compromiso {_d(change['due'])}" if change.get("due") else "")
    if k == "linea_parada":
        return f"Línea {change.get('line_label', change.get('line'))} parada {_dt_txt(change.get('start'))} → {_dt_txt(change.get('end'))}"
    if k == "horas_extra":
        return f"Horas extra {change.get('shift_label', '')} el {_d(change.get('date'))} {change.get('start_time')}–{change.get('end_time')}" + (
            f" con {change['operators']} op." if change.get("operators") else "")
    if k == "turno_adicional":
        return f"Turno adicional el {_d(change.get('date'))} {change.get('start_time')}–{change.get('end_time')}" + (
            f" con {change['operators']} op." if change.get("operators") else "")
    if k == "festivo":
        return f"No se trabaja el {_d(change.get('date'))}"
    if k == "prioridad":
        parts = []
        if change.get("priority"):
            parts.append(f"prioridad {change['priority']}")
        if change.get("due"):
            parts.append(f"compromiso {_d(change['due'])}")
        return f"{change.get('order_label', change.get('order'))}: " + ", ".join(parts)
    return str(change)


def apply_change(site: Site, change: dict, user=None):
    """Aplica un cambio (dentro de la simulación o de verdad). Devuelve el objeto creado o modificado."""
    k = change.get("kind")
    u = user if user and user.is_authenticated else None
    if k == "pedido":
        product = None
        if change.get("product"):
            product, _ = services.resolve_product(change["product"])
        product = product or Product.objects.order_by("pk").first()
        if product is None:
            raise ValidationError("No hay productos creados.")
        due = parse_date(change.get("due") or "")
        return services.create_order(
            site=site, product=product, process_type=change["process_type"], quantity=Decimal(str(change["quantity"])),
            priority=int(change.get("priority") or 1), due_date=_aware(due, time(23, 59)) if due else None,
            reference=change.get("reference") or "Simulación", created_by=u)
    if k == "linea_parada":
        wc = WorkCenter.objects.get(pk=change["line"], site=site)
        start = timezone.make_aware(datetime.fromisoformat(change["start"]))
        end = timezone.make_aware(datetime.fromisoformat(change["end"]))
        if end <= start:
            raise ValidationError("El fin de la parada debe ser posterior al inicio.")
        return Downtime.objects.create(work_center=wc, start=start, end=end, reason=change.get("reason") or "Parada simulada",
                                       created_by=u)
    if k in ("horas_extra", "turno_adicional", "festivo"):
        d = parse_date(change["date"])
        kind = {"horas_extra": CalendarException.Kind.OVERTIME, "turno_adicional": CalendarException.Kind.EXTRA_SHIFT,
                "festivo": CalendarException.Kind.CLOSURE}[k]
        exc = CalendarException(site=site, kind=kind, date=d, name=change.get("name") or "",
                                operators=int(change.get("operators") or 0), created_by=u)
        if k == "horas_extra":
            exc.shift = Shift.objects.get(pk=change["shift"], site=site)
        if k != "festivo":
            exc.start_time = parse_time(change["start_time"])
            exc.end_time = parse_time(change["end_time"])
        exc.full_clean()
        exc.save()
        if change.get("lines"):
            exc.work_centers.set(WorkCenter.objects.filter(site=site, pk__in=change["lines"]))
        workcal.invalidate(site.pk)
        return exc
    if k == "prioridad":
        order = WorkOrder.objects.get(pk=change["order"], site=site)
        fields = []
        if change.get("priority"):
            order.priority = int(change["priority"])
            fields.append("priority")
        if change.get("due"):
            order.due_date = _aware(parse_date(change["due"]), time(23, 59))
            fields.append("due_date")
        if fields:
            order.save(update_fields=fields)
        return order
    raise ValidationError(f"Cambio desconocido: {k}")


@dataclass
class Outcome:
    orders: dict[int, dict] = field(default_factory=dict)
    late: int = 0
    end: datetime | None = None
    changeover: float = 0.0
    new_order: int | None = None
    error: str = ""


def _collect(site, result: planning.PlanResult, new_order: int | None) -> Outcome:
    out = Outcome(changeover=result.changeover_minutes, new_order=new_order)
    for o in WorkOrder.objects.filter(site=site, status__in=WorkOrder.OPEN_STATUSES).select_related("product", "customer"):
        late = o.is_late
        out.orders[o.pk] = {"number": o.number or "(nuevo)", "product": o.product.name, "customer": o.customer.name if o.customer else "",
                            "due": o.due_date, "end": o.planned_end, "late": late, "url": o.get_absolute_url() if o.number else ""}
        out.late += 1 if late else 0
        if o.planned_end and (out.end is None or o.planned_end > out.end):
            out.end = o.planned_end
    return out


def run(site: Site, changes: list[dict], user=None) -> Outcome:
    """Reprograma la planta con los cambios y devuelve el resultado, sin guardar nada."""
    try:
        with transaction.atomic():
            new_order = None
            for ch in changes:
                obj = apply_change(site, ch, user)
                if ch.get("kind") == "pedido":
                    new_order = obj.pk
            result = planning.generate_plan(site, log=False)
            raise _Rollback(_collect(site, result, new_order))
    except _Rollback as r:
        return r.value
    except (ValidationError, KeyError, ValueError, WorkCenter.DoesNotExist, Shift.DoesNotExist,
            WorkOrder.DoesNotExist) as exc:
        msg = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        return Outcome(error=f"No se pudo simular: {msg}")
    finally:
        workcal.invalidate(site.pk)


def compare(site: Site, scenarios: list[dict], user=None) -> dict:
    """Plan base (reprogramado sin cambios) frente a cada escenario."""
    base = run(site, [], user)
    results = [run(site, sc.get("changes", []), user) for sc in scenarios]
    ids = list(base.orders)
    for r in results:
        if r.new_order and r.new_order not in ids:
            ids.append(r.new_order)
    rows = []
    for oid in ids:
        b = base.orders.get(oid)
        cells, changed = [], False
        for r in results:
            o = r.orders.get(oid)
            delta = None
            if o and b and o["end"] and b["end"]:
                delta = round((o["end"] - b["end"]).total_seconds() / 3600, 1)
                if abs(delta) < 0.05:
                    delta = None
                changed |= delta is not None or o["late"] != b["late"]
            elif o and not b:
                changed = True
            cells.append({"o": o, "delta": delta})
        info = b or next((c["o"] for c in cells if c["o"]), None)
        rows.append({"id": oid, "info": info, "base": b, "cells": cells, "changed": changed,
                     "new": b is None})
    rows.sort(key=lambda r: (not r["new"], not r["changed"], -(max((abs(c["delta"] or 0) for c in r["cells"]), default=0))))
    return {"base": base, "results": results, "rows": rows}


def apply_scenario(site: Site, changes: list[dict], user=None) -> planning.PlanResult:
    with transaction.atomic():
        for ch in changes:
            apply_change(site, ch, user)
        result = planning.generate_plan(site, user=user)
    workcal.invalidate(site.pk)
    return result


# --- promesa de entrega -----------------------------------------------------------------

def promise(site: Site, *, process_type: str, quantity, product_text: str = "", priority: int = 3,
            transit_days: int = 0) -> dict:
    """¿Cuándo se puede entregar? Programa el pedido en el primer hueco sin mover lo que ya está planeado."""
    try:
        with transaction.atomic():
            product = None
            if product_text:
                product, _ = services.resolve_product(product_text)
            product = product or Product.objects.order_by("pk").first()
            if product is None:
                raise ValidationError("No hay productos creados.")
            order = services.create_order(site=site, product=product, process_type=process_type,
                                          quantity=Decimal(str(quantity)), priority=priority, reference="Promesa")
            if not order.operations.exists():
                raise ValidationError("El tipo de proceso no tiene ruta de operaciones.")
            planning.generate_plan(site, only_orders={order.pk}, log=False)
            order.refresh_from_db()
            ops = list(order.operations.all())
            end = order.planned_end
            hours = sum(o.man_hours for o in ops)
            raise _Rollback({"end": end, "start": order.planned_start, "ops": len(ops), "man_hours": hours})
    except _Rollback as r:
        data = r.value
    except ValidationError as exc:
        return {"error": "; ".join(exc.messages)}
    end = data["end"]
    if not end:
        return {"error": "No se pudo programar el pedido."}
    local_end = timezone.localtime(end)
    dispatch = local_end.date()
    for _ in range(60):  # primer día cuya jornada empieza después de terminar la producción
        if services.production_deadline(site, dispatch) >= end:
            break
        dispatch += timedelta(days=1)
    return {"start": data["start"], "end": end, "dispatch": dispatch, "delivery": dispatch + timedelta(days=transit_days),
            "ops": data["ops"], "man_hours": round(data["man_hours"], 1)}


# --- sugerencias de horas extra ------------------------------------------------------------

def overtime_candidates(site: Site, days: int = 7) -> list[dict]:
    """Opciones de horas extra / turnos adicionales a evaluar: turnos sobrecargados y días no laborables."""
    from .capacity import dashboard_data

    shifts = site_shifts(site)
    if not shifts:
        return []
    today = timezone.localdate()
    cands: list[dict] = []
    data = dashboard_data(site, day=today, shift=shifts[0])
    for row in data.get("grid", []):
        for c in row["cells"]:
            if c is None or c.unlimited or c.off or c.balance_h >= -0.05 or getattr(c.shift, "is_extra", False):
                continue
            people = max(1, c.available)
            need = -c.balance_h / people
            hours = min(4, max(1, math.ceil(need)))
            end = (datetime.combine(c.day, c.shift.end_time) + timedelta(hours=hours)).time()
            cands.append({
                "kind": "horas_extra", "shift": c.shift.pk, "shift_label": c.shift.name, "date": c.day.isoformat(),
                "start_time": c.shift.end_time.strftime("%H:%M"), "end_time": end.strftime("%H:%M"),
                "operators": people, "reason": f"{c.shift.name} {c.day:%d/%m} con sobrecarga de {-c.balance_h:.0f} h",
                "added_hh": hours * people,
            })
    cal = workcal.get_calendar(site.pk)
    day_shifts = [s for s in shifts if not s.crosses_midnight] or list(shifts)
    first = min(day_shifts, key=lambda s: s.start_time)
    found = 0
    for i in range(1, 15):  # próximos días sin trabajo (fin de semana, festivo)
        d = today + timedelta(days=i)
        if any(not cal.shift_off_reason(s, d) for s in shifts):
            continue
        ops = site.operators_total or cal.people(first, d) or 0
        dur = first.effective_minutes / 60
        cands.append({
            "kind": "turno_adicional", "date": d.isoformat(), "name": f"Turno adicional {d:%d/%m}",
            "start_time": first.start_time.strftime("%H:%M"), "end_time": first.end_time.strftime("%H:%M"),
            "operators": ops, "reason": f"El {DOW_ES[d.weekday()]} {d:%d/%m} normalmente no se trabaja",
            "added_hh": round(dur * ops),
        })
        found += 1
        if found >= 2:
            break
    return cands[:8]


def suggestions(site: Site, user=None) -> dict:
    base = run(site, [], user)
    out = []
    for cand in overtime_candidates(site):
        res = run(site, [cand], user)
        if res.error:
            continue
        fixed = [base.orders[i]["number"] for i, o in res.orders.items()
                 if i in base.orders and base.orders[i]["late"] and not o["late"]]
        gained = 0.0
        for i, o in res.orders.items():
            b = base.orders.get(i)
            if b and b["end"] and o["end"]:
                gained += max(0.0, (b["end"] - o["end"]).total_seconds() / 3600)
        out.append({"change": cand, "text": describe(cand), "late_before": base.late, "late_after": res.late,
                    "fixed": fixed, "hours_earlier": round(gained, 1), "end": res.end})
    out.sort(key=lambda x: (x["late_after"], -x["hours_earlier"], -x["change"].get("added_hh", 0)))
    return {"base": base, "options": out}
