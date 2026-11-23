"""Tablero principal: capacidad de planta e indicadores para planeación."""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from . import site_scope
from .capacity import all_sites_data, dashboard_data, fmt_h
from .workcal import get_calendar
from .models import ScheduleChange, Shift, ShiftAttendance, WorkOrder

PLAN_PERM = "mes_core.plan_workorder"


def _back(site_id, day=None, shift_id=None, anchor=""):
    params = {"site": site_id}
    if day:
        params["day"] = day.isoformat()
    if shift_id:
        params["shift"] = shift_id
    return redirect(f"{reverse('mes_core:dashboard')}?{urlencode(params)}{anchor}")


def _int(value, default=0, lo=0, hi=None):
    try:
        n = int(str(value).strip() or default)
    except (TypeError, ValueError):
        return None
    if n < lo or (hi is not None and n > hi):
        return None
    return n


@login_required
def dashboard(request):
    from .context_processors import menu_flags

    user = request.user
    if not menu_flags(user)["capacity"]:  # cada rol entra directo a su pantalla
        for perm, home in (("mes_core.supervise_crew", "mes_core:supervision_home"),
                           ("mes_core.release_to_plant", "mes_core:supplies_queue"),
                           ("mes_core.mark_goods", "mes_core:goods_queue"), ("eanes.manage_eans", "eanes:queue"),
                           ("mes_core.view_workorder", "mes_core:order_list")):
            if user.has_perm(perm):
                try:
                    return redirect(home)
                except NoReverseMatch:  # módulo desinstalado
                    continue
    scope = site_scope.pick(request, allow_all=True)
    site, sites = scope.site, scope.sites
    if not sites:
        return render(request, "mes_core/dashboard.html", {"no_site": True})
    day = parse_date(request.GET.get("day") or "")
    if scope.all:  # toda la capacidad instalada: todas las bodegas, todas las líneas y todo el personal
        from django.utils import timezone

        from . import workforce

        data = all_sites_data(sites, day=day)
        return render(request, "mes_core/dashboard.html", {
            **data, "all_sites": True, "site": None, "sites": sites,
            "site_allow_all": True, "can_plan": request.user.has_perm(PLAN_PERM),
            # indicadores de la empresa: bolsa global de personal, capacidad en horas y desempeño
            "company": workforce.company_kpis(day or timezone.localdate(), data),
            "today_date": timezone.localdate(),
        })
    shift = None
    if request.GET.get("shift"):
        shift = Shift.objects.filter(site=site, active=True, pk=request.GET["shift"]).first()
    data = dashboard_data(site, day=day, shift=shift)
    sel = data.get("sel")
    return render(request, "mes_core/dashboard.html", {
        **data,
        "sites": sites,
        "site_allow_all": True,
        "can_plan": request.user.has_perm(PLAN_PERM),
        "balance_txt": fmt_h(abs(sel.balance_h)) if sel else "",
        "bar_pct": min(100, sel.occ_pct) if sel and sel.occ_pct is not None else 0,
    })


@login_required
@require_POST
def save_attendance(request):
    if not request.user.has_perm(PLAN_PERM):
        messages.error(request, "No tienes permiso para registrar ausencias.")
        return redirect("mes_core:dashboard")
    shift = get_object_or_404(Shift.objects.select_related("site"), pk=request.POST.get("shift"))
    day = parse_date(request.POST.get("day") or "")
    absent = _int(request.POST.get("absent"))
    scheduled_raw = (request.POST.get("scheduled") or "").strip()
    scheduled = _int(scheduled_raw) if scheduled_raw else None
    if day is None or absent is None or (scheduled_raw and scheduled is None):
        messages.error(request, "Revisa los datos: las ausencias y los programados deben ser números enteros ≥ 0.")
        return _back(shift.site_id, day, shift.pk)
    base = scheduled if scheduled is not None else (
        get_calendar(shift.site_id).people(shift, day) or shift.site.operators_total)
    if base and absent > base:
        messages.error(request, f"Las ausencias ({absent}) no pueden superar el personal programado ({base}).")
        return _back(shift.site_id, day, shift.pk)
    with transaction.atomic():
        ShiftAttendance.objects.update_or_create(shift=shift, day=day, defaults={
            "absent": absent, "scheduled": scheduled, "notes": (request.POST.get("notes") or "")[:255],
            "updated_by": request.user})
        ScheduleChange.objects.create(
            site=shift.site, action=ScheduleChange.Action.ATTENDANCE, user=request.user,
            description=f"{shift.name} {day:%d/%m}: {absent} ausencias" + (f" de {scheduled} programados" if scheduled is not None else ""))
    messages.success(request, f"Personal de {shift.name} {day:%d/%m} actualizado.")
    return _back(shift.site_id, day, shift.pk)


@login_required
@require_POST
def save_resources(request, pk):
    order = get_object_or_404(WorkOrder.objects.select_related("site"), pk=pk)
    day = parse_date(request.POST.get("day") or "")
    shift_id = request.POST.get("shift")
    if not request.user.has_perm(PLAN_PERM):
        messages.error(request, "No tienes permiso para actualizar recursos.")
        return _back(order.site_id, day, shift_id, "#recursos")
    goods = _int(request.POST.get("goods_pct"), hi=100)
    supplies = _int(request.POST.get("supplies_pct"), hi=100)
    if request.POST.get("all") == "1":
        goods = supplies = 100
    if not request.user.has_perm("mes_core.release_to_plant"):
        supplies = order.supplies_pct  # el % de insumos lo maneja el equipo de insumos con sus entregas
    if goods is None or supplies is None:
        messages.error(request, "Los porcentajes deben ser números entre 0 y 100.")
        return _back(order.site_id, day, shift_id, "#recursos")
    with transaction.atomic():
        order.goods_pct, order.supplies_pct = goods, supplies
        order.save(update_fields=["goods_pct", "supplies_pct"])
        ScheduleChange.objects.create(site=order.site, action=ScheduleChange.Action.RESOURCES, user=request.user,
                                      description=f"{order.number}: mercancía {goods} %, insumos {supplies} %")
    messages.success(request, f"{order.number}: mercancía {goods} %, insumos {supplies} %.")
    return _back(order.site_id, day, shift_id, "#recursos")
