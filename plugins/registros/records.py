"""Registros de producción por persona, como la hoja «Registros» de la app anterior.

Cada reporte de producción del MES (de la tablet o del pedido) se abre en una fila por operario: lo que hizo esa
persona, de qué hora a qué hora, cuánto tiempo neto y con qué eficiencia. El tiempo es «encadenado» como en la app
anterior: cada marcación cuenta desde la anterior de la misma persona en el día (o desde el inicio del tramo, si se
reportó por tramos), sin los paros. Así la suma de las filas de un día da lo mismo que la eficiencia diaria de piso.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from mes_core.models import Employee, Operation, ProductionReport, VoidedReport, WorkOrder
from plugins.piso.models import IntervalReport, Mark, Stop

ZERO = Decimal(0)
GAP_MAX = timedelta(minutes=90)   # entre dos marcaciones: más que esto es una pausa, no trabajo


def local(dt):
    return timezone.localtime(dt) if dt else None


def day_bounds(d_from, d_to):
    tz = timezone.get_current_timezone()
    return (datetime.combine(d_from, time.min, tzinfo=tz),
            datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=tz))


def user_name(u) -> str:
    return (u.get_full_name() or u.get_username()) if u else ""


@dataclass
class Row:
    id: int                      # número del reporte (Lote / ID)
    order_id: int
    pedido: str
    op_cliente: str
    cliente: str
    proceso: str
    etapa: str
    linea: str
    clasificacion: str
    coordinador: str
    supernumerario: str
    registrado_por: str
    employee_id: int | None
    cedula: str
    operario: str
    ean: str
    inicio: datetime | None
    fin: datetime | None
    std_uh: float | None         # unidades / hora estándar de la etapa (por persona)
    total_envio: Decimal         # lo que trae el reporte completo
    unidades: Decimal            # lo que hizo esta persona
    merma: Decimal
    sobrantes: Decimal
    minutos_brutos: float | None
    minutos_paro: float
    observacion: str
    estado: str = "Activo"
    ultima_accion: str = ""
    std_min: float = 0.0
    ean_desc: str = ""
    voided_pk: int | None = None

    @property
    def fecha(self):
        return local(self.fin or self.inicio).date() if (self.fin or self.inicio) else None

    @property
    def minutos(self) -> float | None:
        if self.minutos_brutos is None:
            return None
        return max(0.0, self.minutos_brutos - self.minutos_paro)

    @property
    def horas(self) -> float | None:
        return round(self.minutos / 60, 2) if self.minutos is not None else None

    @property
    def uh_real(self) -> float | None:
        m = self.minutos
        return round(float(self.unidades) / (m / 60), 2) if m and m >= 1 else None

    @property
    def eficiencia(self) -> float | None:
        m = self.minutos
        return round(self.std_min / m * 100, 1) if m and m >= 1 and self.std_min else None


# ---------------------------------------------------------------- tiempo encadenado de cada marcación
def mark_windows(marks, stops_by_emp) -> dict[int, tuple]:
    """{id_marca: (desde, hasta, minutos_de_paro)} encadenando las marcaciones de cada persona (o de cada tablet de
    línea) dentro del día."""
    out = {}
    chains = defaultdict(list)
    for m in marks:
        who = ("e", m["employee_id"]) if m["employee_id"] else ("l", m["work_center_id"])
        chains[(who, local(m["at"]).date())].append(m)
    for (who, _d), ms in chains.items():
        ms.sort(key=lambda m: (m["at"], m["id"]))
        prev = None
        stops = stops_by_emp.get(who[1], []) if who[0] == "e" else []
        for m in ms:
            cands = [x for x in (m["since"], prev) if x]
            start = max(cands) if cands else m["at"]
            if not m["since"] and prev and m["at"] - prev > GAP_MAX:  # una pausa larga no cuenta como trabajo
                start = m["at"] - timedelta(minutes=float(m["qty"] * m["minutes_per_unit"]) or 0)
            start = min(start, m["at"])
            paro = sum(max(0.0, (min(s_end, m["at"]) - max(s_start, start)).total_seconds() / 60)
                       for s_start, s_end in stops if s_start < m["at"] and s_end > start)
            out[m["id"]] = (start, m["at"], paro)
            prev = max(prev, m["at"]) if prev else m["at"]
    return out


def _stops_for(emp_ids, a, b):
    now = timezone.now()
    out = defaultdict(list)
    for s in (Stop.objects.filter(employee_id__in=emp_ids, start__lt=b).filter(Q(end__isnull=True) | Q(end__gt=a))
              .values("employee_id", "start", "end")):
        out[s["employee_id"]].append((s["start"], s["end"] or now))
    return out


# ---------------------------------------------------------------- filas
REPORT_RELATED = ("operation__order__customer", "operation__order__product", "operation__supervisor",
                  "operation__work_center", "operation__stage", "employee", "reported_by", "edited_by")


def build_rows(reports) -> list[Row]:
    """Filas por persona de los reportes dados (QuerySet o lista de ProductionReport)."""
    from mes_core.service_types import label_for

    reports = list(reports)
    if not reports:
        return []
    ids = [r.pk for r in reports]
    marks = list(Mark.objects.filter(report_id__in=ids).exclude(kind=Mark.Kind.REWORK)
                 .values("id", "report_id", "employee_id", "work_center_id", "kind", "qty", "at", "since",
                         "minutes_per_unit"))
    # Para encadenar hacen falta todas las marcaciones del día de esas personas (no solo las de estos reportes)
    if marks:
        a = min(m["since"] or m["at"] for m in marks) - timedelta(hours=1)
        b = max(m["at"] for m in marks) + timedelta(minutes=1)
        a = day_bounds(local(a).date(), local(a).date())[0]
        emp_ids = {m["employee_id"] for m in marks if m["employee_id"]}
        wc_ids = {m["work_center_id"] for m in marks if not m["employee_id"]}
        chain = list(Mark.objects.filter(at__gte=a, at__lt=b).exclude(kind=Mark.Kind.REWORK)
                     .filter(Q(employee_id__in=emp_ids) | Q(employee__isnull=True, work_center_id__in=wc_ids))
                     .values("id", "report_id", "employee_id", "work_center_id", "kind", "qty", "at", "since",
                             "minutes_per_unit"))
        windows = mark_windows(chain, _stops_for(emp_ids, a, b))
    else:
        windows = {}
    by_report = defaultdict(list)
    for m in marks:
        by_report[m["report_id"]].append(m)
    supers = {i.report_id: i for i in IntervalReport.objects.filter(report_id__in=ids).select_related("employee")}
    emps = {e.pk: e for e in Employee.objects.filter(pk__in={m["employee_id"] for m in marks if m["employee_id"]})}
    eans = _ean_descriptions({r.operation.order_id for r in reports})
    rows = []
    for r in reports:
        op, order = r.operation, r.operation.order
        ir = supers.get(r.pk)
        sup = ir.employee.display_name if ir and ir.employee_id and ir.kind != IntervalReport.Kind.OWN else ""
        base = dict(
            id=r.pk, order_id=order.pk, pedido=order.number, op_cliente=order.external_op or order.purchase_order,
            cliente=order.customer.name if order.customer_id else "", proceso=op.name,
            etapa=op.stage.name if op.stage_id else op.name, linea=op.work_center.name if op.work_center_id else "",
            clasificacion=label_for(order.process_type) or "", coordinador=user_name(op.supervisor),
            supernumerario=sup, registrado_por=user_name(r.reported_by), ean=r.ean,
            std_uh=round(60 / float(op.minutes_per_unit), 2) if op.minutes_per_unit else None,
            total_envio=r.qty_good + r.qty_extra, observacion=r.comment,
            ultima_accion=(f"Editado — {user_name(r.edited_by)} — {r.edit_reason} — "
                           f"{local(r.edited_at):%Y-%m-%d %H:%M}") if r.edited_at else "")
        ms = by_report.get(r.pk)
        if not ms:  # reporte hecho en el MES (sin marcaciones): una sola fila
            e = r.employee
            rows.append(Row(**base, employee_id=r.employee_id, cedula=e.document if e else "",
                            operario=e.display_name if e else (base["registrado_por"] or "—"), inicio=r.reported_at,
                            fin=r.reported_at, unidades=r.qty_good + r.qty_extra, merma=r.qty_scrap,
                            sobrantes=r.qty_leftover, minutos_brutos=None, minutos_paro=0.0,
                            std_min=float((r.qty_good + r.qty_extra) * op.minutes_per_unit)))
            continue
        people = defaultdict(list)
        for m in ms:
            people[m["employee_id"]].append(m)
        first = True
        for eid, pm in sorted(people.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
            w = [windows.get(m["id"]) for m in pm if windows.get(m["id"])]
            e = emps.get(eid)
            rows.append(Row(
                **base, employee_id=eid, cedula=e.document if e else "",
                operario=e.display_name if e else f"Tablet de la línea {base['linea']}".strip(),
                inicio=min((x[0] for x in w), default=None), fin=max((x[1] for x in w), default=None),
                unidades=sum((m["qty"] for m in pm if m["kind"] == Mark.Kind.GOOD), ZERO),
                merma=sum((m["qty"] for m in pm if m["kind"] == Mark.Kind.SCRAP), ZERO),
                sobrantes=r.qty_leftover if first else ZERO,
                minutos_brutos=sum((x[1] - x[0]).total_seconds() / 60 for x in w) if w else None,
                minutos_paro=sum(x[2] for x in w),
                std_min=sum(float(m["qty"] * m["minutes_per_unit"]) for m in pm if m["kind"] == Mark.Kind.GOOD)))
            first = False
    for row in rows:
        row.ean_desc = eans.get((row.order_id, row.ean), "")
    return rows


def _ean_descriptions(order_ids) -> dict:
    try:
        from plugins.eanes.models import EanLine
    except Exception:  # noqa: BLE001 - módulo de EAN desinstalado
        return {}
    return {(o, e): d for o, e, d in EanLine.objects.filter(order_id__in=order_ids)
            .values_list("order_id", "ean", "description")}


def voided_rows(qs) -> list[Row]:
    """Los anulados, una fila por reporte (con lo que traía cuando se anuló)."""
    out = []
    for v in qs.select_related("order__customer", "operation__work_center", "operation__supervisor", "voided_by"):
        op, order = v.operation, v.order
        out.append(Row(
            id=v.original_id, order_id=order.pk, pedido=order.number,
            op_cliente=order.external_op or order.purchase_order, cliente=order.customer.name if order.customer_id else "",
            proceso=v.operation_name, etapa=v.operation_name, linea=op.work_center.name if op and op.work_center_id else "",
            clasificacion="", coordinador=user_name(op.supervisor) if op else "", supernumerario="",
            registrado_por="", employee_id=None, cedula="", operario=v.who, ean=v.ean, inicio=v.reported_at,
            fin=v.reported_at, std_uh=None, total_envio=v.qty_good, unidades=v.qty_good, merma=v.qty_scrap,
            sobrantes=v.qty_leftover, minutos_brutos=None, minutos_paro=0.0, observacion="", estado="Anulado",
            ultima_accion=f"Anulado — {user_name(v.voided_by)} — {v.reason} — {local(v.voided_at):%Y-%m-%d %H:%M}",
            voided_pk=v.pk))
    return out


# ---------------------------------------------------------------- búsqueda (como «Editar registros»)
@dataclass
class Filters:
    d_from: object = None
    d_to: object = None
    pedido: str = ""
    operario: str = ""
    coordinador: str = ""          # id de usuario
    supernumerario: str = ""       # id de operario
    proceso: str = ""
    ean: str = ""
    linea: str = ""                # id de línea
    cliente: str = ""
    estado: str = "activos"        # activos · anulados · todos
    order_id: int | None = None
    employee_id: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def any(self) -> bool:
        return any((self.d_from, self.pedido, self.operario, self.coordinador, self.supernumerario, self.proceso,
                    self.ean, self.linea, self.cliente, self.order_id, self.employee_id))


def _ean_forms(text):
    try:
        from plugins.eanes.eans import normalize
    except Exception:  # noqa: BLE001
        return [text]
    code = normalize(text)[0]
    forms = {text.strip()}
    if code:
        forms |= {code, code.lstrip("0") if len(code) == 13 and code.startswith("0") else code}
    return [f for f in forms if f]


def report_queryset(f: Filters):
    qs = ProductionReport.objects.select_related(*REPORT_RELATED)
    if f.d_from:
        a, b = day_bounds(f.d_from, f.d_to or f.d_from)
        qs = qs.filter(reported_at__gte=a, reported_at__lt=b)
    if f.order_id:
        qs = qs.filter(operation__order_id=f.order_id)
    if f.pedido:
        qs = qs.filter(Q(operation__order__number__icontains=f.pedido) | Q(operation__order__external_op__icontains=f.pedido)
                       | Q(operation__order__purchase_order__icontains=f.pedido)
                       | Q(operation__order__reference__icontains=f.pedido))
    if f.proceso:
        qs = qs.filter(Q(operation__name__icontains=f.proceso) | Q(operation__stage__name__icontains=f.proceso))
    if f.ean:
        qs = qs.filter(ean__in=_ean_forms(f.ean))
    if f.linea:
        qs = qs.filter(operation__work_center_id=f.linea)
    if f.cliente:
        qs = qs.filter(operation__order__customer__name__icontains=f.cliente)
    if f.coordinador:
        qs = qs.filter(operation__supervisor_id=f.coordinador)
    if f.supernumerario:
        qs = qs.filter(pk__in=IntervalReport.objects.filter(employee_id=f.supernumerario)
                       .exclude(kind=IntervalReport.Kind.OWN).values("report_id"))
    emp_ids = None
    if f.employee_id:
        emp_ids = [f.employee_id]
    elif f.operario:
        emp_ids = list(Employee.objects.filter(Q(full_name__icontains=f.operario) | Q(document__icontains=f.operario))
                       .values_list("pk", flat=True))
    if emp_ids is not None:
        qs = qs.filter(Q(employee_id__in=emp_ids) | Q(pk__in=Mark.objects.filter(employee_id__in=emp_ids)
                                                         .values("report_id")))
    return qs.order_by("-reported_at", "-pk"), emp_ids


def voided_queryset(f: Filters):
    qs = VoidedReport.objects.all()
    if f.d_from:
        a, b = day_bounds(f.d_from, f.d_to or f.d_from)
        qs = qs.filter(reported_at__gte=a, reported_at__lt=b)
    if f.order_id:
        qs = qs.filter(order_id=f.order_id)
    if f.pedido:
        qs = qs.filter(Q(order__number__icontains=f.pedido) | Q(order__external_op__icontains=f.pedido))
    if f.proceso:
        qs = qs.filter(operation_name__icontains=f.proceso)
    if f.ean:
        qs = qs.filter(ean__in=_ean_forms(f.ean))
    if f.operario:
        qs = qs.filter(who__icontains=f.operario)
    if f.cliente:
        qs = qs.filter(order__customer__name__icontains=f.cliente)
    if f.linea:
        qs = qs.filter(operation__work_center_id=f.linea)
    return qs.order_by("-voided_at")


def search(f: Filters, limit: int | None = 500) -> tuple[list[Row], bool]:
    """(filas, si se cortó en el límite). Los filtros por operario dejan solo las filas de esa persona."""
    rows: list[Row] = []
    cut = False
    if f.estado in ("activos", "todos"):
        qs, emp_ids = report_queryset(f)
        reps = list(qs[: (limit + 1) if limit else None])
        cut = bool(limit) and len(reps) > limit
        rows = build_rows(reps[:limit] if limit else reps)
        if emp_ids is not None:
            keep = set(emp_ids)
            rows = [r for r in rows if r.employee_id in keep]
    if f.estado in ("anulados", "todos"):
        rows += voided_rows(voided_queryset(f)[: limit or None])
    rows.sort(key=lambda r: (r.fin or r.inicio or timezone.now()), reverse=True)
    return rows, cut


# ---------------------------------------------------------------- seguimiento de pedidos (como «Seguimiento paquetes»)
def order_tracking(orders, rows_by_order=None) -> list[dict]:
    """Por pedido: lo procesado, lo pendiente, el estado (Pendiente / En proceso / Cumple / No cumple), horas y
    eficiencia, quién lo trabajó y la justificación si se cerró con faltante."""
    from mes_core.corrections import compliance

    orders = list(orders)
    if rows_by_order is None:
        rows_by_order = defaultdict(list)
        reps = ProductionReport.objects.filter(operation__order__in=orders).select_related(*REPORT_RELATED)
        for r in build_rows(reps):
            rows_by_order[r.order_id].append(r)
    out = []
    for o in orders:
        ops = list(o.operations.all())
        last = max(ops, key=lambda x: x.sequence) if ops else None
        processed = (last.qty_good + last.qty_leftover) if last else ZERO
        rows = rows_by_order.get(o.pk, [])
        mins = sum(r.minutos or 0 for r in rows)
        std = sum(r.std_min for r in rows if r.minutos)
        closure = getattr(o, "closure", None) if _has_closure(o) else None
        state = compliance(o)
        if not state:
            if o.status == WorkOrder.Status.CANCELLED:
                state = "Cancelado"
            elif any(op.qty_good or op.qty_leftover or op.qty_scrap for op in ops):
                state = "En proceso"
            else:
                state = "Pendiente"
        pending = closure.total if closure and closure.total else max(ZERO, o.quantity - processed)
        out.append({
            "order": o, "processed": processed, "pending": pending, "state": state, "hours": round(mins / 60, 2),
            "eff": round(std / mins * 100, 1) if mins >= 1 and std else None,
            "supers": sorted({r.supernumerario for r in rows if r.supernumerario}),
            "coords": sorted({r.coordinador for r in rows if r.coordinador}),
            "people": len({r.employee_id for r in rows if r.employee_id}),
            "closure": closure, "last": max((r.fin for r in rows if r.fin), default=None),
        })
    return out


def _has_closure(o) -> bool:
    from mes_core.models import OrderClosure

    try:
        o.closure
        return True
    except OrderClosure.DoesNotExist:
        return False


def ean_matrix(order) -> dict:
    """Avance de cada referencia en cada etapa del pedido (si tiene EAN)."""
    try:
        from plugins.eanes.eans import MODE_ON, mode_of, stage_progress
        from plugins.eanes.models import EanLine
    except Exception:  # noqa: BLE001
        return {"mode": "", "stages": [], "rows": []}
    mode = mode_of(order)
    if mode != MODE_ON:
        return {"mode": mode, "stages": [], "rows": []}
    lines = list(EanLine.objects.filter(order=order))
    stages, seen = [], set()
    for op in order.operations.select_related("stage").order_by("sequence"):
        key = op.stage_id or f"op{op.pk}"
        if key in seen:
            continue
        seen.add(key)
        stages.append((op.stage.name if op.stage_id else op.name, stage_progress(op, lines=lines)))
    rows = []
    for line in lines:
        cells = [prog[line.ean] for _name, prog in stages]
        rows.append({"ean": line.ean, "desc": line.description, "qty": line.quantity, "cells": cells,
                     "final": cells[-1] if cells else None})
    return {"mode": mode, "stages": [s[0] for s in stages], "rows": rows}


# ---------------------------------------------------------------- control de horas (como «Control_Horas»)
def hours_control(rows: list[Row]) -> list[dict]:
    """Por operario y día: horas registradas (encadenadas, sin paros) frente a las de su turno."""
    groups = defaultdict(list)
    for r in rows:
        if r.employee_id and r.estado == "Activo" and r.fecha:
            groups[(r.fecha, r.employee_id)].append(r)
    emps = {e.pk: e for e in Employee.objects.filter(pk__in={k[1] for k in groups}).select_related("shift")}
    out = []
    for (d, eid), rs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[1][0].operario)):
        e = emps.get(eid)
        reg = sum(r.minutos or 0 for r in rs) / 60
        exp = e.shift.effective_minutes / 60 if e and e.shift_id else None
        diff = round((reg - exp) * 60) if exp is not None else None
        sup = Counter(r.supernumerario for r in rs if r.supernumerario).most_common(1)
        out.append({"fecha": d, "operario": rs[0].operario, "cedula": rs[0].cedula, "registradas": round(reg, 2),
                    "esperadas": round(exp, 2) if exp is not None else None, "diferencia": diff,
                    "estado": "" if diff is None else ("Correcto" if abs(diff) <= 15 else "Revisar"),
                    "supernumerario": sup[0][0] if sup else "",
                    "unidades": sum(r.unidades for r in rs),
                    "eficiencia": round(sum(r.std_min for r in rs if r.minutos) / (reg * 60) * 100, 1)
                    if reg * 60 >= 1 else None})
    return out


def operations_for_edit(current: Operation):
    """Operaciones a las que se puede pasar un reporte: las del mismo pedido y las de pedidos en planta."""
    return (Operation.objects.filter(Q(order_id=current.order_id) | Q(order__status__in=(
        WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS)))
        .select_related("order").order_by("-order__pk", "sequence")[:400])
