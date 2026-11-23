"""Logística: «mercar» un pedido = alistar su mercancía y dejarla ubicada para que producción la tome.

Logística no modifica el pedido: solo lo marca como mercado con quién lo mercó, dónde quedó la mercancía y
observaciones. Al marcarlo, la mercancía del pedido queda al 100 % en el tablero de capacidad y, si insumos ya
liberó su parte, el pedido pasa a planta (ver supplies.try_release)."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import ScheduleChange, WorkOrder

PERM = "mes_core.mark_goods"
# Pedidos que logística ve: ya planeados y aún no terminados.
QUEUE_STATUSES = (WorkOrder.Status.PLANNED, WorkOrder.Status.SUPPLIES, WorkOrder.Status.RELEASED,
                  WorkOrder.Status.IN_PROGRESS)


def _clean(value, max_len) -> str:
    return " ".join(str(value or "").split())[:max_len]


def _log(order, text, user):
    ScheduleChange.objects.create(site=order.site, action=ScheduleChange.Action.GOODS, description=text[:255],
                                  user=user if user is not None and user.is_authenticated else None)


def queue(site):
    return WorkOrder.objects.filter(site=site, status__in=QUEUE_STATUSES)


@transaction.atomic
def mark_picked(order: WorkOrder, picked_by: str, location: str, note: str = "", user=None) -> bool:
    """Marca el pedido como mercado (o corrige los datos si ya lo estaba). Devuelve True si cambió de estado."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    if order.status not in QUEUE_STATUSES:
        raise ValidationError(f"El pedido {order.number} está «{order.get_status_display()}»: no se puede mercar.")
    picked_by, location = _clean(picked_by, 120), _clean(location, 120)
    errors = {}
    if not picked_by:
        errors["picked_by"] = "Escribe quién mercó."
    if not location:
        errors["location"] = "Escribe dónde quedó la mercancía."
    if errors:
        raise ValidationError(errors)
    note = "\n".join(" ".join(line.split()) for line in str(note or "").strip().splitlines()).strip()
    first = order.goods_status != WorkOrder.GoodsStatus.PICKED
    order.goods_status = WorkOrder.GoodsStatus.PICKED
    order.goods_picked_by, order.goods_location, order.goods_note = picked_by, location, note
    fields = ["goods_status", "goods_picked_by", "goods_location", "goods_note"]
    if first:
        order.goods_marked_by = user if user is not None and user.is_authenticated else None
        order.goods_marked_at = timezone.now()
        order.goods_pct = 100
        fields += ["goods_marked_by", "goods_marked_at", "goods_pct"]
    order.save(update_fields=fields)
    verb = "mercado" if first else "datos de mercancía corregidos"
    _log(order, f"{order.number} {verb}: {picked_by} · ubicación {location}", user)
    if first:
        from .supplies import try_release

        try_release(order, user)  # si insumos ya liberó, el pedido pasa a planta ahora
    return first


@transaction.atomic
def unmark(order: WorkOrder, reason: str, user=None) -> None:
    """Devuelve el pedido a «Por mercar» (p. ej. se marcó por error). Exige el motivo."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    reason = _clean(reason, 200)
    if not reason:
        raise ValidationError("Escribe por qué vuelve a «Por mercar».")
    if order.goods_status != WorkOrder.GoodsStatus.PICKED:
        return
    stamp = timezone.localtime().strftime("%d/%m %H:%M")
    order.goods_status = WorkOrder.GoodsStatus.PENDING
    order.goods_pct = 0
    order.goods_marked_by, order.goods_marked_at = None, None
    order.goods_note = f"[{stamp}] Vuelve a por mercar: {reason}" + (f"\n{order.goods_note}" if order.goods_note else "")
    order.save(update_fields=["goods_status", "goods_pct", "goods_marked_by", "goods_marked_at", "goods_note"])
    _log(order, f"{order.number} vuelve a por mercar: {reason}", user)
