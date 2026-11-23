"""Casos de uso del núcleo. Las vistas, el admin y los plugins deben pasar por aquí."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from . import signals
from .models import Operation, Product, ProductionReport, ScheduleChange, WorkOrder
from .plugins import registry


@transaction.atomic
def create_order(*, site, product, process_type: str, quantity, **extra) -> WorkOrder:
    """Crea la orden y genera sus operaciones a partir de la ruta del tipo de proceso."""
    _check_product(product)
    order = WorkOrder(site=site, product=product, process_type=process_type, quantity=Decimal(quantity), **extra)
    order.full_clean(exclude=["number"])
    order.save()
    generate_operations(order)
    signals.order_created.send(sender=WorkOrder, order=order)
    return order


def _check_product(product) -> None:
    if product is not None and product.kind != Product.Kind.PRODUCT:
        raise ValidationError(product.kind_error(Product.Kind.PRODUCT) + " Un pedido solo lleva productos.")


def resolve_product(text: str, kind: str = Product.Kind.PRODUCT) -> tuple[Product, bool]:
    """Artículo escrito a mano → (artículo, creado). `kind`: producto (pedidos) o insumo (inventario).

    Busca solo entre los de ese tipo: por SKU y luego por nombre (sin distinguir mayúsculas). Si no existe, lo crea con
    un SKU derivado del texto (CAMISETA-POLO-AZUL); la unidad queda en «UN» y se puede corregir en Administración.
    Productos e insumos nunca se mezclan: si el SKU escrito es de un artículo del otro tipo, se rechaza."""
    text = " ".join((text or "").split())
    if not text:
        raise ValidationError("Escribe el insumo." if kind == Product.Kind.SUPPLY else "Escribe el producto.")
    by_sku = Product.objects.filter(sku__iexact=text).first()
    if by_sku is not None:
        if by_sku.kind != kind:
            raise ValidationError(by_sku.kind_error(kind) + " Usa otro código.")
        return by_sku, False
    found = Product.objects.filter(kind=kind, name__iexact=text).first()
    if found:
        return found, False
    base = re.sub(r"[^A-Z0-9]+", "-", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().upper())
    base = base.strip("-")[:34] or ("INSUMO" if kind == Product.Kind.SUPPLY else "PRODUCTO")
    sku, n = base, 1
    while Product.objects.filter(sku=sku).exists():
        n += 1
        sku = f"{base}-{n}"
    return Product.objects.create(sku=sku, name=text[:200], kind=kind), True


def production_deadline(site, dispatch_date) -> datetime:
    """La producción debe estar lista al inicio de la jornada del día de despacho
    (primer turno diurno de la planta; 00:00 si no hay turnos)."""
    from .scheduling import site_shifts

    day_starts = [s.start_time for s in site_shifts(site) if not s.crosses_midnight]
    return timezone.make_aware(datetime.combine(dispatch_date, min(day_starts) if day_starts else time(0)))


@transaction.atomic
def receive_order(*, site, product, process_type: str, quantity, delivery_date: date, transit_days: int = 0,
                  received_on: date | None = None, user=None, **extra) -> WorkOrder:
    """Pedido registrado por ventas: queda «Por planear», sin etapas; planeación define el proceso."""
    _check_product(product)
    received_on = received_on or timezone.localdate()
    order = WorkOrder(
        site=site, product=product, process_type=process_type, quantity=Decimal(quantity),
        received_on=received_on, delivery_date=delivery_date, transit_days=transit_days,
        status=WorkOrder.Status.TO_PLAN, created_by=user if user and user.is_authenticated else None, **extra,
    )
    if delivery_date < received_on:
        raise ValidationError("La fecha de entrega no puede ser anterior a la recepción del pedido.")
    if order.dispatch_date < received_on:
        raise ValidationError(
            f"Con {transit_days} día(s) de tránsito el vehículo debería haber salido el "
            f"{order.dispatch_date:%d/%m/%Y}, antes de recibir el pedido.")
    order.due_date = production_deadline(site, order.dispatch_date)
    order.full_clean(exclude=["number"])
    order.save()
    ScheduleChange.objects.create(
        site=site, action=ScheduleChange.Action.ORDER,
        description=f"{order.number}: {product.sku} × {quantity:g} para {order.customer or 'sin cliente'}, "
                    f"despacho {order.dispatch_date:%d/%m}",
        user=order.created_by,
    )
    signals.order_created.send(sender=WorkOrder, order=order)
    return order


def generate_operations(order: WorkOrder) -> list[Operation]:
    pt = registry.get_process_type(order.process_type)
    if pt is None:
        raise ValidationError(f"Tipo de proceso desconocido: {order.process_type}")
    ops = [
        Operation(
            order=order,
            sequence=(i + 1) * 10,
            name=tpl.name,
            work_center_kind=tpl.work_center_kind,
            setup_minutes=Decimal(str(tpl.setup_minutes)),
            minutes_per_unit=Decimal(str(tpl.minutes_per_unit)),
        )
        for i, tpl in enumerate(pt.operations)
    ]
    return Operation.objects.bulk_create(ops)


@transaction.atomic
def release_order(order: WorkOrder) -> WorkOrder:
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    if order.status not in (WorkOrder.Status.DRAFT, WorkOrder.Status.PLANNED, WorkOrder.Status.SUPPLIES):
        raise ValidationError(f"No se puede liberar una orden en estado '{order.get_status_display()}'.")
    if not order.operations.exists():
        raise ValidationError("La orden no tiene etapas; primero debe planearse.")
    order.status = WorkOrder.Status.RELEASED
    order.save(update_fields=["status"])
    signals.order_released.send(sender=WorkOrder, order=order)
    return order


@transaction.atomic
def cancel_order(order: WorkOrder) -> WorkOrder:
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    if order.status == WorkOrder.Status.DONE:
        raise ValidationError("Una orden terminada no se puede cancelar.")
    order.status = WorkOrder.Status.CANCELLED
    order.save(update_fields=["status"])
    return order


def _fmt_qty(value: Decimal) -> str:
    return f"{value.normalize():f}" if value == value.to_integral() else f"{value:g}"


@transaction.atomic
def report_production(
    operation: Operation, *, qty_good=0, qty_scrap=0, finished: bool = False, user=None, comment: str = "",
    allow_extra: bool = False, extra_reason: str = "", employee=None, source: str = "", merge_minutes: int = 0,
    ean: str = "", qty_leftover=0,
) -> ProductionReport:
    """Registra producción de una operación.

    Las buenas no pueden superar lo que falta de lo programado. Si de verdad salieron unidades de más, se reportan con
    ``allow_extra=True`` y un ``extra_reason``: lo que falta completa la operación y el excedente queda aparte
    (``qty_extra``), sin contar como cantidad pedida.

    ``ean``: la referencia trabajada (los módulos validan que sea del pedido y que no pase lo que falta de ella).
    ``qty_leftover``: códigos sobrantes; completan lo programado y la referencia, pero no son producción del operario.

    ``merge_minutes``: las marcaciones de piso (una por unidad) se acumulan en el último reporte del mismo operario,
    origen y referencia si tiene menos de esos minutos, para no crear un reporte por cada unidad."""
    operation = Operation.objects.select_for_update().select_related("order").get(pk=operation.pk)
    order = operation.order
    qty_good, qty_scrap, qty_leftover = Decimal(qty_good), Decimal(qty_scrap), Decimal(qty_leftover or 0)
    extra_reason = " ".join((extra_reason or "").split())[:255]

    if order.status not in (WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS):
        raise ValidationError("Solo se reporta producción en órdenes liberadas o en proceso.")
    if operation.status == Operation.Status.DONE:
        raise ValidationError("La operación ya está terminada.")
    if qty_good < 0 or qty_scrap < 0 or qty_leftover < 0:
        raise ValidationError("Las cantidades no pueden ser negativas.")

    pending = operation.qty_pending
    if qty_leftover > pending:
        raise ValidationError(f"Los códigos sobrantes ({_fmt_qty(qty_leftover)}) superan lo que falta de "
                              f"«{operation.name}» ({_fmt_qty(pending)}).")
    ean = registry.check_report(operation, ean=str(ean or "").strip(), qty_good=qty_good,
                                qty_leftover=qty_leftover).get("ean", str(ean or "").strip())
    pending -= qty_leftover
    extra = max(Decimal("0"), qty_good - pending)
    if extra > 0:
        if not allow_extra:
            raise ValidationError(
                f"«{operation.name}» tiene {_fmt_qty(operation.qty)} unidades programadas y ya se reportaron "
                f"{_fmt_qty(operation.qty_good)} buenas: faltan {_fmt_qty(pending)}. Estás reportando "
                f"{_fmt_qty(qty_good)} ({_fmt_qty(extra)} de más). Si de verdad se produjeron de más, marca "
                f"«Reportar unidades de más» y escribe el motivo.")
        if not extra_reason:
            raise ValidationError("Escribe el motivo de las unidades de más (p. ej. sobrante de material, pedido del cliente).")
    counted = qty_good - extra  # lo que cuenta para la cantidad programada

    now = timezone.now()
    report = None
    if merge_minutes and not extra and not qty_leftover:
        report = (ProductionReport.objects
                  .filter(operation=operation, employee=employee, source=source, reported_by=user, finished=False,
                          qty_extra=0, qty_leftover=0, ean=ean, edited_at__isnull=True,
                          reported_at__gte=now - timedelta(minutes=merge_minutes))
                  .order_by("-reported_at").first())
    if report is not None:
        report.qty_good += counted
        report.qty_scrap += qty_scrap
        report.finished = finished
        report.save(update_fields=["qty_good", "qty_scrap", "finished"])
    else:
        report = ProductionReport.objects.create(
            operation=operation, qty_good=counted, qty_scrap=qty_scrap, qty_extra=extra,
            extra_reason=extra_reason if extra else "", finished=finished, reported_by=user, comment=comment,
            employee=employee, source=source, ean=ean, qty_leftover=qty_leftover,
        )

    operation.qty_good += counted
    operation.qty_extra += extra
    operation.qty_scrap += qty_scrap
    operation.qty_leftover += qty_leftover
    if operation.actual_start is None:
        operation.actual_start = now
    if not finished and operation.qty_pending <= 0:
        # llegó al 100 % de lo programado (buenas + códigos sobrantes): la operación se cierra sola, sin que nadie
        # tenga que marcar «terminada»
        finished = True
        report.finished = True
        report.save(update_fields=["finished"])
    operation.status = Operation.Status.DONE if finished else Operation.Status.IN_PROGRESS
    if finished:
        operation.actual_end = now
    # Solo lo que cambia (más rápido y no recalcula reglas de programación que no aplican a un reporte)
    operation.save(update_fields=["qty_good", "qty_extra", "qty_scrap", "qty_leftover", "actual_start", "status",
                                  "actual_end"])

    # Lo terminado de la orden es lo bueno de su última operación.
    last_op = order.operations.order_by("-sequence").first()
    order.quantity_done = last_op.qty_good
    order.status = WorkOrder.Status.IN_PROGRESS
    completed = not order.operations.exclude(status=Operation.Status.DONE).exists()
    if completed:
        order.status = WorkOrder.Status.DONE
    order.save(update_fields=["quantity_done", "status"])

    signals.production_reported.send(sender=ProductionReport, report=report)
    if completed:
        signals.order_completed.send(sender=WorkOrder, order=order)
    return report


def close_completed() -> int:
    """Cierra solas las operaciones que ya tienen el 100 % de lo programado (buenas + sobrantes) y siguen abiertas
    (p. ej. reportadas antes de que el cierre fuera automático). Si con eso todas las de un pedido quedan terminadas,
    el pedido pasa a «Terminada» (con su aviso). Lo corre la revisión automática del MES cada 5 minutos."""
    from django.db.models import F
    from django.db.models.functions import Coalesce

    from .corrections import recompute_order

    open_full = (Operation.objects
                 .filter(order__status__in=[WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS])
                 .exclude(status=Operation.Status.DONE)
                 .annotate(planned=Coalesce(F("quantity"), F("order__quantity")))
                 .filter(planned__gt=0, qty_good__gte=F("planned") - F("qty_leftover"))
                 .values_list("pk", flat=True))
    closed = 0
    for pk in list(open_full):
        with transaction.atomic():
            op = Operation.objects.select_for_update().select_related("order").get(pk=pk)
            if op.status == Operation.Status.DONE or op.qty_pending > 0:
                continue
            last = op.reports.order_by("-reported_at").values_list("reported_at", flat=True).first()
            op.status = Operation.Status.DONE
            op.actual_end = last or timezone.now()
            if op.actual_start is None:
                op.actual_start = op.actual_end
            op.save(update_fields=["status", "actual_end", "actual_start"])
            closed += 1
            was_done = op.order.status == WorkOrder.Status.DONE
            order = recompute_order(op.order)
            if order.status == WorkOrder.Status.DONE and not was_done:
                transaction.on_commit(lambda o=order: signals.order_completed.send(sender=WorkOrder, order=o))
    return closed
