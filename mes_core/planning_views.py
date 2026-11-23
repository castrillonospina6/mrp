"""Recepción de pedidos (ventas), configuración de turnos y planeación por etapas (planeación)."""

import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import corrections, site_scope, staffing, service_types, services, stage_planning
from django import forms

from .forms import OrderIntakeForm, ShiftConfigForm, parse_time_rows
from .models import PlanningRules, Shift, ScheduleChange, Customer, PlanStage, Site, WorkCenter, WorkOrder
from .scheduler_views import _dt, _json_api
from .scheduling import site_shifts
from .shift_config import OverlapSpec, apply_shift_config, build_shift_specs, current_config, save_overlap_shifts


# ---------------------------------------------------------------- ventas
@login_required
@permission_required("mes_core.add_workorder", raise_exception=True)
def order_intake(request):
    initial = {"received_on": timezone.localdate(), "site": site_scope.site_and_sites(request)[0]}
    form = OrderIntakeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            with transaction.atomic():  # si el pedido no es válido, no se crean ni el producto ni el cliente
                customer = d["customer"]
                if d["new_customer"]:
                    customer, _ = Customer.objects.get_or_create(name=d["new_customer"].strip())
                product, product_created = services.resolve_product(d["product"])
                service, service_created = service_types.resolve(d["process_type"])
                order = services.receive_order(
                    site=d["site"], product=product, process_type=service.key if service else "", quantity=d["quantity"],
                    received_on=d["received_on"], delivery_date=d["delivery_date"], transit_days=d["transit_days"],
                    customer=customer, reference=d["reference"], purchase_order=d["purchase_order"],
                    external_op=d["external_op"], shipment=d["shipment"], description=d["description"],
                    priority=d["priority"], notes=d["notes"], user=request.user,
                )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            msg = (f"Pedido {order.number} registrado. Queda «Por planear» para el equipo de planeación "
                   f"(despacho {order.dispatch_date:%d/%m/%Y}).")
            if product_created:
                msg += f" Se creó el producto nuevo {product.sku} («{product.name}», unidad UN; ajústalo en Administración si hace falta)."
            if service_created:
                msg += f" Se agregó el tipo de servicio «{service.name}» a la lista."
            elif service and not service.active:
                msg += f" Ojo: el tipo de servicio «{service.name}» está desactivado en Configuración."
            messages.success(request, msg)
            nxt = request.GET.get("next", "")
            if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
                return redirect(nxt)
            return redirect(order)
    def day_start(site):
        starts = [sh.start_time for sh in site_shifts(site) if not sh.crosses_midnight]
        return f"{min(starts):%H:%M}" if starts else "00:00"

    return render(request, "mes_core/order_intake.html", {
        "form": form,
        "site_day_start": {s.pk: day_start(s) for s in Site.objects.filter(active=True)},
    })


