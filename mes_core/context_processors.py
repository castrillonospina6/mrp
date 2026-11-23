from django.utils import timezone

from .plugins import registry


def mes(request):
    ctx = {"mes_plugin_menu": [], "mes_tz": timezone.get_current_timezone_name()}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        items = [item for item in registry.menu_items() if item.visible_to(user)]
        # El control de piso (tablets) tiene su propia sección en el menú; el resto va en «Módulos»
        ctx["piso_menu"] = [i for i in items if i.url_name.startswith("piso:")]
        ctx["mes_plugin_menu"] = [i for i in items if not i.url_name.startswith("piso:")]
        ctx["menu"] = menu_flags(user)
        if user.is_superuser:
            role = "Administrador"
        else:
            role = ", ".join(user.groups.values_list("name", flat=True)) or "Usuario"
        ctx["nav_role"] = role
        from .notify import run_checks

        try:
            run_checks()  # asistencia, atrasos y polivalencia: como máximo cada 5 minutos
        except Exception:  # noqa: BLE001 - los avisos nunca deben impedir mostrar la página
            import logging

            logging.getLogger(__name__).exception("Falló la revisión de avisos")
        ctx["nav_unread"] = user.notifications.filter(read_at__isnull=True).count()
        if user.has_perm("mes_core.plan_workorder"):
            from .models import WorkOrder

            ctx["nav_to_plan"] = WorkOrder.objects.filter(status=WorkOrder.Status.TO_PLAN).count()
        if user.has_perm("mes_core.approve_plan"):
            from .models import PlanVersion

            ctx["nav_to_approve"] = PlanVersion.objects.filter(status=PlanVersion.Status.PROPOSED).count()
        if user.has_perm("mes_core.assign_supervisor") or user.has_perm("mes_core.supervise_crew"):
            ctx.update(_staff_counts(user))
        if user.has_perm("mes_core.mark_goods"):
            from .logistics import QUEUE_STATUSES
            from .models import WorkOrder

            ctx["nav_to_pick"] = WorkOrder.objects.filter(status__in=QUEUE_STATUSES,
                                                          goods_status=WorkOrder.GoodsStatus.PENDING).count()
        if user.has_perm("mes_core.release_to_plant"):
            from django.db.models import F

            from .models import SupplyStock, WorkOrder

            ctx["nav_to_release"] = WorkOrder.objects.filter(status=WorkOrder.Status.SUPPLIES,
                                                             supplies_released_at__isnull=True).count()
            ctx["nav_low_stock"] = SupplyStock.objects.filter(active=True, safety_stock__gt=0,
                                                              on_hand__lt=F("safety_stock")).count()
    return ctx


def menu_flags(user) -> dict:
    """Qué ve cada cuenta en el menú: solo lo que su rol usa (p. ej. logística: tablero de planta, pedidos y mercar)."""
    p = user.has_perm
    plans = p("mes_core.plan_workorder")
    boss = p("mes_core.approve_plan")
    sup = p("mes_core.supervise_crew")
    flags = {
        "capacity": plans or boss,
        "plant_board": p("mes_core.view_operation"),
        "at_risk": plans or boss or sup,
        "orders": p("mes_core.view_workorder"),
        "scheduler": plans or boss or sup,
        "gantt": plans or boss or sup,
        "calendar": plans or p("mes_core.view_calendarexception"),
        "plugins": user.is_staff or plans or boss,
    }
    flags["planning_sec"] = plans or flags["scheduler"] or flags["gantt"] or flags["calendar"]
    flags["config_sec"] = (plans or p("mes_core.view_servicetype") or p("mes_core.change_shift") or flags["plugins"]
                           or user.is_staff)
    return flags


def _staff_counts(user) -> dict:
    """Operaciones de los próximos 7 días sin supervisor (planeación) y, del supervisor, las de hoy sin cuadrilla completa."""
    from datetime import datetime, time, timedelta

    from django.db.models import Count, F

    from .models import Operation, WorkOrder

    tz = timezone.get_current_timezone()
    today = datetime.combine(timezone.localdate(), time(0), tzinfo=tz)
    base = Operation.objects.filter(order__status__in=WorkOrder.OPEN_STATUSES).exclude(status=Operation.Status.DONE)
    out = {}
    if user.has_perm("mes_core.assign_supervisor"):
        out["nav_no_supervisor"] = base.filter(supervisor__isnull=True, planned_start__lt=today + timedelta(days=7),
                                               planned_end__gt=timezone.now()).count()
    if user.has_perm("mes_core.supervise_crew"):
        out["nav_crew_missing"] = (base.filter(supervisor=user, planned_start__lt=today + timedelta(days=1),
                                               planned_end__gt=today)
                                   .annotate(n=Count("crew_assignments")).filter(n__lt=F("crew")).count())
    return out
