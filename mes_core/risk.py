"""Alertas de atraso: si lo reportado va por debajo de lo programado a la fecha, la operación (y su pedido) queda
«en riesgo» y el sistema sugiere qué hacer: reprogramar, añadir gente o añadir horas / turno.

Lo programado a la fecha se mide en horas HÁBILES (turnos, descansos, festivos y cierres del calendario), no en horas
de reloj: una operación de dos días no «se atrasa» de noche."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from .models import Operation, WorkOrder

ACTIVE = (WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS)
LOOKBACK = timedelta(days=7)  # operaciones que debían terminar hace más de esto ya no se vigilan


@dataclass
class Risk:
    op: Operation
    expected: Decimal
    actual: Decimal
    behind_pct: int
    overdue: bool
    suggestions: list[str] = field(default_factory=list)

    @property
    def missing(self) -> Decimal:
        return max(Decimal("0"), self.expected - self.actual)

    @property
    def label(self) -> str:
        return "Atrasada" if self.overdue else "En riesgo"


def _fmt(q) -> str:
    q = Decimal(q).quantize(Decimal("0.01"))
    return f"{q.normalize():f}" if q == q.to_integral() else f"{q:f}".rstrip("0").rstrip(".")


class _Clock:
    """Minutos hábiles entre dos instantes para una línea (con caché de turnos y calendario por planta)."""

    def __init__(self, site):
        from .scheduling import center_shifts, site_shifts
        from .workcal import get_calendar

        self.cal = get_calendar(site.pk)
        self.base = site_shifts(site)
        self._center_shifts = center_shifts
        self._by_wc = {}

    def minutes(self, op: Operation, a: datetime, b: datetime) -> float:
        if b <= a:
            return 0.0
        wc = op.work_center
        if wc is not None and wc.pk not in self._by_wc:
            self._by_wc[wc.pk] = self._center_shifts(wc, self.base)
        shifts = self._by_wc.get(wc.pk) if wc is not None else self.base
        wins = self.cal.windows(shifts, a, b, wc.pk if wc else None)
        return sum((e - s).total_seconds() for s, e in wins) / 60


def evaluate(op: Operation, now: datetime, clock: _Clock, tolerance_pct: int) -> Risk | None:
    if op.status == Operation.Status.DONE or not op.planned_start or not op.planned_end or now <= op.planned_start:
        return None
    qty, actual = op.qty, op.qty_good
    total = clock.minutes(op, op.planned_start, op.planned_end)
    elapsed = clock.minutes(op, op.planned_start, min(now, op.planned_end))
    if total <= 0:  # sin ventanas hábiles registradas: reloj
        total = (op.planned_end - op.planned_start).total_seconds() / 60
        elapsed = (min(now, op.planned_end) - op.planned_start).total_seconds() / 60
    overdue = now >= op.planned_end
    frac = Decimal("1") if overdue else Decimal(str(max(0.0, min(1.0, elapsed / total)))) if total else Decimal("0")
    expected = qty * frac
    if expected < 1 or actual >= qty:
        return None
    gap = expected - actual
    if gap <= expected * Decimal(tolerance_pct) / 100:
        return None
    behind = int(round(float(gap / expected) * 100))
    r = Risk(op, expected, actual, behind, overdue)
    remaining = qty - actual
    left_min = max(0.0, total - elapsed)
    pace = float(actual) / elapsed if elapsed > 0 and actual > 0 else 0.0  # unidades por minuto hábil
    crew = max(1, op.crew)
    if actual <= 0:
        r.suggestions.append("No hay producción reportada: confirma que arrancó y que el supervisor esté reportando.")
    if overdue:
        r.suggestions.append(f"Ya pasó el fin programado y faltan {_fmt(remaining)} u. Reprograma lo pendiente en el programador.")
    elif pace > 0 and left_min > 0:
        need = float(remaining) / left_min
        if need > pace:
            extra = math.ceil(crew * need / pace) - crew
            r.suggestions.append(f"Para terminar a tiempo hacen falta unas {extra} persona(s) más en la cuadrilla "
                                 f"(hoy {crew}).")
            hours = (float(remaining) / pace - left_min) / 60
            r.suggestions.append(f"O, con la misma gente, unas {hours:.1f} h más (horas extra o un turno adicional).")
    if not overdue:
        r.suggestions.append("Si no se puede recuperar, reprograma la operación y avisa a ventas si cambia el compromiso.")
    return r


def tolerance(site) -> int:
    from .planning import get_rules

    rules = get_rules(site)
    return rules.risk_tolerance_pct if rules else 10


def site_risks(site, now: datetime | None = None, ops=None) -> list[Risk]:
    now = now or timezone.now()
    if ops is None:
        ops = (Operation.objects.filter(order__site=site, order__status__in=ACTIVE, planned_start__lte=now,
                                        planned_end__gte=now - LOOKBACK)
               .exclude(status=Operation.Status.DONE)
               .select_related("order", "order__product", "order__customer", "work_center", "supervisor"))
    clock, tol = _Clock(site), tolerance(site)
    out = [r for r in (evaluate(op, now, clock, tol) for op in ops) if r]
    out.sort(key=lambda r: (not r.overdue, -r.behind_pct))
    return out


def order_risks(order: WorkOrder, now: datetime | None = None) -> list[Risk]:
    if order.status not in ACTIVE:
        return []
    ops = order.operations.select_related("order", "work_center", "supervisor").exclude(status=Operation.Status.DONE)
    return site_risks(order.site, now, ops)


def check(site, now: datetime | None = None) -> int:
    """Avisa (una vez al día por operación) a planeación y al supervisor de cada operación en riesgo."""
    from .notify import K, notify, users_with_perm

    now = now or timezone.now()
    risks = site_risks(site, now)
    if not risks:
        return 0
    planners = list(users_with_perm("plan_workorder"))
    day = timezone.localdate(now)
    sent = 0
    for r in risks:
        op = r.op
        title = f"{op.order.number} {r.label.lower()}: {op.name}"
        body = (f"Reportado {_fmt(r.actual)} de {_fmt(r.expected)} u. que debían llevar a esta hora "
                f"({r.behind_pct} % por debajo). " + " ".join(r.suggestions[:2]))
        users = planners + ([op.supervisor] if op.supervisor_id else [])
        sent += notify(users, K.AT_RISK, title, body, reverse("mes_core:at_risk") + f"?site={site.pk}", site,
                       f"risk:{op.pk}:{day}")
    return sent
