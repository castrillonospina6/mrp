"""Planificador automático de capacidad finita (programación hacia adelante).

Reglas:
  * Órdenes abiertas: en proceso primero; luego por puntaje de prioridad (reglas de la planta: fecha
    compromiso, nivel del cliente, antigüedad y prioridad manual). Sin reglas configuradas: prioridad,
    fecha compromiso y antigüedad.
  * Con «agrupar familias» activo, entre pedidos de puntaje parecido se juntan los de la misma familia de
    producto para perder menos tiempo en cambios.
  * Operaciones de una orden en secuencia; las partes de una operación dividida trabajan en paralelo.
  * Cada operación va a la línea de su tipo que la termine más temprano (capacidad = estaciones en paralelo),
    solo dentro del calendario laboral de la línea (turnos, festivos, horas extra…) y evitando mantenimientos.
  * Cambio de producto: si la estación venía de otra familia se suma su tiempo de cambio.
  * Plan congelado: lo que empieza dentro del horizonte congelado no se mueve.
  * ``keep_existing=True``: respeta lo ya programado y solo programa el backlog.
  * ``only_orders``: programa solo esas órdenes y deja todo lo demás quieto (promesa de entrega).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from . import signals
from .models import (
    ChangeoverTime, Customer, Downtime, Operation, PlanningRules, ScheduleChange, Site, WorkCenter, WorkOrder,
)
from .scheduling import (
    center_shifts, fit_avoiding, fit_ending_after, flow_reference, predecessors, refresh_order_dates, remaining_minutes, site_shifts,
)

DEFAULT_WEIGHTS = {"w_due": 4, "w_tier": 3, "w_age": 1, "w_priority": 5}


@dataclass
class PlanResult:
    start: datetime
    orders: list[WorkOrder] = field(default_factory=list)
    late_orders: list[WorkOrder] = field(default_factory=list)
    unassigned_operations: list[Operation] = field(default_factory=list)
    scheduled_operations: int = 0
    changeover_minutes: float = 0.0
    split_operations: int = 0
    frozen_operations: int = 0

    @property
    def end(self) -> datetime | None:
        ends = [o.planned_end for o in self.orders if o.planned_end]
        return max(ends) if ends else None


def get_rules(site) -> PlanningRules | None:
    return PlanningRules.objects.filter(site=site).first()


def priority_score(order: WorkOrder, rules: PlanningRules | None = None, now: datetime | None = None) -> dict:
    """Puntaje 0–100 (más alto = se programa antes) y su desglose."""
    now = now or timezone.now()
    w = {k: getattr(rules, k) for k in DEFAULT_WEIGHTS} if rules else dict(DEFAULT_WEIGHTS)
    if order.due_date:
        hours = (order.due_date - now).total_seconds() / 3600
        due = 1.25 if hours < 0 else max(0.0, 1 - hours / (14 * 24))
    else:
        due = 0.0
    tier = Customer.TIER_RANK.get(order.customer.tier, 0) / 4 if order.customer_id else 0.0
    ref = order.received_on or (order.created_at.date() if order.created_at else now.date())
    age = min(1.0, max(0, (timezone.localdate(now) - ref).days) / 30)
    prio = max(0.0, min(1.0, (5 - order.priority) / 4))
    total_w = sum(w.values()) or 1
    score = 100 * (w["w_due"] * due + w["w_tier"] * tier + w["w_age"] * age + w["w_priority"] * prio) / total_w
    return {"score": round(min(score, 125), 1), "due": round(due * 100), "tier": round(tier * 100),
            "age": round(age * 100), "priority": round(prio * 100)}


def _family(op: Operation) -> str:
    return (op.order.product.family or "").strip()


class Changeovers:
    """Busca el tiempo de cambio entre familias (tabla específica → por defecto de la planta)."""

    def __init__(self, site, rules: PlanningRules | None):
        self.default = rules.default_changeover_minutes if rules else 0
        self.table = {(c.work_center_kind, c.from_family.lower(), c.to_family.lower()): c.minutes
                      for c in ChangeoverTime.objects.filter(site=site)}
        self.enabled = bool(self.table) or bool(self.default)

    def minutes(self, kind: str, prev: str, new: str) -> float:
        if not prev or not new or prev.lower() == new.lower():
            return 0.0
        key = (prev.lower(), new.lower())
        for k in (kind, ""):
            if (k, *key) in self.table:
                return float(self.table[(k, *key)])
        return float(self.default)


def _base_minutes(op: Operation) -> float:
    """Minutos de trabajo sin el cambio de producto (que el plan recalcula)."""
    if op.status == Operation.Status.IN_PROGRESS:
        return remaining_minutes(op)
    return float(op.setup_minutes + op.minutes_per_unit * op.qty / max(1, op.crew))


def _auto_split(order_ops: list[Operation], by_kind, rules: PlanningRules) -> int:
    """Divide operaciones pendientes muy largas entre las líneas compatibles (plan automático)."""
    created = 0
    limit = max(1, rules.split_threshold_hours) * 60
    max_seq = max((o.sequence for o in order_ops), default=0)
    for op in list(order_ops):
        if op.status != Operation.Status.PENDING or op.split_of_id or any(p.split_of_id == op.pk for p in order_ops):
            continue
        lines = by_kind.get(op.work_center_kind) or []
        minutes = _base_minutes(op)
        if len(lines) < 2 or minutes <= limit:
            continue
        k = min(len(lines), math.ceil(minutes / limit))
        if k < 2:
            continue
        qty = op.qty
        part_qty = (qty / k).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        base_name = op.name.split(" · parte ")[0]
        op.quantity = qty - part_qty * (k - 1)
        op.name = f"{base_name} · parte 1/{k}"[:120]
        op.save(update_fields=["quantity", "name"])
        for i in range(1, k):
            max_seq += 1
            part = Operation.objects.create(
                order=op.order, stage=op.stage, quantity=part_qty, crew=op.crew, sequence=max_seq,
                name=f"{base_name} · parte {i + 1}/{k}"[:120], work_center_kind=op.work_center_kind,
                setup_minutes=op.setup_minutes, minutes_per_unit=op.minutes_per_unit, split_of=op)
            part.order = op.order
            order_ops.append(part)
            created += 1
    return created


@transaction.atomic
def generate_plan(site: Site, start: datetime | None = None, keep_existing: bool = False, user=None,
                  only_orders: set[int] | None = None, log: bool = True) -> PlanResult:
    start = timezone.localtime(start or timezone.now())
    result = PlanResult(start=start)
    rules = get_rules(site)
    changeovers = Changeovers(site, rules)
    frozen_limit = timezone.now() + timedelta(hours=rules.frozen_hours) if rules and rules.frozen_hours else None

    centers = list(WorkCenter.objects.filter(site=site, active=True).prefetch_related("shifts"))
    site_sh = site_shifts(site)
    shifts = {wc.pk: center_shifts(wc, site_sh) for wc in centers}
    by_kind: dict[str, list[WorkCenter]] = defaultdict(list)
    for wc in centers:
        by_kind[wc.kind].append(wc)
    blocked: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
    for d in Downtime.objects.filter(work_center__in=centers, end__gt=start):
        blocked[d.work_center_id].append((d.start, d.end))
    # Estado de cada estación paralela: [libre desde, última familia procesada]
    slots: dict[int, list[list]] = {wc.pk: [[start, ""] for _ in range(wc.capacity)] for wc in centers}

    orders = list(WorkOrder.objects.filter(site=site, status__in=WorkOrder.OPEN_STATUSES)
                  .select_related("customer", "product"))
    now = timezone.now()
    scores = {o.pk: priority_score(o, rules, now)["score"] for o in orders}
    far = datetime.max.replace(tzinfo=start.tzinfo)

    def base_key(o):
        return (0 if o.status == WorkOrder.Status.IN_PROGRESS else 1,)

    if rules:
        if rules.group_changeovers:
            orders.sort(key=lambda o: (*base_key(o), -round(scores[o.pk] / 10), (o.product.family or "~").lower(),
                                       -scores[o.pk], o.due_date or far, o.created_at))
        else:
            orders.sort(key=lambda o: (*base_key(o), -scores[o.pk], o.due_date or far, o.created_at))
    else:  # comportamiento clásico
        orders.sort(key=lambda o: (*base_key(o), o.priority, o.due_date or far, o.created_at))

    ops_by_order: dict[int, list[Operation]] = defaultdict(list)
    for op in Operation.objects.filter(order__in=orders).select_related("order__product").order_by("order_id", "sequence"):
        ops_by_order[op.order_id].append(op)
    order_by_id = {o.pk: o for o in orders}
    for oid, ops in ops_by_order.items():
        for op in ops:
            op.order = order_by_id[oid]

    def is_fixed(op: Operation) -> bool:
        if op.planned_start is None:
            return False
        if keep_existing:
            return True
        if only_orders is not None and op.order_id not in only_orders:
            return True
        return bool(frozen_limit and op.planned_start < frozen_limit)

    if rules and rules.allow_split:
        for order in orders:
            if only_orders is not None and order.pk not in only_orders:
                continue
            pending = [op for op in ops_by_order[order.pk] if not is_fixed(op)]
            if pending:
                result.split_operations += _auto_split(ops_by_order[order.pk], by_kind, rules)

    # Lo fijo (manual, congelado o de otras órdenes) ocupa sus estaciones
    fixed_ops = sorted((op for ops in ops_by_order.values() for op in ops
                        if is_fixed(op) and op.status != Operation.Status.DONE and op.work_center_id in slots
                        and op.planned_end and op.planned_end > start), key=lambda o: o.planned_end)
    for op in fixed_ops:
        st = slots[op.work_center_id]
        i = min(range(len(st)), key=lambda k: st[k][0])
        st[i][0] = max(st[i][0], op.planned_end)
        st[i][1] = _family(op)
        if frozen_limit and not keep_existing and op.planned_start < frozen_limit:
            result.frozen_operations += 1

    ops_to_save: list[Operation] = []
    for order in orders:
        if only_orders is not None and order.pk not in only_orders:
            continue
        ops = ops_by_order[order.pk]
        roots = {op.pk: op for op in ops if not op.split_of_id}
        groups: dict[int, list[Operation]] = defaultdict(list)
        for op in ops:
            groups[op.split_of_id or op.pk].append(op)
        ordered_groups = sorted(groups.items(), key=lambda kv: roots[kv[0]].sequence if kv[0] in roots else 0)
        root_list = list(roots.values())
        prev_end = start
        for root_id, group in ordered_groups:
            root = roots.get(root_id)
            group_start, ends = prev_end, []
            min_end = None
            if root is not None and root.stage_id is not None:
                # etapas planeadas: según la relación de la etapa (en cola, a la par o al mismo tiempo)
                group_start = start
                for p in predecessors(root, root_list):
                    if p.status != Operation.Status.DONE and p.planned_end and p.planned_end > group_start:
                        group_start = p.planned_end
                flow = flow_reference(root, root_list)
                if flow:
                    ref, lag = flow
                    placed = [p for p in ref if p.planned_start and p.status != Operation.Status.DONE]
                    if placed:
                        first = min(placed, key=lambda p: p.planned_start)
                        per_unit = float(first.minutes_per_unit or 0) / max(1, first.crew)
                        group_start = max(group_start, first.planned_start
                                          + timedelta(minutes=float(first.setup_minutes or 0) + lag * per_unit))
                        mine = [x for x in root_list if x.sequence // 100 == root.sequence // 100
                                and x.sequence % 100 != 99]
                        if root.sequence == max(x.sequence for x in mine):  # el último lote no termina antes
                            min_end = max(p.planned_end for p in placed)
            for op in group:
                if op.status == Operation.Status.DONE:
                    continue
                if is_fixed(op):
                    ends.append(op.planned_end)
                    continue
                base = _base_minutes(op)
                fam = _family(op)
                candidates = by_kind.get(op.work_center_kind) or [wc for wc in centers if wc.pk == op.work_center_id]
                if not candidates:
                    op.planned_start, op.planned_end = group_start, group_start + timedelta(minutes=base)
                    op.changeover_minutes = Decimal("0")
                    result.unassigned_operations.append(op)
                else:
                    best = None  # (fin, inicio, centro, estación, cambio)
                    for wc in candidates:
                        for idx, (free_at, last_fam) in enumerate(slots[wc.pk]):
                            co = changeovers.minutes(wc.kind, last_fam, fam) if op.status == Operation.Status.PENDING else 0.0
                            # (a la par: el último lote no termina antes que la etapa anterior)
                            s, e = fit_ending_after(max(group_start, free_at), base + co, shifts[wc.pk], blocked[wc.pk],
                                                    min_end)
                            if best is None or e < best[0]:
                                best = (e, s, wc, idx, co)
                    e, s, wc, idx, co = best
                    slots[wc.pk][idx] = [e, fam or slots[wc.pk][idx][1]]
                    op.work_center = wc
                    op.planned_start, op.planned_end = s, e
                    op.changeover_minutes = Decimal(str(round(co, 2)))
                    result.changeover_minutes += co
                ends.append(op.planned_end)
                ops_to_save.append(op)
            if ends:
                prev_end = max(ends)

    Operation.objects.bulk_update(ops_to_save, ["work_center", "planned_start", "planned_end", "changeover_minutes"])
    if ops_to_save:
        from .staffing import apply_line_rules

        apply_line_rules(ops_to_save)  # supervisor por línea y turno
    result.scheduled_operations = len(ops_to_save)
    touched = {op.order_id for op in ops_to_save}
    for order in orders:
        if only_orders is not None and order.pk not in only_orders:
            continue
        if order.pk in touched or not keep_existing:
            refresh_order_dates(order)
        result.orders.append(order)
        if order.is_late:
            result.late_orders.append(order)

    if log:
        extras = []
        if keep_existing:
            extras.append("respetando lo manual")
        if result.frozen_operations:
            extras.append(f"{result.frozen_operations} congeladas")
        if result.changeover_minutes:
            extras.append(f"{round(result.changeover_minutes)} min de cambios")
        if result.split_operations:
            extras.append(f"{result.split_operations} partes nuevas")
        ScheduleChange.objects.create(
            site=site, action=ScheduleChange.Action.AUTO,
            description=(f"Plan automático: {len(ops_to_save)} operaciones programadas"
                         + (f" ({', '.join(extras)})" if extras else ""))[:255],
            user=user if user and user.is_authenticated else None,
        )
        signals.plan_generated.send(sender=Site, site=site, result=result)
    return result


def ordered_backlog(site, ops: list[Operation]) -> list[Operation]:
    """Ordena operaciones del backlog por el puntaje de prioridad de su orden."""
    rules = get_rules(site)
    now = timezone.now()
    cache: dict[int, float] = {}

    def score(op):
        if op.order_id not in cache:
            cache[op.order_id] = priority_score(op.order, rules, now)["score"]
        return cache[op.order_id]

    far = datetime.max.replace(tzinfo=timezone.get_current_timezone())
    return sorted(ops, key=lambda op: (-score(op), op.order.due_date or far, op.order_id, op.sequence))


__all__ = ["PlanResult", "generate_plan", "priority_score", "get_rules", "ordered_backlog"]
