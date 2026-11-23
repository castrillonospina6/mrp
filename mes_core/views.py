
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import corrections, planning, services, site_scope
from .forms import OrderEditForm, PlanForm, ProductionReportForm
from .models import ProductionReport, Site, WorkOrder
from .plugins import registry


def _error_text(exc: ValidationError) -> str:
    return "; ".join(exc.messages)


def _approval_required(order, user) -> bool:
    """La planta exige plan aprobado y el usuario no puede aprobar → no puede liberar directamente."""
    from .models import PlanningRules
    rules = PlanningRules.objects.filter(site_id=order.site_id).first()
    return bool(rules and rules.require_approval and not user.has_perm("mes_core.approve_plan"))


@login_required
def order_list(request):
    base = WorkOrder.objects.select_related("site", "product", "customer")
    status = request.GET.get("status", "")
    scope = site_scope.pick(request, allow_all=True)
    base = base.filter(scope.q())
    q = (request.GET.get("q") or "").strip()
    if q:
        base = base.filter(
            Q(number__icontains=q) | Q(product__sku__icontains=q) | Q(product__name__icontains=q)
            | Q(customer__name__icontains=q) | Q(reference__icontains=q) | Q(purchase_order__icontains=q)
            | Q(external_op__icontains=q) | Q(shipment__icontains=q))
    counts = dict(base.values_list("status").annotate(n=Count("id")))
    orders = base.filter(status=status) if status else base
    return render(request, "mes_core/order_list.html", {
        "orders": orders[:200],
        "total": sum(counts.values()),
        "status_tabs": [(value, label, counts.get(value, 0)) for value, label in WorkOrder.Status.choices],
        "sites": scope.sites, "site": scope.site, "site_allow_all": True,
        "current_status": status,
        "current_site": scope.param,
        "q": q,
    })


def _risks(order):
    from .risk import order_risks

    try:
        return order_risks(order)
    except Exception:  # noqa: BLE001 - la alerta es informativa; nunca debe impedir ver el pedido
        import logging

        logging.getLogger(__name__).exception("No se pudo calcular el riesgo de %s", order.pk)
        return []


def _stage_summary(order, operations) -> list[dict]:
    """Sumatoria de lo reportado por etapa del proceso (todas sus partes / lotes juntos), para no sumar a mano."""
    from decimal import Decimal

    from django.db.models import Max, Sum

    sums = {r["operation_id"]: r for r in ProductionReport.objects.filter(operation__order=order)
            .values("operation_id").annotate(good=Sum("qty_good"), extra=Sum("qty_extra"), scrap=Sum("qty_scrap"),
                                             n=Count("id"), last=Max("reported_at"))}
    zero = Decimal("0")
    groups: dict = {}
    for op in sorted(operations, key=lambda o: o.sequence):
        key = ("s", op.stage_id) if op.stage_id else ("o", op.pk)
        g = groups.setdefault(key, {"name": op.stage.name if op.stage_id else op.name,
                                    "line": op.work_center.code if op.work_center_id else "", "planned": zero,
                                    "good": zero, "extra": zero, "scrap": zero, "reports": 0, "parts": 0,
                                    "done_parts": 0, "last": None})
        s = sums.get(op.pk, {})
        g["planned"] += op.qty
        g["good"] += s.get("good") or zero
        g["extra"] += s.get("extra") or zero
        g["scrap"] += s.get("scrap") or zero
        g["reports"] += s.get("n") or 0
        g["parts"] += 1
        g["done_parts"] += op.status == op.Status.DONE
        if s.get("last") and (g["last"] is None or s["last"] > g["last"]):
            g["last"] = s["last"]
    out = list(groups.values())
    for g in out:
        g["pending"] = max(zero, g["planned"] - g["good"])
        g["pct"] = min(100, int(g["good"] * 100 / g["planned"])) if g["planned"] else 0
        g["done"] = g["parts"] and g["done_parts"] == g["parts"]
    return out