# ---------------------------------------------------------------- turnos
@login_required
@permission_required("mes_core.change_shift", raise_exception=True)
def shift_settings(request):
    site, sites = site_scope.site_and_sites(request)
    if site is None:
        messages.error(request, "Primero crea una planta en Administración.")
        return redirect("mes_core:dashboard")
    if request.method == "POST" and request.POST.get("action") == "weekdays":
        changed = []
        for sh in site.shifts.filter(active=True):
            days = "".join(str(i) for i in range(7) if request.POST.get(f"wd_{sh.pk}_{i}") == "on")
            if days != sh.weekdays:
                sh.weekdays = days
                sh.save(update_fields=["weekdays"])
                changed.append(f"{sh.name}: {sh.weekdays_label}")
        if changed:
            ScheduleChange.objects.create(site=site, action=ScheduleChange.Action.CONFIG,
                                          description=("Días laborables · " + "; ".join(changed))[:255], user=request.user)
            messages.success(request, "Días laborables actualizados: " + "; ".join(changed) + ".")
        else:
            messages.info(request, "Sin cambios en los días laborables.")
        return redirect(f"{reverse('mes_core:shift_settings')}?site={site.pk}")
    if request.method == "POST" and request.POST.get("action") == "checkin":
        # hora máxima para confirmar la llegada en la tablet (después queda «Llegó tarde»)
        from datetime import datetime as _dt

        changed, bad = [], []
        for sh in site.shifts.filter(active=True):
            raw = (request.POST.get(f"ci_{sh.pk}") or "").strip()
            try:
                value = _dt.strptime(raw[:5], "%H:%M").time() if raw else None
            except ValueError:
                bad.append(sh.name)
                continue
            if value != sh.checkin_deadline:
                sh.checkin_deadline = value
                sh.save(update_fields=["checkin_deadline"])
                changed.append(f"{sh.name}: {value:%H:%M}" if value else f"{sh.name}: sin límite")
        if bad:
            messages.error(request, "Hora inválida en: " + ", ".join(bad) + ". Usa HH:MM.")
        if changed:
            ScheduleChange.objects.create(site=site, action=ScheduleChange.Action.CONFIG,
                                          description=("Llegada a la terminal · " + "; ".join(changed))[:255],
                                          user=request.user)
            messages.success(request, "Horas de llegada actualizadas: " + "; ".join(changed) + ".")
        elif not bad:
            messages.info(request, "Sin cambios en las horas de llegada.")
        return redirect(f"{reverse('mes_core:shift_settings')}?site={site.pk}#llegada")
    overlap_error, overlap_rows = None, None
    if request.method == "POST" and request.POST.get("action") == "overlap":
        try:
            specs = _overlap_specs(request.POST)
            changes = save_overlap_shifts(site, specs, user=request.user)
        except (ValidationError, forms.ValidationError) as exc:
            overlap_error = "; ".join(exc.messages)
            overlap_rows = _overlap_rows_from_post(request.POST)
        else:
            if changes:
                messages.success(request, "Turnos de traslape actualizados: " + "; ".join(changes) + ".")
            else:
                messages.info(request, "Sin cambios en los turnos de traslape.")
            return redirect(f"{reverse('mes_core:shift_settings')}?site={site.pk}#traslape")
    cfg = current_config(site)
    initial = {
        "first_start": cfg["first_start"], "duration_hours": round(cfg["duration_minutes"] / 60, 2),
        "count": cfg["count"], "operators_total": site.operators_total,
        **{f"operators_{i}": n for i, n in enumerate(cfg["operators"], start=1)},
    }
    bound = request.method == "POST" and request.POST.get("action") not in ("overlap", "weekdays", "checkin")
    form = ShiftConfigForm(request.POST if bound else None, initial=initial,
                           groups={"breakfast": cfg["breakfast"], "lunch": cfg["lunch"]})
    if bound and form.is_valid():
        d = form.cleaned_data
        try:
            specs = build_shift_specs(
                first_start=d["first_start"], duration_minutes=int(round(float(d["duration_hours"]) * 60)),
                count=d["count"], breakfast=d["breakfast_groups"], lunch=d["lunch_groups"],
                operators=form.operators_list(),
            )
            site.operators_total = d["operators_total"]
            site.save(update_fields=["operators_total"])
            apply_shift_config(site, specs, user=request.user)
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, f"Turnos de {site.code} actualizados: {len(specs)} turno(s).")
            return redirect(f"{reverse('mes_core:shift_settings')}?site={site.pk}")
    lines = WorkCenter.objects.filter(site=site, active=True).prefetch_related("shifts")
    wd_rows = [{"shift": sh, "days": [(i, Shift.WEEKDAY_SHORT[i], Shift.WEEKDAY_NAMES[i], sh.works_on_weekday(i)) for i in range(7)]}
               for sh in site.shifts.filter(active=True).order_by("start_time")]
    if overlap_rows is None:
        overlap_rows = [_overlap_row(sh) for sh in site.shifts.filter(active=True, is_overlap=True).order_by("start_time")]
    return render(request, "mes_core/shift_settings.html", {
        "form": form, "site": site, "sites": sites, "shifts": cfg["shifts"], "lines": lines, "wd_rows": wd_rows,
        "ci_rows": list(site.shifts.filter(active=True).order_by("start_time")),
        "overlap_rows": overlap_rows, "overlap_error": overlap_error,
        "breakfast_rows": form.group_rows("breakfast"), "lunch_rows": form.group_rows("lunch"),
        "new_overlap": {"key": "__KEY__", "name": "Traslape", "start": "11:00", "end": "19:00", "ops": "",
                        "breakfast": [], "lunch": [], "delete": False},
        "base_shifts": [{"name": s.name, "start": f"{s.start_time:%H:%M}", "end": f"{s.end_time:%H:%M}",
                         "ops": s.operators or site.operators_total} for s in cfg["shifts"]],
    })


