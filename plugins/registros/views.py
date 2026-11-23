"""Registros de producción (buscar, editar con motivo, anular, reactivar, eliminar anulados), seguimiento de pedidos
(Cumple / No cumple, por referencia) y exportación a Excel con el formato del libro anterior."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.views.decorators.http import require_POST

from mes_core import corrections
from mes_core.models import Employee, Operation, ProductionReport, VoidedReport, WorkCenter, WorkOrder

from . import export, records
from .records import Filters

VIEW_PERMS = ("mes_core.view_productionreport", "mes_core.plan_workorder", "mes_core.approve_plan")
FIX_PERMS = ("mes_core.plan_workorder", "mes_core.change_workorder", "mes_core.supervise_crew")
PURGE_PERMS = ("mes_core.plan_workorder", "mes_core.change_workorder")
STATES = ("Pendiente", "En proceso", "Cumple", "No cumple", "Cancelado")


def can_view(u) -> bool:
    return any(u.has_perm(p) for p in VIEW_PERMS)


def can_fix(u) -> bool:
    return any(u.has_perm(p) for p in FIX_PERMS)


def can_purge(u) -> bool:
    return any(u.has_perm(p) for p in PURGE_PERMS)


def _need(cond):
    if not cond:
        raise PermissionDenied


def _day(text, default):
    d = parse_date(text or "")
    return d or default


def _filters(get) -> Filters:
    today = timezone.localdate()
    d_from = _day(get.get("desde"), today - timedelta(days=6))
    d_to = _day(get.get("hasta"), today)
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    estado = get.get("estado") if get.get("estado") in ("activos", "anulados", "todos") else "activos"
    return Filters(d_from=d_from, d_to=d_to, pedido=get.get("pedido", "").strip(),
                   operario=get.get("operario", "").strip(), coordinador=get.get("coord", "").strip(),
                   supernumerario=get.get("sup", "").strip(), proceso=get.get("proceso", "").strip(),
                   ean=get.get("ean", "").strip(), linea=get.get("linea", "").strip(),
                   cliente=get.get("cliente", "").strip(), estado=estado)


def _choices():
    User = get_user_model()
    coords = User.objects.filter(pk__in=Operation.objects.exclude(supervisor=None).values("supervisor")).order_by(
        "first_name", "username")
    try:
        from plugins.piso.models import FloorAccess

        sups = Employee.objects.filter(floor_access__role=FloorAccess.Role.SUPERNUMERARY).order_by("full_name")
    except Exception:  # noqa: BLE001
        sups = Employee.objects.none()
    return {"coords": coords, "sups": sups, "lines": WorkCenter.objects.filter(active=True).order_by("site__code", "code")}


def _back(request, default):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else default


# ---------------------------------------------------------------- registros
@login_required
def records_view(request):
    _need(can_view(request.user))
    f = _filters(request.GET)
    rows, cut = records.search(f, limit=500)
    active = [r for r in rows if r.estado == "Activo"]
    mins = sum(r.minutos or 0 for r in active)
    std = sum(r.std_min for r in active if r.minutos)
    totals = {"rows": len(rows), "units": sum(r.unidades for r in active), "scrap": sum(r.merma for r in active),
              "left": sum(r.sobrantes for r in active), "hours": round(mins / 60, 1),
              "eff": round(std / mins * 100, 1) if mins >= 1 and std else None,
              "voided": sum(1 for r in rows if r.estado == "Anulado")}
    return render(request, "registros/records.html", {
        "f": f, "rows": rows, "cut": cut, "totals": totals, "can_fix": can_fix(request.user),
        "can_purge": can_purge(request.user), "qs": request.GET.urlencode(), **_choices(),
    })


def _report_or_404(pk):
    return get_object_or_404(ProductionReport.objects.select_related(*records.REPORT_RELATED), pk=pk)


@login_required
def report_edit(request, pk):
    _need(can_fix(request.user))
    report = _report_or_404(pk)
    op, order = report.operation, report.operation.order
    back = _back(request, f"{reverse('registros:records')}?{urlencode({'pedido': order.number, 'desde': '2000-01-01'})}")
    people = sorted({e for e in [report.employee] if e} | {ca.employee for ca in op.crew_assignments.select_related("employee")},
                    key=lambda e: e.full_name)
    multi = len({r.employee_id for r in records.build_rows([report])}) > 1
    eans = _ean_choices(order)
    if request.method == "POST":
        p = request.POST
        try:
            target = Operation.objects.filter(pk=p.get("operation") or op.pk).first() or op
            when = report.reported_at
            d, t = parse_date(p.get("fecha", "")), parse_time(p.get("hora", ""))
            if d and t:
                when = timezone.make_aware(datetime.combine(d, t))
                if abs((when - report.reported_at).total_seconds()) < 60:
                    when = report.reported_at  # mismo minuto: no se mueve
            kw = dict(qty_good=p.get("qty_good"), qty_scrap=p.get("qty_scrap"), qty_leftover=p.get("qty_leftover"),
                      ean=p.get("ean", ""), operation=target, reported_at=when, finished=p.get("finished") == "on",
                      comment=p.get("comment", ""))
            if not multi:
                doc = (p.get("doc") or "").strip()
                if doc:
                    emp = Employee.objects.filter(document=doc).first()
                    if emp is None:
                        raise ValidationError(f"No hay ningún operario con documento {doc}.")
                    kw["employee"] = emp
                elif p.get("employee") is not None:
                    kw["employee"] = Employee.objects.filter(pk=p.get("employee") or 0).first()
            corrections.edit_report(report, reason=p.get("reason", ""), user=request.user, **kw)
            messages.success(request, f"Reporte #{report.pk} corregido: se recalcularon la operación, el pedido y la "
                                      "referencia.")
            return redirect(back)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    return render(request, "registros/edit.html", {
        "r": report, "op": op, "order": order, "people": people, "multi": multi, "eans": eans,
        "ops": records.operations_for_edit(op), "back": back, "when": timezone.localtime(report.reported_at),
        "good_total": report.qty_good + report.qty_extra,
    })


def _ean_choices(order):
    try:
        from plugins.eanes.eans import MODE_ON, mode_of
        from plugins.eanes.models import EanLine
    except Exception:  # noqa: BLE001
        return None
    if mode_of(order) != MODE_ON:
        return None
    return list(EanLine.objects.filter(order=order))


@login_required
@require_POST
def report_void(request, pk):
    _need(can_fix(request.user))
    report = _report_or_404(pk)
    back = _back(request, reverse("registros:records"))
    try:
        v = corrections.void_report(report, request.POST.get("reason", ""), user=request.user)
        messages.success(request, f"Reporte #{v.original_id} anulado: salió de las sumas. Está en «Anulados» por si hay "
                                  "que reactivarlo.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(back)


@login_required
@require_POST
def voided_reactivate(request, pk):
    _need(can_fix(request.user))
    v = get_object_or_404(VoidedReport, pk=pk)
    back = _back(request, reverse("registros:records") + "?estado=anulados")
    try:
        r = corrections.reactivate_report(v, request.POST.get("reason", ""), user=request.user)
        messages.success(request, f"Reporte reactivado (ahora #{r.pk}): volvió a las sumas.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(back)


@login_required
@require_POST
def voided_purge(request, pk):
    _need(can_purge(request.user))
    v = get_object_or_404(VoidedReport, pk=pk)
    corrections.purge_voided(v, user=request.user)
    messages.success(request, f"Reporte anulado #{v.original_id} eliminado definitivamente.")
    return redirect(_back(request, reverse("registros:records") + "?estado=anulados"))


# ---------------------------------------------------------------- seguimiento de pedidos
@login_required
def tracking_view(request):
    _need(can_view(request.user))
    today = timezone.localdate()
    d_from = _day(request.GET.get("desde"), today - timedelta(days=30))
    d_to = _day(request.GET.get("hasta"), today)
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    q = request.GET.get("q", "").strip()
    state = request.GET.get("estado", "")
    orders = export.orders_in_range(d_from, d_to).select_related("closure")
    if q:
        orders = orders.filter(Q(number__icontains=q) | Q(customer__name__icontains=q) | Q(product__name__icontains=q)
                               | Q(product__sku__icontains=q) | Q(external_op__icontains=q)
                               | Q(purchase_order__icontains=q))
    data = records.order_tracking(orders[:600])
    counts = {s: sum(1 for t in data if t["state"] == s) for s in STATES}
    if state in STATES:
        data = [t for t in data if t["state"] == state]
    mins = sum(t["hours"] * 60 for t in data)
    effs = [t for t in data if t["eff"] is not None]
    eff_avg = round(sum(t["eff"] * t["hours"] for t in effs) / sum(t["hours"] for t in effs), 1) if effs and sum(
        t["hours"] for t in effs) else None
    data.sort(key=lambda t: (t["state"] not in ("En proceso",), -(t["order"].pk)))
    top = sorted(effs, key=lambda t: -t["hours"])[:15]
    return render(request, "registros/tracking.html", {
        "d_from": d_from, "d_to": d_to, "q": q, "state": state, "data": data, "counts": counts, "states": STATES,
        "total": sum(counts.values()), "eff_avg": eff_avg, "hours": round(mins / 60, 1), "top": top,
        "max_count": max(counts.values()) if counts else 0,
    })


@login_required
def tracking_detail(request, pk):
    _need(can_view(request.user))
    order = get_object_or_404(WorkOrder.objects.select_related("customer", "product", "site"), pk=pk)
    t = records.order_tracking([order])[0]
    rows = records.build_rows(ProductionReport.objects.filter(operation__order=order)
                              .select_related(*records.REPORT_RELATED).order_by("-reported_at")[:400])
    preview = corrections.shortfall_preview(order) if order.status in (
        WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS) else []
    return render(request, "registros/tracking_detail.html", {
        "o": order, "t": t, "m": records.ean_matrix(order), "rows": rows,
        "ops": order.operations.select_related("work_center", "stage").order_by("sequence"),
        "voided": order.voided_reports.select_related("voided_by")[:100], "preview": preview,
        "preview_total": sum((p["pending"] for p in preview), 0), "can_fix": can_fix(request.user),
        "can_purge": can_purge(request.user),
        "me": request.user.get_full_name() or request.user.get_username(),
    })


@login_required
@require_POST
def close_short(request, pk):
    _need(can_fix(request.user))
    order = get_object_or_404(WorkOrder, pk=pk)
    try:
        c = corrections.close_with_shortfall(order, reason=request.POST.get("reason", ""),
                                             justified_by=request.POST.get("justified_by", ""), user=request.user,
                                             expected_total=request.POST.get("expected") or None)
        messages.success(request, f"{order.number} cerrado con faltante de {c.total:g} unidades: queda «No cumple».")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(_back(request, reverse("registros:tracking_detail", args=[pk])))


@login_required
@require_POST
def undo_close(request, pk):
    _need(can_fix(request.user))
    order = get_object_or_404(WorkOrder, pk=pk)
    try:
        corrections.undo_closure(order, request.POST.get("reason", ""), user=request.user)
        messages.success(request, f"{order.number}: se deshizo el cierre con faltante; las etapas quedaron abiertas.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(_back(request, reverse("registros:tracking_detail", args=[pk])))


# ---------------------------------------------------------------- exportar
@login_required
def export_view(request):
    _need(can_view(request.user))
    today = timezone.localdate()
    d_from = _day(request.GET.get("desde"), today.replace(day=1))
    d_to = _day(request.GET.get("hasta"), today)
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    if request.GET.get("descargar"):
        if (d_to - d_from).days > 400:
            messages.error(request, "El rango máximo es de 400 días: exporta por partes.")
        else:
            sheets = [s for s in request.GET.getlist("hoja") if s in export.SHEETS] or list(export.SHEETS)
            data = export.workbook(d_from, d_to, sheets=sheets, include_voided=request.GET.get("anulados") == "1")
            resp = HttpResponse(data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            resp["Content-Disposition"] = (f'attachment; filename="Control de la produccion '
                                           f'{d_from:%Y-%m-%d} a {d_to:%Y-%m-%d}.xlsx"')
            return resp
    presets = [("Hoy", today, today), ("Ayer", today - timedelta(days=1), today - timedelta(days=1)),
               ("Últimos 7 días", today - timedelta(days=6), today),
               ("Este mes", today.replace(day=1), today),
               ("Mes anterior", (today.replace(day=1) - timedelta(days=1)).replace(day=1),
                today.replace(day=1) - timedelta(days=1)),
               ("Este año", date(today.year, 1, 1), today)]
    return render(request, "registros/export.html", {
        "d_from": d_from, "d_to": d_to, "sheets": export.SHEETS, "presets": presets,
        "cols": {"registros": export.REG_COLS, "seguimiento": export.SEG_COLS, "eanes": export.EAN_COLS,
                 "horas": export.HOR_COLS},
    })