@login_required
def order_detail(request, pk):
    order = get_object_or_404(WorkOrder.objects.select_related("site", "product"), pk=pk)
    can_report_perm = request.user.has_perm(REPORT_PERM)
    form = ProductionReportForm(request.POST or None, order=order)
    if request.method == "POST" and not can_report_perm:
        messages.error(request, "Solo supervisores, jefe de planta y planeación reportan producción.")
        return redirect(order)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            report = services.report_production(
                data["operation"], qty_good=data["qty_good"], qty_scrap=data["qty_scrap"],
                finished=data["finished"], comment=data["comment"], user=request.user,
                allow_extra=data["allow_extra"], extra_reason=data["extra_reason"],
                ean=data.get("ean") or "", qty_leftover=data.get("qty_leftover") or 0,
            )
        except ValidationError as exc:
            form.add_error(None, _error_text(exc))  # se queda en la página con lo escrito
        else:
            msg = "Producción reportada."
            if report.qty_extra:
                msg += (f" {services._fmt_qty(report.qty_good)} cuentan para lo programado y "
                        f"{services._fmt_qty(report.qty_extra)} quedaron registradas "
                        f"como unidades de más ({report.extra_reason}).")
            if report.finished and not data["finished"]:  # llegó al 100 %: se cerró sola
                op = report.operation
                msg += f" «{op.name}» completó lo programado y quedó terminada."
                order.refresh_from_db(fields=["status"])
                if order.status == WorkOrder.Status.DONE:
                    msg += f" Con eso el pedido {order.number} quedó terminado."
            messages.success(request, msg)
            return redirect(order)
    operations = list(order.operations.select_related("work_center", "supervisor", "stage"))
    last = operations[-1] if operations else None
    user = request.user
    planned = order.status in (WorkOrder.Status.DRAFT, WorkOrder.Status.PLANNED)
    from .supplies import completeness

    supplies_pct, supplies_missing = completeness(order)
    return render(request, "mes_core/order_detail.html", {
        "order": order,
        "operations": operations,
        "supply_lines": order.supply_lines.select_related("stock__product", "operation", "deliver_to"),
        "risks": _risks(order),
        "supplies_pct": supplies_pct, "supplies_missing": supplies_missing,
        "can_send_supplies": planned and bool(operations) and user.has_perm(PLAN_PERM)
                             and not _approval_required(order, user),
        "can_release_supplies": order.status == WorkOrder.Status.SUPPLIES and user.has_perm(RELEASE_PERM),
        "can_view_supplies": user.has_perm(RELEASE_PERM) or user.has_perm("mes_core.view_supplyrequirement"),
        "has_extra": any(op.qty_extra for op in operations),
        "order_extra": last.qty_extra if last else 0,
        "pending_json": form.pending,
        "reports": ProductionReport.objects.filter(operation__order=order)
                   .select_related("operation", "reported_by", "employee", "edited_by")
                   .annotate(good_total=F("qty_good") + F("qty_extra")),
        "voided_count": order.voided_reports.count(),
        "compliance": corrections.compliance(order),
        "closure": getattr(order, "closure", None) if corrections.compliance(order) == "No cumple" else None,
        "refs": {str(k): v.get("ref") for k, v in registry.report_info(
            [op for op in operations if op.status != op.Status.DONE]).items()} if can_report_perm else {},
        "report_form": form,
        "stage_summary": _stage_summary(order, operations),
        "plugin_actions": registry.order_actions(order),
        "needs_approval": planned and user.has_perm(PLAN_PERM) and _approval_required(order, user),
        "can_cancel": order.status not in (WorkOrder.Status.DONE, WorkOrder.Status.CANCELLED)
                      and user.has_perm(CANCEL_PERM),
        "can_report": order.status in (WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS) and can_report_perm,
        "can_fix": can_fix(user),
    })


def can_fix(user) -> bool:
    """Editar el pedido y corregir lo reportado: planeación, jefe de planta y administradores."""
    return user.has_perm(PLAN_PERM) or user.has_perm(CANCEL_PERM)


@login_required
def order_edit(request, pk):
    order = get_object_or_404(WorkOrder.objects.select_related("site", "product", "customer"), pk=pk)
    if not can_fix(request.user):
        messages.error(request, "Editar pedidos le corresponde a planeación o al jefe de planta.")
        return redirect(order)
    form = OrderEditForm(request.POST or None, instance=order)
    if request.method == "POST" and form.is_valid():
        changed = [form.fields[f].label for f in form.changed_data]
        order = form.save()
        if changed:
            from .models import ScheduleChange

            ScheduleChange.objects.create(site=order.site, action=ScheduleChange.Action.EDIT, user=request.user,
                                          description=f"{order.number}: editó {', '.join(changed)}"[:255])
            messages.success(request, f"Pedido {order.number} actualizado ({', '.join(changed)}).")
        return redirect(order)
    return render(request, "mes_core/order_edit.html", {"order": order, "form": form})


@login_required
@require_POST
def report_delete(request, pk):
    """Anular (con motivo): el reporte sale de las sumas y queda en «Anulados», de donde se puede reactivar. Eliminar
    del todo solo se puede con lo ya anulado (Registros de producción)."""
    report = get_object_or_404(ProductionReport.objects.select_related("operation__order"), pk=pk)
    order = report.operation.order
    if not can_fix(request.user):
        messages.error(request, "Corregir lo reportado le corresponde a planeación o al jefe de planta.")
        return redirect(order)
    try:
        corrections.void_report(report, request.POST.get("reason", ""), user=request.user)
        messages.success(request, "Reporte anulado: salió de las sumas (la operación y el pedido se recalcularon). "
                                  "Se puede reactivar desde «Anulados».")
    except ValidationError as exc:
        messages.error(request, _error_text(exc))
    return redirect(f"{order.get_absolute_url()}#reportes")