def _overlap_row(sh: Shift) -> dict:
    fmt = lambda groups: [(f"{a:%H:%M}", f"{b:%H:%M}") for a, b in groups]
    return {"key": str(sh.pk), "name": sh.name, "start": f"{sh.start_time:%H:%M}", "end": f"{sh.end_time:%H:%M}",
            "ops": sh.operators or "", "breakfast": fmt(sh.breakfast_groups), "lunch": fmt(sh.lunch_groups), "delete": False}


def _overlap_rows_from_post(post) -> list[dict]:
    rows = []
    for key in post.getlist("ov_key"):
        p = f"ov_{key}_"
        rows.append({"key": key, "name": post.get(p + "name", ""), "start": post.get(p + "start", ""),
                     "end": post.get(p + "end", ""), "ops": post.get(p + "ops", ""), "delete": post.get(p + "del") == "1",
                     "breakfast": list(zip(post.getlist(p + "breakfast_start"), post.getlist(p + "breakfast_end"))),
                     "lunch": list(zip(post.getlist(p + "lunch_start"), post.getlist(p + "lunch_end")))})
    return rows


def _overlap_specs(post) -> list[OverlapSpec]:
    from datetime import datetime

    def t(value, label, name):
        value = (value or "").strip()
        if not value:
            return None
        try:
            return datetime.strptime(value[:5], "%H:%M").time()
        except ValueError:
            raise ValidationError(f"Hora de {label} inválida en «{name}».") from None

    specs = []
    for key in post.getlist("ov_key"):
        p = f"ov_{key}_"
        name = post.get(p + "name", "").strip()
        ops_raw = (post.get(p + "ops") or "").strip()
        if ops_raw and not ops_raw.isdigit():
            raise ValidationError(f"Los operarios de «{name}» deben ser un número entero.")
        delete = post.get(p + "del") == "1"
        if delete and not key.isdigit():
            continue  # fila nueva que se quitó antes de guardar
        specs.append(OverlapSpec(
            pk=int(key) if key.isdigit() else None, name=name, delete=delete,
            start=None if delete else t(post.get(p + "start"), "inicio", name),
            end=None if delete else t(post.get(p + "end"), "fin", name),
            operators=int(ops_raw or 0),
            breakfast_groups=[] if delete else parse_time_rows(post, p + "breakfast", "desayuno"),
            lunch_groups=[] if delete else parse_time_rows(post, p + "lunch", "almuerzo"),
        ))
    return specs


# ---------------------------------------------------------------- planeación
@login_required
@permission_required("mes_core.plan_workorder", raise_exception=True)
def planning_list(request):
    tab = request.GET.get("tab", "pendientes")
    base = (WorkOrder.objects.select_related("site", "product", "customer")
            .annotate(n_stages=Count("plan_stages")))
    pending = base.filter(status=WorkOrder.Status.TO_PLAN)
    planned = base.filter(status__in=[WorkOrder.Status.DRAFT, WorkOrder.Status.PLANNED, WorkOrder.Status.SUPPLIES,
                                      WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS])
    q = request.GET.get("q", "").strip()
    orders = pending if tab == "pendientes" else planned
    if q:
        orders = orders.filter(Q(number__icontains=q) | Q(customer__name__icontains=q) | Q(product__sku__icontains=q)
                               | Q(product__name__icontains=q) | Q(reference__icontains=q)
                               | Q(purchase_order__icontains=q) | Q(external_op__icontains=q) | Q(shipment__icontains=q))
    return render(request, "mes_core/planning_list.html", {
        "orders": orders.order_by("due_date", "priority")[:300], "tab": tab, "q": q,
        "n_pending": pending.count(), "n_planned": planned.count(), "now": timezone.now(),
    })


