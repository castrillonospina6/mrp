"""Tablero de planta (en vivo): qué está rodando, qué está asignado y qué está por asignar, línea por línea.

Estados de cada línea en el momento actual:
  rodando     operación en proceso (ya arrancó y no ha terminado)                → verde
  asignada    operación programada que cubre este momento pero aún no arranca    → verde claro
  mantenimiento  paro/mantenimiento activo                                       → morado
  por_asignar    sin operación ahora; la línea está disponible                    → gris rayado
  fuera_turno    la línea no trabaja en el turno actual                           → gris

Indicadores del turno actual (tipo OEE):
  Disponibilidad = (minutos hábiles transcurridos − paros) ÷ minutos hábiles transcurridos
  Rendimiento    = unidades reportadas ÷ unidades esperadas al ritmo estándar desde el inicio real
  Calidad        = buenas ÷ (buenas + merma)
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db.models import Q
from django.utils import timezone

from .models import Downtime, Operation, Shift, Site, WorkCenter, WorkOrder
from . import workcal
from .scheduling import OperatorLedger, center_shifts, local_tz, shift_window, site_shifts, working_minutes

DOW = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
DOW_LONG = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
STATES = {
    "rodando": "Rodando",
    "asignada": "Asignada · por iniciar",
    "mantenimiento": "Mantenimiento",
    "por_asignar": "Por asignar",
    "fuera_turno": "Fuera de turno",
}


class ExtraWindow:
    """Turno adicional en curso, con la misma forma que un turno para el tablero."""

    def __init__(self, x):
        self.pk = None
        self.name = x.label
        self.start_time = timezone.localtime(x.start).time()
        self.end_time = timezone.localtime(x.end).time()
        self.color = "#6b7280"
        self.lines = x.lines


def current_shift(shifts: list[Shift], now: datetime, site_id: int | None = None):
    """Turno (o franja de horas extra / turno adicional) que se trabaja ahora según el calendario laboral."""
    tz = local_tz()
    cal = workcal.get_calendar(site_id if site_id is not None else (shifts[0].site_id if shifts else None))
    for x in cal.extras:
        if x.start <= now < x.end:
            base = next((s for s in shifts if s.pk == x.shift_id), None)
            return (base or ExtraWindow(x)), x.start, x.end
    base = timezone.localtime(now, tz).date()
    for d in (base - timedelta(days=1), base, base + timedelta(days=1)):
        for s in shifts:
            a, b = shift_window(s, d, tz)
            if a <= now < b and not cal.shift_off_reason(s, d):
                return s, a, b
    return None, None, None


def next_shift_start(shifts: list[Shift], now: datetime) -> tuple[Shift | None, datetime | None]:
    tz = local_tz()
    base = timezone.localtime(now, tz).date()
    best = (None, None)
    for d in (base, base + timedelta(days=1), base + timedelta(days=2)):
        for s in shifts:
            a, _ = shift_window(s, d, tz)
            if a > now and (best[1] is None or a < best[1]):
                best = (s, a)
    return best


def _hm(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return ""
    loc, today = timezone.localtime(dt), timezone.localtime(now).date()
    if loc.date() == today:
        return loc.strftime("%H:%M")
    return f"{DOW[loc.weekday()]} {loc.day:02d} {loc.strftime('%H:%M')}"


def _overlap_minutes(a: datetime, b: datetime, c: datetime, d: datetime) -> float:
    lo, hi = max(a, c), min(b, d)
    return max(0.0, (hi - lo).total_seconds() / 60)


def _pct(value: float | None) -> int | None:
    return None if value is None else round(value * 100)


def _performance(op: Operation, shifts: list[Shift], now: datetime) -> float | None:
    """Unidades reportadas vs. las esperadas al ritmo estándar desde que arrancó."""
    if op.status != Operation.Status.IN_PROGRESS or not op.actual_start or not op.minutes_per_unit:
        return None
    elapsed = working_minutes(shifts, op.actual_start, now) - float(op.setup_minutes)
    if elapsed <= 0:
        return None
    expected = elapsed * max(1, op.crew) / float(op.minutes_per_unit)
    if expected <= 0:
        return None
    return float(op.qty_good + op.qty_scrap) / expected


def _quality(op: Operation) -> float | None:
    total = op.qty_good + op.qty_scrap
    return float(op.qty_good / total) if total else None


def _op_info(op: Operation, now: datetime) -> dict:
    order = op.order
    qty = float(op.qty)
    done = float(op.qty_good + op.qty_scrap)
    span = (op.planned_end - op.planned_start).total_seconds() if op.planned_start and op.planned_end else 0
    time_pct = None
    if span > 0:
        time_pct = round(max(0.0, min(1.0, (now - op.planned_start).total_seconds() / span)) * 100)
    return {
        "id": op.pk,
        "order": order.number,
        "order_url": order.get_absolute_url(),
        "op": op.name,
        "product": order.product.name,
        "sku": order.product.sku,
        "customer": order.customer.name if order.customer else "",
        "tier": order.customer.tier if order.customer else "",
        "qty": qty,
        "done": done,
        "unit": order.product.unit,
        "qty_pct": round(min(1.0, done / qty) * 100) if qty else None,
        "time_pct": time_pct,
        "crew": op.crew,
        "start": _hm(op.planned_start, now),
        "end": _hm(op.planned_end, now),
        "late": bool(op.planned_end and op.planned_end < now),
    }


def plant_board_data(site: Site, now: datetime | None = None) -> dict:
    now = now or timezone.now()
    tz = local_tz()
    shifts = site_shifts(site)
    shift, sh_start, sh_end = current_shift(shifts, now, site.pk) if (shifts or workcal.get_calendar(site.pk).extras) \
        else (None, None, None)
    nxt_shift, nxt_start = (None, None) if shift or not shifts else next_shift_start(shifts, now)
    horizon = (sh_end or now) + timedelta(hours=24)

    centers = list(WorkCenter.objects.filter(site=site, active=True).prefetch_related("shifts").order_by("code"))
    ops = list(
        Operation.objects.filter(work_center__in=centers)
        .exclude(status=Operation.Status.DONE)
        .exclude(order__status__in=[WorkOrder.Status.CANCELLED, WorkOrder.Status.DONE])
        .filter(Q(status=Operation.Status.IN_PROGRESS) | Q(planned_start__isnull=False, planned_end__gt=now - timedelta(hours=1),
                                                            planned_start__lt=horizon))
        .select_related("order__product", "order__customer")
        .order_by("planned_start")
    )
    backlog_by_kind: dict[str, int] = {}
    for kind in (Operation.objects.filter(order__site=site, planned_start__isnull=True)
                 .exclude(status=Operation.Status.DONE)
                 .exclude(order__status__in=[WorkOrder.Status.CANCELLED, WorkOrder.Status.DONE])
                 .values_list("work_center_kind", flat=True)):
        backlog_by_kind[kind] = backlog_by_kind.get(kind, 0) + 1

    window_from = sh_start or now
    downtimes = list(Downtime.objects.filter(work_center__in=centers, end__gt=window_from - timedelta(hours=1),
                                             start__lt=horizon).order_by("start"))

    counts = {k: 0 for k in STATES}
    down_shift_min = 0.0
    lines = []
    for wc in centers:
        wc_shifts = center_shifts(wc, shifts)
        mine = [o for o in ops if o.work_center_id == wc.pk]
        running = [o for o in mine if o.status == Operation.Status.IN_PROGRESS]
        assigned = [o for o in mine if o.status == Operation.Status.PENDING and o.planned_start
                    and o.planned_start <= now < o.planned_end]
        upcoming = [o for o in mine if o.status == Operation.Status.PENDING and o.planned_start and o.planned_start > now]
        dts = [d for d in downtimes if d.work_center_id == wc.pk]
        active_dt = next((d for d in dts if d.start <= now < d.end), None)
        if isinstance(shift, ExtraWindow):
            on_shift = not shift.lines or wc.pk in shift.lines
        else:
            on_shift = shift is None or not shifts or any(s.pk == shift.pk for s in wc_shifts)

        if running:
            state = "rodando"
        elif active_dt:
            state = "mantenimiento"
        elif assigned:
            state = "asignada"
        elif shifts and (shift is None or not on_shift):
            state = "fuera_turno"
        else:
            state = "por_asignar"
        counts[state] += 1

        current = running + assigned
        main = current[0] if current else None

        # Indicadores del turno en curso
        avail = perf = qual = None
        if shift and on_shift:
            planned = working_minutes(wc_shifts, sh_start, now)
            down = sum(_overlap_minutes(d.start, d.end, sh_start, now) for d in dts)
            if planned > 0:
                avail = max(0.0, (planned - min(down, planned)) / planned)
            down_shift_min += sum(_overlap_minutes(d.start, d.end, sh_start, sh_end) for d in dts)
        if running:
            perf = _performance(running[0], wc_shifts, now)
            qual = _quality(running[0])

        lines.append({
            "id": wc.pk,
            "code": wc.code,
            "name": wc.name,
            "kind": wc.kind,
            "kind_label": wc.kind.replace("_", " ").capitalize(),
            "capacity": wc.capacity,
            "state": state,
            "state_label": STATES[state],
            "busy": len(current),
            "crew": sum(max(1, o.crew) for o in current),
            "main": _op_info(main, now) if main else None,
            "more": max(0, len(current) - 1),
            "next": _op_info(upcoming[0], now) if upcoming else None,
            "downtime": {"reason": active_dt.reason, "end": _hm(active_dt.end, now)} if active_dt else None,
            "next_downtime": next(({"reason": d.reason, "start": _hm(d.start, now)} for d in dts if d.start > now), None),
            "backlog": backlog_by_kind.get(wc.kind, 0),
            "kpi": {"avail": _pct(avail), "perf": _pct(perf), "qual": _pct(qual)},
        })

    # Operarios: disponibles según el turno (bolsa de la planta); en uso = cuadrillas de lo que está en las líneas ahora.
    ledger = OperatorLedger(site, shifts, since=now)
    available = ledger.available_at(now)
    used = sum(line["crew"] for line in lines)
    loc = timezone.localtime(now, tz)
    return {
        "site": {"id": site.pk, "code": site.code, "name": site.name},
        "now": loc.isoformat(),
        "tz": str(tz),
        "day_label": f"{DOW_LONG[loc.weekday()]} {loc.day:02d}/{loc.month:02d}",
        "shift": {
            "name": shift.name, "start": shift.start_time.strftime("%H:%M"), "end": shift.end_time.strftime("%H:%M"),
            "color": shift.color,
        } if shift else None,
        "next_shift": {"name": nxt_shift.name, "start": _hm(nxt_start, now)} if nxt_shift else None,
        "downtime_minutes": round(down_shift_min),
        "stations": len(lines),
        "counts": counts,
        "operators": None if available is None else {"available": available, "used": used, "free": available - used},
        "lines": lines,
    }


def plant_board_all(sites: list[Site], now: datetime | None = None) -> dict:
    """Todas las bodegas en un solo tablero: las líneas agrupadas por bodega, cada una con su turno."""
    now = now or timezone.now()
    tz = local_tz()
    parts = [plant_board_data(s, now) for s in sites]
    groups = [p for p in parts if p["lines"]]
    counts = {k: sum(p["counts"][k] for p in groups) for k in STATES}
    ops = [p["operators"] for p in groups if p["operators"]]
    loc = timezone.localtime(now, tz)
    for p in groups:
        for line in p["lines"]:
            line["site"] = p["site"]["code"]
    return {
        "all": True,
        "site": None,
        "now": loc.isoformat(),
        "tz": str(tz),
        "day_label": f"{DOW_LONG[loc.weekday()]} {loc.day:02d}/{loc.month:02d}",
        "shift": None,
        "next_shift": None,
        "downtime_minutes": sum(p["downtime_minutes"] for p in groups),
        "stations": sum(p["stations"] for p in groups),
        "counts": counts,
        "operators": {k: sum(o[k] for o in ops) for k in ("available", "used", "free")} if ops else None,
        "groups": [{"site": p["site"], "shift": p["shift"], "next_shift": p["next_shift"], "counts": p["counts"],
                    "stations": p["stations"]} for p in groups],
        "lines": [line for p in groups for line in p["lines"]],
    }
