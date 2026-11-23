"""Aviso a validación EAN cuando entra un pedido nuevo."""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.urls import reverse

log = logging.getLogger(__name__)


def validators():
    q = (Q(groups__permissions__codename="manage_eans", groups__permissions__content_type__app_label="eanes")
         | Q(user_permissions__codename="manage_eans", user_permissions__content_type__app_label="eanes"))
    return get_user_model().objects.filter(is_active=True).filter(q).distinct()


def order_needs_eans(order) -> None:
    from mes_core.models import Notification, WorkOrder
    from mes_core.notify import _qty, notify

    if order.status == WorkOrder.Status.CANCELLED:
        return
    notify(validators(), Notification.Kind.NEW_ORDER, f"Pedido nuevo {order.number}: cargar sus EAN",
           f"{order.product.sku} · {order.product.name} × {_qty(order)}"
           + (f" · {order.customer.name}" if order.customer_id else ""),
           reverse("eanes:order", args=[order.pk]), order.site, f"ean:{order.pk}", exclude=order.created_by)


def on_order_created(sender, order, **kwargs):
    from mes_core.order_import import bulk_import

    if bulk_import.get():  # carga masiva desde Excel: los ven en su cola, sin un aviso por pedido
        return

    def run():
        try:
            order_needs_eans(order)
        except Exception:  # noqa: BLE001 - un aviso nunca debe tumbar la creación del pedido
            log.exception("No se pudo avisar a validación EAN del pedido %s", order.pk)

    transaction.on_commit(run)