@login_required
@permission_required("mes_core.plan_workorder", raise_exception=True)
def planning_detail(request, pk):
    order = get_object_or_404(WorkOrder.objects.select_related("site", "product", "customer"), pk=pk)
    lines = list(WorkCenter.objects.filter(site=order.site, active=True))
    rules = PlanningRules.objects.filter(site=order.site).first()
    now = timezone.localtime()
    can_replan = stage_planning.can_replan(order)
    first_op = order.operations.filter(planned_start__isnull=False).order_by("planned_start").first()
    default_start = timezone.localtime(first_op.planned_start) if first_op else (
        now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    if can_replan:  # lo pendiente se re-planea desde ahora
        default_start = now.replace(second=0, microsecond=0)
    config = {
        "order": {
            "id": order.pk, "number": order.number, "quantity": float(order.quantity), "unit": order.product.unit,
            "due": timezone.localtime(order.due_date).isoformat() if order.due_date else None,
        },
        "lines": [{"id": wc.pk, "label": f"{wc.code} · {wc.name}", "kind": wc.kind, "efficiency": wc.efficiency,
                   "measured": stage_planning.measured_efficiency(wc)} for wc in lines],
        "supervisors": [{"id": u.pk, "label": staffing.user_label(u)} for u in staffing.supervisors()],
        "can_assign": request.user.has_perm("mes_core.assign_supervisor"),
        "default_efficiency": rules.default_efficiency if rules else 100,
        "overtime_efficiency": rules.overtime_efficiency if rules else 90,
        "rules_url": f"{reverse('mes_core:planning_rules')}?site={order.site.pk}",
        "stages": stage_planning.initial_stages(order),
        "start": default_start.strftime("%Y-%m-%dT%H:%M"),
        "locked": stage_planning.is_locked(order),
        "can_replan": can_replan,
        "can_reopen": corrections.can_reopen(order),
        "cancelled": order.status == WorkOrder.Status.CANCELLED,
        "progress": corrections.progress_summary(order),
        "replan_info": stage_planning.replan_info(order) if can_replan else None,
        "links": PlanStage.Link.choices,
        "report_modes": PlanStage.ReportMode.choices, "split_modes": PlanStage.SplitMode.choices,
        "std_units": PlanStage.StdUnit.choices, "modes": PlanStage.Mode.choices, "qty_modes": PlanStage.QtyMode.choices,
        "urls": {
            "preview": reverse("mes_core:api_plan_preview", args=[order.pk]),
            "save": reverse("mes_core:api_plan_save", args=[order.pk]),
            "overtime": reverse("mes_core:api_plan_overtime", args=[order.pk]),
            "scheduler": reverse("mes_core:scheduler"),
        },
        "site": order.site.pk,
        "operators_total": order.site.operators_total,
        "shifts_url": f"{reverse('mes_core:shift_settings')}?site={order.site.pk}",
        "tz": timezone.get_current_timezone_name(),
    }
    return render(request, "mes_core/planning_detail.html", {
        "order": order, "config": config, "shifts": site_shifts(order.site),
    })


def _plan_request(request, pk):
    order = get_object_or_404(WorkOrder.objects.select_related("site", "product"), pk=pk)
    data = json.loads(request.body or b"{}")
    return (order, data.get("stages") or [], _dt(data.get("start")), bool(data.get("respect_operators", True)),
            bool(data.get("replan")), bool(data.get("reopen")))


@login_required
@permission_required("mes_core.plan_workorder", raise_exception=True)
@require_POST
@_json_api
def api_plan_preview(request, pk):
    order, stages, start, respect, replan, _reopen = _plan_request(request, pk)
    return JsonResponse(stage_planning.preview(order, stages, start, respect, replan))


@login_required
@permission_required("mes_core.plan_workorder", raise_exception=True)
@require_POST
@_json_api
def api_plan_overtime(request, pk):
    """Opciones de horas extra para cumplir el compromiso (cada una verificada recalculando el plan)."""
    order, stages, start, respect, replan, _reopen = _plan_request(request, pk)
    return JsonResponse(stage_planning.overtime_options(order, stages, start, respect, replan))


@login_required
@permission_required("mes_core.plan_workorder", raise_exception=True)
@require_POST
@_json_api
def api_plan_save(request, pk):
    order, stages, start, respect, replan, reopen = _plan_request(request, pk)
    order = stage_planning.save_plan(order, stages, start, user=request.user, respect_operators=respect, replan=replan,
                                     reopen=reopen and not replan)
    week = timezone.localtime(start).date().isoformat()
    return JsonResponse({
        "ok": True, "status": order.get_status_display(),
        "redirect": f"{reverse('mes_core:scheduler')}?site={order.site_id}&view=semana&date={week}",
    })
