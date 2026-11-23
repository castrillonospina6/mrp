"""Exportar a Excel con las mismas hojas y columnas del libro de la app anterior («Control de la produccion»), para
que los indicadores que se sacan de ese informe sigan funcionando igual."""

from __future__ import annotations

import io
from collections import defaultdict
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from mes_core.models import ProductionReport, WorkOrder

from . import records
from .records import Filters, local

SHEETS = {
    "registros": "Registros",
    "seguimiento": "Seguimiento paquetes",
    "eanes": "Validacion eanes",
    "horas": "Control_Horas",
}

# Mismas columnas (y en el mismo orden) que la hoja «Registros»; al final, las que el MES agrega.
REG_COLS = ["Lote ID", "Fecha", "Coordinador", "Cedula operario", "Operario", "Supernumerario", "Cliente", "Proceso",
            "Unidades hora estandar", "Sublinea", "Evento", "Numero pedido", "EAN", "Unidades totales procesadas",
            "Hora inicio", "Hora final", "Suplemento", "Observación", "Total tiempo", "Clasificacion proceso",
            "Tiempo total", "Unidades procesadas operario", "Unidades hora real", "Eficiencia", "ID D1",
            "Última acción",
            "Códigos sobrantes", "Merma", "Línea", "Etapa", "OP / OC cliente", "Descripción EAN", "Registrado por",
            "Estado"]
SEG_COLS = ["Fecha", "Numero pedido", "Sublinea - Cliente", "Unds procesadas", "Unds pendientes", "Eficiencia",
            "Supernumerarios", "Supervisores", "Estado", "Justificación",
            "Producto", "Unidades del pedido", "Horas", "Personas", "Estado del pedido", "OP / OC cliente"]
EAN_COLS = ["Numero pedido", "Sublinea", "Ean producto", "Unidades totales", "Unidades procesadas", "Operarios",
            "Estado", "Códigos sobrantes", "Descripción", "Justificado", "Etapa", "Cliente"]
HOR_COLS = ["Fecha", "Operario", "Horas Registradas", "Horas Esperadas", "Diferencia Minutos", "Estado",
            "Supernumerario Principal", "Cedula operario", "Unidades", "Eficiencia"]


def _n(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def registros_rows(rows):
    for r in rows:
        ini, fin = local(r.inicio), local(r.fin)
        yield [r.id, r.fecha.isoformat() if r.fecha else "", r.coordinador, r.cedula, r.operario, r.supernumerario,
               r.cliente, r.proceso, r.std_uh, "", "", r.pedido, r.ean, _n(r.total_envio),
               ini.strftime("%H:%M") if ini else "", fin.strftime("%H:%M") if fin else "", round(r.minutos_paro),
               r.observacion, round(r.minutos_brutos) if r.minutos_brutos is not None else None, r.clasificacion,
               r.horas, _n(r.unidades), r.uh_real, r.eficiencia, r.id, r.ultima_accion,
               _n(r.sobrantes), _n(r.merma), r.linea, r.etapa, r.op_cliente, r.ean_desc, r.registrado_por, r.estado]


def orders_in_range(d_from, d_to):
    """Pedidos creados en el rango o con reportes en el rango."""
    a, b = records.day_bounds(d_from, d_to)
    with_reports = ProductionReport.objects.filter(reported_at__gte=a, reported_at__lt=b).values("operation__order_id")
    return (WorkOrder.objects.filter(Q(created_at__gte=a, created_at__lt=b) | Q(pk__in=with_reports))
            .select_related("customer", "product").prefetch_related("operations").order_by("pk"))


def seguimiento_rows(tracking):
    for t in tracking:
        o = t["order"]
        just = ""
        if t["closure"]:
            c = t["closure"]
            just = f"{c.justified_by} - {c.reason}"
        yield [local(o.created_at).date().isoformat(), o.number, o.customer.name if o.customer_id else "",
               _n(t["processed"]), _n(t["pending"]) if t["state"] in ("No cumple", "En proceso", "Pendiente") else None,
               t["eff"], " - ".join(t["supers"]), " - ".join(t["coords"]), t["state"], just,
               f"{o.product.sku} · {o.product.name}", _n(o.quantity), t["hours"], t["people"],
               o.get_status_display(), o.external_op or o.purchase_order]


def ean_rows(orders, rows_by_order):
    for o in orders:
        m = records.ean_matrix(o)
        if not m["rows"]:
            continue
        people = defaultdict(set)
        for r in rows_by_order.get(o.pk, []):
            if r.ean and r.employee_id:
                people[r.ean].add(r.operario)
        stage = m["stages"][-1] if m["stages"] else ""
        for row in m["rows"]:
            f = row["final"] or {}
            yield [o.number, "", row["ean"], row["qty"], _n(f.get("good")), " - ".join(sorted(people[row["ean"]])),
                   f.get("status", ""), _n(f.get("left")), row["desc"], _n(f.get("just")), stage,
                   o.customer.name if o.customer_id else ""]


def horas_rows(hours):
    for h in hours:
        yield [h["fecha"].isoformat(), h["operario"], h["registradas"], h["esperadas"], h["diferencia"], h["estado"],
               h["supernumerario"], h["cedula"], _n(h["unidades"]), h["eficiencia"]]


def workbook(d_from, d_to, sheets=("registros", "seguimiento", "eanes", "horas"), include_voided=False) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    f = Filters(d_from=d_from, d_to=d_to, estado="todos" if include_voided else "activos")
    rows, _ = records.search(f, limit=None)
    active = [r for r in rows if r.estado == "Activo"]
    rows_by_order = defaultdict(list)
    for r in active:
        rows_by_order[r.order_id].append(r)
    orders = list(orders_in_range(d_from, d_to)) if {"seguimiento", "eanes"} & set(sheets) else []
    if orders:
        # para seguimiento y EAN se usan todos los reportes de esos pedidos (no solo los del rango)
        all_rows = records.build_rows(ProductionReport.objects.filter(operation__order__in=orders)
                                      .select_related(*records.REPORT_RELATED))
        rows_by_order = defaultdict(list)
        for r in all_rows:
            rows_by_order[r.order_id].append(r)
    wb = Workbook()
    wb.remove(wb.active)
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="2F3E8F")
    plan = [("registros", REG_COLS, lambda: registros_rows(sorted(rows, key=lambda r: (r.fin or r.inicio or timezone.now())))),
            ("seguimiento", SEG_COLS, lambda: seguimiento_rows(records.order_tracking(orders, rows_by_order))),
            ("eanes", EAN_COLS, lambda: ean_rows(orders, rows_by_order)),
            ("horas", HOR_COLS, lambda: horas_rows(records.hours_control(active)))]
    for key, cols, gen in plan:
        if key not in sheets:
            continue
        ws = wb.create_sheet(SHEETS[key])
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
        for line in gen():
            ws.append(line)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, name in enumerate(cols, start=1):
            ws.column_dimensions[get_column_letter(i)].width = max(10, min(40, len(name) + 4))
    if not wb.worksheets:
        wb.create_sheet("Sin datos")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
