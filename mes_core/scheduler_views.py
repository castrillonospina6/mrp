"""Programador visual: página + API JSON que usa el tablero (arrastrar y soltar)."""

import json
from datetime import datetime, time
from functools import wraps

from django.db import transaction
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_GET, require_POST

from . import planning, scheduling, site_scope
from .board import board_data
from .importers import import_orders_csv
from .models import Downtime, Operation, ScheduleChange, Site, WorkCenter


def _json_api(view):
    """Convierte ValidationError / errores de entrada en respuestas 400 con mensaje legible."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except scheduling.FrozenPlanError as exc:
            return JsonResponse({"error": "; ".join(exc.messages), "frozen": True}, status=409)
        except ValidationError as exc:
            return JsonResponse({"error": "; ".join(exc.messages)}, status=400)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return JsonResponse({"error": f"Petición inválida: {exc}"}, status=400)

    return wrapper


def _perm(codename):
    """Como permission_required, pero responde JSON 403 (lo muestra el tablero)."""

    def deco(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.has_perm(codename):
                return JsonResponse({"error": "No tienes permiso para esta acción (pídelo a un administrador)."}, status=403)
            return view(request, *args, **kwargs)

        return wrapper

    return deco


def _body(request) -> dict:
    return json.loads(request.body or b"{}")


def _dt(value):
    dt = parse_datetime(value) if value else None
    if dt is None:
        raise ValueError(f"fecha inválida: {value!r}")
    return dt if timezone.is_aware(dt) else timezone.make_aware(dt)


@login_required
def scheduler(request):
    return render(request, "mes_core/scheduler.html", {
        "sites": Site.objects.filter(active=True),
        # Sin permiso de planeación el programador es de solo consulta (las API también lo exigen).
        "can_plan": request.user.has_perm("mes_core.plan_workorder"),
        "can_import": request.user.has_perm("mes_core.add_workorder"),
    })


@login_required
@require_GET
@_json_api
def api_board(request):
    site, _ = site_scope.site_and_sites(request)  # la bodega elegida antes (o la más usada), no la primera de la lista
    if site is None:
        return JsonResponse({"error": "No hay plantas activas. Crea una en Administración."}, status=400)
    view = request.GET.get("view", "semana")
    if view not in scheduling.VIEWS:
        view = "semana"
    anchor = parse_date(request.GET.get("date") or "") or timezone.localdate()
    line = request.GET.get("line")
    return JsonResponse(board_data(site, view, anchor, int(line) if line else None))


@login_required
@require_POST
@_perm("mes_core.plan_workorder")
@_json_api
def api_place(request):
    data = _body(request)
    op = get_object_or_404(Operation, pk=data["operation"])
    wc = get_object_or_404(WorkCenter, pk=data["line"])
    scheduling.place_operation(op, wc, _dt(data["slot_start"]), _dt(data["slot_end"]), user=request.user,
                               force=bool(data.get("force")))
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_perm("mes_core.plan_workorder")
@_json_api
def api_edit(request):
    """Panel de edición: línea + inicio/fin exactos, y opcionalmente compromiso/prioridad de la orden."""
    data = _body(request)
    op = get_object_or_404(Operation.objects.select_related("order"), pk=data["operation"])
    wc = get_object_or_404(WorkCenter, pk=data["line"])
    with transaction.atomic():
        due = None
        if data.get("due_date"):
            day = parse_date(data["due_date"])
            if day is None:
                raise ValueError("fecha compromiso inválida")
            current = timezone.localtime(op.order.due_date) if op.order.due_date else None
            due = timezone.make_aware(datetime.combine(day, current.time() if current else time(23, 59)))
        scheduling.update_order_terms(op.order, due_date=due, priority=data.get("priority"), user=request.user)
        scheduling.update_operation(op, wc=wc, start=_dt(data["start"]), end=_dt(data["end"]), user=request.user,
                                    force=bool(data.get("force")))
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_perm("mes_core.plan_workorder")
@_json_api
def api_resize(request):
    data = _body(request)
    op = get_object_or_404(Operation, pk=data["operation"])
    scheduling.resize_operation(op, _dt(data["end"]), user=request.user, force=bool(data.get("force")))
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_perm("mes_core.plan_workorder")
@_json_api
def api_unschedule(request):
    data = _body(request)
    op = get_object_or_404(Operation, pk=data["operation"])
    scheduling.unschedule_operation(op, user=request.user, force=bool(data.get("force")))
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_perm("mes_core.plan_workorder")
@_json_api
def api_auto(request):
    data = _body(request)
    site = get_object_or_404(Site, pk=data["site"])
    result = planning.generate_plan(site, keep_existing=data.get("keep_existing", True), user=request.user)
    return JsonResponse({
        "ok": True,
        "scheduled": result.scheduled_operations,
        "late": [o.number for o in result.late_orders],
        "unassigned": sorted({op.work_center_kind for op in result.unassigned_operations}),
    })


@login_required
@require_POST
@_perm("mes_core.plan_workorder")
@_json_api
def api_downtime_create(request):
    data = _body(request)
    wc = get_object_or_404(WorkCenter, pk=data["line"])
    scheduling.create_downtime(work_center=wc, start=_dt(data["start"]), end=_dt(data["end"]),
                               reason=data.get("reason", "").strip() or "Mantenimiento", user=request.user)
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_perm("mes_core.plan_workorder")
@_json_api
def api_downtime_delete(request, pk):
    scheduling.delete_downtime(get_object_or_404(Downtime, pk=pk), user=request.user)
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_perm("mes_core.add_workorder")
@_json_api
def api_import(request):
    upload = request.FILES.get("file")
    if not upload:
        raise ValidationError("Adjunta un archivo CSV.")
    if upload.size > 2 * 1024 * 1024:
        raise ValidationError("El archivo supera 2 MB.")
    created, errors = import_orders_csv(upload.read(), user=request.user)
    if errors:
        return JsonResponse({"error": "No se importó nada.", "details": errors[:50]}, status=400)
    return JsonResponse({"ok": True, "created": created})


# --- Tablero de planta (en vivo) -------------------------------------------------------

@login_required
def plant_board(request):
    scope = site_scope.pick(request, allow_all=True)
    return render(request, "mes_core/plant_board.html", {"sites": scope.sites, "site": scope.site, "scope": scope,
                                                         "site_allow_all": True})


@login_required
@require_GET
@_json_api
def api_plant_board(request):
    from .plant_board import plant_board_all, plant_board_data

    scope = site_scope.pick(request, allow_all=True)
    if not scope.sites:
        return JsonResponse({"error": "No hay plantas activas. Crea una en Administración."}, status=400)
    if scope.all:
        return JsonResponse(plant_board_all(scope.sites))
    return JsonResponse(plant_board_data(scope.site))



@login_required
@require_POST
@_perm("mes_core.plan_workorder")
@_json_api
def api_split(request):
    """Divide una operación entre varias líneas: {operation, parts: [{line, qty}, …], force}."""
    from decimal import Decimal, InvalidOperation

    data = _body(request)
    op = get_object_or_404(Operation.objects.select_related("order"), pk=data["operation"])
    parts = []
    for p in data.get("parts") or []:
        wc = get_object_or_404(WorkCenter, pk=p["line"])
        try:
            qty = Decimal(str(p["qty"]))
        except (InvalidOperation, KeyError):
            raise ValueError("cantidad inválida")
        parts.append((wc, qty))
    created = scheduling.split_operation(op, parts, user=request.user, force=bool(data.get("force")))
    return JsonResponse({"ok": True, "parts": [p.pk for p in created]})


@login_required
@require_POST
@_perm("mes_core.plan_workorder")
@_json_api
def api_merge(request):
    op = get_object_or_404(Operation, pk=_body(request)["operation"])
    scheduling.merge_operation(op, user=request.user)
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_perm("mes_core.plan_workorder")
@_json_api
def api_stage_link(request):
    """Relación de la etapa de una operación con la anterior: {operation, link, link_lag, reschedule}.

    reschedule=true vuelve a programar la orden con la nueva relación (respeta lo congelado)."""
    from decimal import Decimal, InvalidOperation

    from .models import PlanStage

    data = _body(request)
    op = get_object_or_404(Operation.objects.select_related("order", "stage"), pk=data["operation"])
    ps = op.stage
    if ps is None:
        raise ValidationError("Esta operación no viene de la planeación por etapas.")
    link = data.get("link")
    if link not in PlanStage.Link.values:
        raise ValidationError("Relación inválida.")
    first = not ps.order.plan_stages.filter(sequence__lt=ps.sequence).exists()
    if first and link != PlanStage.Link.QUEUE:
        raise ValidationError("Es la primera etapa: no tiene etapa anterior con la cual ir a la par.")
    lag = None
    if link == PlanStage.Link.FLOW and data.get("link_lag") not in (None, ""):
        try:
            lag = Decimal(str(data["link_lag"]).replace(",", "."))
        except InvalidOperation:
            raise ValidationError("Unidades para arrancar inválidas.") from None
        if lag <= 0:
            raise ValidationError("Las unidades para arrancar deben ser mayores que cero.")
    ps.link, ps.link_lag = link, lag
    ps.save(update_fields=["link", "link_lag"])
    label = PlanStage.Link(link).label.split(":")[0]
    scheduling._log(op.order.site, ScheduleChange.Action.EDIT,
                    f"{op.order.number} · {ps.name}: {label.lower()}"
                    + (f" (arranca con {lag:g} unidades)" if lag else ""), request.user)
    scheduled = 0
    if data.get("reschedule"):
        result = planning.generate_plan(op.order.site, only_orders={op.order_id}, user=request.user)
        scheduled = result.scheduled_operations
    return JsonResponse({"ok": True, "scheduled": scheduled})
