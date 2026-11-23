"""Bandeja de pendientes por rol: avisos en la aplicación (campana) y, si la planta configuró correo, por e-mail.

Eventos que avisan:
    pedido nuevo (ventas)              → planeación
    enviado a insumos (planeación)     → insumos y logística (trabajan en paralelo)
    liberado a planta                  → supervisores de sus operaciones (o planeación si no tiene supervisor)
    insumo bajo stock de seguridad     → insumos
    asistencia sin confirmar a tiempo  → jefe de planta y el supervisor
    pedido en riesgo (atrasado)        → planeación y el supervisor          (risk.py)
    sugerencia de nivel de polivalencia → supervisor                          (skill_growth.py)

Las revisiones por tiempo (asistencia, riesgo, polivalencia) corren solas como máximo cada 5 minutos cuando alguien
usa la aplicación, y también con `python manage.py mes_avisos` (para el Programador de tareas de Windows).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import Notification, Site, WorkOrder

log = logging.getLogger(__name__)
K = Notification.Kind
CHECK_EVERY = timedelta(minutes=5)


def users_with_perm(codename: str):
    """Usuarios activos con el permiso por grupo o directo (los superusuarios solo si están en el grupo)."""
    return (get_user_model().objects.filter(is_active=True)
            .filter(Q(groups__permissions__codename=codename, groups__permissions__content_type__app_label="mes_core")
                    | Q(user_permissions__codename=codename, user_permissions__content_type__app_label="mes_core"))
            .distinct())


def notify(users, kind: str, title: str, body: str = "", url: str = "", site=None, key: str = "",
           exclude=None) -> int:
    """Crea el aviso para cada usuario (una sola vez por `key`). Devuelve cuántos se crearon."""
    skip = exclude.pk if exclude is not None and getattr(exclude, "is_authenticated", False) else None
    created = []
    for user in users:
        if user.pk == skip:
            continue
        if key and Notification.objects.filter(user=user, key=key).exists():
            continue
        try:
            with transaction.atomic():
                created.append(Notification.objects.create(user=user, kind=kind, title=title[:200], body=body,
                                                           url=url[:300], site=site, key=key[:120]))
        except IntegrityError:  # otro proceso ya lo creó
            continue
    if created and getattr(settings, "MES_EMAIL_NOTIFICATIONS", False):
        transaction.on_commit(lambda: _email([n.pk for n in created]))
    return len(created)


def _email(ids) -> None:
    from django.core.mail import send_mail

    base = getattr(settings, "MES_BASE_URL", "")
    for n in Notification.objects.filter(pk__in=ids, emailed_at__isnull=True).select_related("user"):
        if not n.user.email:
            continue
        text = n.body + (f"\n\nAbrir: {base}{n.url}" if n.url else "") + "\n\n— MES Planta (aviso automático)"
        try:
            send_mail(f"[MES] {n.title}", text.strip(), None, [n.user.email], fail_silently=False)
        except Exception:  # noqa: BLE001 - el correo nunca debe tumbar la operación
            log.exception("No se pudo enviar el aviso %s por correo", n.pk)
            continue
        Notification.objects.filter(pk=n.pk).update(emailed_at=timezone.now())


def _order_url(order) -> str:
    return reverse("mes_core:order_detail", args=[order.pk])


def _qty(order) -> str:
    q = order.quantity
    return f"{q.normalize():f}" if q == q.to_integral() else f"{q:f}"


# ---------------------------------------------------------------- eventos del flujo
def order_created(order, user=None) -> None:
    if order.status == WorkOrder.Status.CANCELLED:
        return
    who = user or order.created_by
    notify(users_with_perm("plan_workorder"), K.NEW_ORDER, f"Pedido nuevo {order.number}: planearlo",
           f"{order.product.sku} · {order.product.name} × {_qty(order)}"
           + (f" · {order.customer.name}" if order.customer_id else "")
           + (f" · compromiso {timezone.localtime(order.due_date):%d/%m %H:%M}" if order.due_date else ""),
           _order_url(order), order.site, f"new:{order.pk}", exclude=who)


def orders_imported(orders, user=None) -> None:
    by_site: dict = {}
    for o in orders:
        by_site.setdefault(o.site, []).append(o)
    for site, items in by_site.items():
        nums = ", ".join(o.number for o in items[:12]) + ("…" if len(items) > 12 else "")
        notify(users_with_perm("plan_workorder"), K.NEW_ORDER, f"{len(items)} pedidos nuevos cargados desde Excel: planearlos",
               nums, reverse("mes_core:planning_list") + f"?site={site.pk}", site,
               f"import:{items[0].pk}:{items[-1].pk}", exclude=user)


def order_to_supplies(order, user=None) -> None:
    body = f"{order.product.sku} · {order.product.name} × {_qty(order)}"
    if order.planned_start:
        body += f" · inicia {timezone.localtime(order.planned_start):%a %d/%m %H:%M}"
    notify(users_with_perm("release_to_plant"), K.TO_SUPPLIES, f"{order.number} llegó a insumos: entregar y liberar",
           body, reverse("mes_core:supply_order", args=[order.pk]), order.site, f"sup:{order.pk}:{order.sent_to_supplies_at:%Y%m%d%H%M%S}",
           exclude=user)
    if order.goods_status != WorkOrder.GoodsStatus.PICKED:
        notify(users_with_perm("mark_goods"), K.TO_SUPPLIES, f"{order.number} por mercar", body,
               f"{reverse('mes_core:goods_queue')}?site={order.site_id}&q={order.number}", order.site,
               f"goods:{order.pk}:{order.sent_to_supplies_at:%Y%m%d%H%M%S}", exclude=user)


def order_released(order, user=None) -> None:
    ops = list(order.operations.select_related("supervisor").order_by("planned_start"))
    sups = {op.supervisor for op in ops if op.supervisor_id}
    first = next((op.planned_start for op in ops if op.planned_start), None)
    body = f"{order.product.sku} · {order.product.name} × {_qty(order)}"
    if first:
        body += f" · inicia {timezone.localtime(first):%a %d/%m %H:%M}"
    if order.goods_location:
        body += f" · mercancía en {order.goods_location}"
    if sups:
        notify(sups, K.RELEASED, f"{order.number} liberado a planta: arma la cuadrilla", body,
               reverse("mes_core:supervision_home") + f"?site={order.site_id}", order.site, f"rel:{order.pk}")
    else:
        notify(users_with_perm("assign_supervisor"), K.RELEASED, f"{order.number} pasó a planta sin supervisor",
               body + ". Asígnale supervisor.", reverse("mes_core:supervisor_assign") + f"?site={order.site_id}",
               order.site, f"rel:{order.pk}")


def low_stock(stock) -> None:
    today = timezone.localdate()
    notify(users_with_perm("release_to_plant"), K.LOW_STOCK,
           f"{stock.product.name} bajo stock de seguridad",
           f"Quedan {stock.on_hand.normalize():f} {stock.product.unit}; el stock de seguridad es "
           f"{stock.safety_stock.normalize():f}. Toca reponer.",
           f"{reverse('mes_core:supply_inventory')}?site={stock.site_id}", stock.site, f"stock:{stock.pk}:{today}")


# ---------------------------------------------------------------- revisiones por tiempo
def attendance_check(site, now=None) -> int:
    """Al vencer el plazo de confirmación de un turno: avisa al jefe de planta cuántos operarios de ese turno siguen sin
    confirmar en la lista general, y recuerda a los supervisores con operaciones en ese turno que no han marcado a nadie."""
    from .models import Attendance, Employee, Operation
    from .planning import get_rules
    from .scheduling import shift_window
    from .staffing import shift_day_of, user_label
    from .workcal import get_calendar

    now = now or timezone.now()
    rules = get_rules(site)
    grace = timedelta(minutes=rules.attendance_grace_minutes if rules else 60)
    cal = get_calendar(site.pk)
    sent = 0
    for sh in site.shifts.filter(active=True):
        match, day = shift_day_of(site, now, [sh])
        if match is None or cal.shift_off_reason(sh, day):
            continue
        start, end = shift_window(sh, day)
        if now < start + grace:
            continue
        url = f"{reverse('mes_core:supervision_home')}?site={site.pk}&day={day.isoformat()}"
        # Personal general: los de turno habitual en este turno (de cualquier planta de origen)
        emps = Employee.objects.filter(active=True, shift=sh)
        total = emps.count()
        day_att = Attendance.objects.filter(day=day)
        pending = emps.exclude(pk__in=day_att.values("employee_id")).count()
        # Supervisores con operaciones en este turno que no han marcado a nadie en la lista general
        sups = {op.supervisor for op in Operation.objects.filter(order__site=site, supervisor__isnull=False,
                                                                  planned_start__lt=end, planned_end__gt=start)
                .exclude(status=Operation.Status.DONE).select_related("supervisor")}
        marked = set(day_att.values_list("confirmed_by_id", flat=True))
        quiet = sorted((s for s in sups if s.pk not in marked), key=user_label)
        for sup in quiet:
            sent += notify([sup], K.ATTENDANCE, f"Marca la asistencia del turno {sh.name}",
                           f"Tienes operaciones en el turno {sh.name} ({day:%d/%m}) y aún no has marcado a nadie. "
                           "Busca en la lista general a los que tienes contigo y márcalos presentes.", url + "&lista=todos",
                           site, f"att:{sh.pk}:{day}:{sup.pk}")
        if pending or quiet:
            parts = []
            if pending:
                parts.append(f"{pending} de {total} operarios de turno {sh.name} sin confirmar en la lista general")
            if quiet:
                parts.append("supervisores con operaciones que no han marcado a nadie: "
                             + ", ".join(user_label(s) for s in quiet))
            text = "; ".join(parts)
            sent += notify(users_with_perm("approve_plan"), K.ATTENDANCE,
                           f"Asistencia sin confirmar · turno {sh.name} {day:%d/%m}", text[:1].upper() + text[1:] + ".",
                           url + "&lista=pendientes&ops=todas", site, f"att:{sh.pk}:{day}")
    return sent


def run_checks(now=None, force: bool = False) -> dict:
    """Corre las revisiones por tiempo de todas las plantas (como máximo cada 5 minutos salvo `force`)."""
    if not force and (not getattr(settings, "MES_AUTO_CHECKS", True)
                      or not cache.add("mes_checks_lock", 1, int(CHECK_EVERY.total_seconds()))):
        return {}
    from . import risk, services, skill_growth

    out = {"asistencia": 0, "riesgo": 0, "polivalencia": 0, "cerradas": 0}
    try:  # operaciones que ya llegaron al 100 % y siguen abiertas: se cierran solas
        out["cerradas"] = services.close_completed()
    except Exception:  # noqa: BLE001
        log.exception("Falló el cierre automático de operaciones completas")
    try:  # quien estaba con un supervisor por una cuadrilla ya terminada (o cancelada) vuelve a quedar libre
        from . import staffing

        out["cuadrillas"] = staffing.sync_crew_supervisors()
    except Exception:  # noqa: BLE001
        log.exception("Falló liberar al personal de cuadrillas terminadas")
    for site in Site.objects.filter(active=True):
        for name, fn in (("asistencia", attendance_check), ("riesgo", risk.check), ("polivalencia", skill_growth.check)):
            try:
                out[name] += fn(site, now) or 0
            except Exception:  # noqa: BLE001 - una revisión con error no debe frenar las demás ni la página
                log.exception("Falló la revisión %s de %s", name, site)
    return out


# ---------------------------------------------------------------- conexión
def _on_created(sender, order, **kwargs):
    from .order_import import bulk_import

    if bulk_import.get():  # carga desde Excel: un solo aviso al final (orders_imported)
        return
    transaction.on_commit(lambda: _safe(order_created, order))


def _on_released(sender, order, **kwargs):
    transaction.on_commit(lambda: _safe(order_released, order))


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        log.exception("No se pudo crear el aviso %s", fn.__name__)


def connect_signals() -> None:
    from . import signals

    signals.order_created.connect(_on_created, sender=WorkOrder, dispatch_uid="mes_notify_created")
    signals.order_released.connect(_on_released, sender=WorkOrder, dispatch_uid="mes_notify_released")