@login_required
@require_POST
def report_delete_all(request, pk):
    order = get_object_or_404(WorkOrder, pk=pk)
    if not can_fix(request.user):
        messages.error(request, "Corregir lo reportado le corresponde a planeación o al jefe de planta.")
        return redirect(order)
    try:
        n = corrections.void_all_reports(order, request.POST.get("reason", ""), user=request.user)
        messages.success(request, f"Se anularon {n} reporte(s): el pedido quedó sin unidades procesadas. "
                                  "Se pueden reactivar desde «Anulados».")
    except ValidationError as exc:
        messages.error(request, _error_text(exc))
    return redirect(f"{order.get_absolute_url()}#reportes")


@login_required
@require_POST
def report_edit(request, pk):
    report = get_object_or_404(ProductionReport.objects.select_related("operation__order"), pk=pk)
    order = report.operation.order
    if not can_fix(request.user):
        messages.error(request, "Corregir lo reportado le corresponde a planeación o al jefe de planta.")
        return redirect(order)
    try:
        p = request.POST
        kw = {"qty_good": p.get("qty_good") or 0, "qty_scrap": p.get("qty_scrap") or 0,
              "finished": p.get("finished") == "on", "comment": p.get("comment", "")}
        if "qty_leftover" in p:
            kw["qty_leftover"] = p.get("qty_leftover") or 0
        if "ean" in p:
            kw["ean"] = p.get("ean", "")
        corrections.edit_report(report, reason=p.get("reason", ""), user=request.user, **kw)
        messages.success(request, "Reporte corregido: la operación y el pedido se recalcularon.")
    except ValidationError as exc:
        messages.error(request, _error_text(exc))
    return redirect(f"{order.get_absolute_url()}#reportes")


CANCEL_PERM = "mes_core.change_workorder"   # Planeación, Jefe de planta, administrador
PLAN_PERM = "mes_core.plan_workorder"       # planeación envía a insumos
RELEASE_PERM = "mes_core.release_to_plant"  # el equipo de insumos libera a planta
REPORT_PERM = "mes_core.add_productionreport"  # supervisores (ejecución), jefe de planta y planeación


@login_required
def order_release(request, pk):
    """La liberación a planta la hace el equipo de insumos desde la pantalla de insumos del pedido."""
    order = get_object_or_404(WorkOrder, pk=pk)
    if not request.user.has_perm(RELEASE_PERM):
        messages.error(request, "Liberar a planta le corresponde al equipo de insumos.")
        return redirect(order)
    return redirect("mes_core:supply_order", pk=order.pk)


@login_required
@require_POST
def order_cancel(request, pk):
    order = get_object_or_404(WorkOrder, pk=pk)
    if not request.user.has_perm(CANCEL_PERM):
        messages.error(request, "No tienes permiso para cancelar pedidos.")
        return redirect(order)
    try:
        services.cancel_order(order)
        messages.success(request, f"Orden {order.number} cancelada.")
    except ValidationError as exc:
        messages.error(request, _error_text(exc))
    return redirect(order)


@login_required
def plan(request):
    """Diagrama de Gantt interactivo (los datos vienen de la API del programador)."""
    site, sites = site_scope.site_and_sites(request)
    if request.method == "POST":
        if not request.user.has_perm("mes_core.plan_workorder"):
            messages.error(request, "No tienes permiso para generar el plan.")
            return redirect("mes_core:plan")
        form = PlanForm(request.POST)
        if form.is_valid():
            target = form.cleaned_data["site"]
            result = planning.generate_plan(target, start=form.cleaned_data["start"], user=request.user)
            msg = f"Plan generado: {len(result.orders)} órdenes."
            if result.late_orders:
                msg += f" {len(result.late_orders)} quedan después de su fecha compromiso: " + ", ".join(
                    o.number for o in result.late_orders)
            messages.info(request, msg)
            return redirect(f"{request.path}?site={target.pk}")
    return render(request, "mes_core/plan.html", {
        "sites": sites,
        "site": site,
        "can_plan": request.user.has_perm("mes_core.plan_workorder"),
    })


@login_required
def plugin_list(request):
    return render(request, "mes_core/plugins.html", {
        "plugins": registry.plugins(),
        "process_types": registry.process_types(),
    })
