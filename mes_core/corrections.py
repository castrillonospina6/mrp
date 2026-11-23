"""Correcciones desde la aplicación (sin pasar por Administración): editar o eliminar reportes de producción y reabrir
la planeación completa de un pedido que ya empezó. Todo queda en la bitácora de cambios del programa."""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import OrderClosure, Operation, ProductionReport, ScheduleChange, Shortfall, VoidedReport, WorkOrder

ZERO = Decimal("0")
REPORTABLE = (WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS, WorkOrder.Status.DONE)


def _fmt(v) -> str:
    v = Decimal(v)
    return f"{v.normalize():f}" if v == v.to_integral() else f"{v:g}"


def _log(order: WorkOrder, text: str, user=None) -> None:
    ScheduleChange.objects.create(site=order.site, action=ScheduleChange.Action.EDIT, description=text[:255],
                                  user=user if getattr(user, "is_authenticated", False) else None)


def recompute_operation(op: Operation) -> Operation:
    """Rehace cantidades y estado de la operación a partir de los reportes que quedan (y del cierre con faltante)."""
    reps = list(op.reports.order_by("reported_at"))
    op.qty_good = sum((r.qty_good for r in reps), ZERO)
    op.qty_scrap = sum((r.qty_scrap for r in reps), ZERO)
    op.qty_extra = sum((r.qty_extra for r in reps), ZERO)
    op.qty_leftover = sum((r.qty_leftover for r in reps), ZERO)
    finished = [r for r in reps if r.finished]
    closed = OrderClosure.objects.filter(order_id=op.order_id, lines__operation=op).first()
    complete = bool(reps) and op.qty > 0 and op.qty_pending <= 0  # 100 % de lo programado: se cierra sola
    if finished or closed or complete:
        op.status = Operation.Status.DONE
        ends = ([r.reported_at for r in finished] + ([closed.closed_at] if closed else [])
                + ([reps[-1].reported_at] if complete else []))
        op.actual_end = max(ends)
    elif reps:
        op.status = Operation.Status.IN_PROGRESS
        op.actual_end = None
    else:
        op.status = Operation.Status.PENDING
        op.actual_end = None
    op.actual_start = reps[0].reported_at if reps else None
    op.save(update_fields=["qty_good", "qty_scrap", "qty_extra", "qty_leftover", "status", "actual_start",
                           "actual_end"])
    return op


def recompute_order(order: WorkOrder) -> WorkOrder:
    """Terminado = lo bueno de la última operación; el estado vuelve atrás si se quitó lo reportado."""
    ops = list(order.operations.order_by("sequence"))
    order.quantity_done = ops[-1].qty_good if ops else ZERO
    if ops and all(o.status == Operation.Status.DONE for o in ops):
        order.status = WorkOrder.Status.DONE
    elif any(o.status != Operation.Status.PENDING for o in ops):
        order.status = WorkOrder.Status.IN_PROGRESS
    elif order.status in (WorkOrder.Status.IN_PROGRESS, WorkOrder.Status.DONE):
        order.status = WorkOrder.Status.RELEASED  # ya estaba en planta: queda liberado, sin avance
    order.save(update_fields=["quantity_done", "status"])
    return order


@transaction.atomic
def delete_report(report: ProductionReport, user=None) -> None:
    op = Operation.objects.select_for_update().select_related("order__site").get(pk=report.operation_id)
    text = (f"{op.order.number}: eliminó un reporte de «{op.name}» ({_fmt(report.qty_good)} buenas, "
            f"{_fmt(report.qty_scrap)} merma{', ' + _fmt(report.qty_extra) + ' de más' if report.qty_extra else ''} "
            f"del {timezone.localtime(report.reported_at):%d/%m %H:%M})")
    report.delete()
    recompute_operation(op)
    recompute_order(op.order)
    _log(op.order, text, user)


@transaction.atomic
def delete_all_reports(order: WorkOrder, user=None) -> int:
    reps = ProductionReport.objects.filter(operation__order=order)
    agg = reps.aggregate(g=Sum("qty_good"), x=Sum("qty_scrap"))
    n = reps.count()
    if not n:
        return 0
    for r in reps:  # uno a uno: dispara las señales (p. ej. borra las marcaciones de piso asociadas)
        r.delete()
    for op in order.operations.all():
        recompute_operation(op)
    recompute_order(order)
    _log(order, f"{order.number}: eliminó todo lo reportado ({n} reporte(s), {_fmt(agg['g'] or 0)} buenas, "
                f"{_fmt(agg['x'] or 0)} merma)", user)
    return n


@transaction.atomic
def update_report(report: ProductionReport, *, qty_good, qty_scrap, finished: bool, comment: str = "",
                  user=None) -> ProductionReport:
    op = Operation.objects.select_for_update().select_related("order__site").get(pk=report.operation_id)
    try:
        good, scrap = Decimal(str(qty_good).replace(",", ".")), Decimal(str(qty_scrap).replace(",", "."))
    except Exception:  # noqa: BLE001
        raise ValidationError("Cantidades no válidas.") from None
    if good < 0 or scrap < 0:
        raise ValidationError("Las cantidades no pueden ser negativas.")
    others = op.reports.exclude(pk=report.pk).aggregate(g=Sum("qty_good"))["g"] or ZERO
    room = op.qty - others
    extra = max(ZERO, good - room)  # lo que pase de lo programado queda como «de más», igual que al reportar
    before = f"{_fmt(report.qty_good + report.qty_extra)} buenas / {_fmt(report.qty_scrap)} merma"
    report.qty_good, report.qty_extra, report.qty_scrap = good - extra, extra, scrap
    if extra and not report.extra_reason:
        report.extra_reason = "Corrección de un reporte"
    report.finished = finished
    report.comment = " ".join((comment or "").split())[:255]
    report.save(update_fields=["qty_good", "qty_extra", "qty_scrap", "finished", "comment", "extra_reason"])
    recompute_operation(op)
    recompute_order(op.order)
    _log(op.order, f"{op.order.number}: corrigió un reporte de «{op.name}»: {before} → {_fmt(good)} buenas / "
                   f"{_fmt(scrap)} merma", user)
    return report


def progress_summary(order: WorkOrder) -> dict:
    """Lo que se perdería al reabrir la planeación completa (para el aviso de confirmación)."""
    agg = ProductionReport.objects.filter(operation__order=order).aggregate(g=Sum("qty_good"), x=Sum("qty_scrap"))
    return {
        "reports": ProductionReport.objects.filter(operation__order=order).count(),
        "good": float(agg["g"] or 0), "scrap": float(agg["x"] or 0),
        "started": order.operations.exclude(status=Operation.Status.PENDING).count(),
        "crews": order.operations.filter(crew_assignments__isnull=False).distinct().count(),
    }


def can_reopen(order: WorkOrder) -> bool:
    """Desde planeación se puede rehacer todo siempre (en proceso, terminada o cancelada: esta se reactiva)."""
    return True


# ---------------------------------------------------------------- correcciones con motivo (como la app anterior)
def _reason(reason: str, what: str) -> str:
    reason = " ".join(str(reason or "").split())[:255]
    if not reason:
        raise ValidationError(f"Escribe el motivo {what}.")
    return reason


def _user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _dec(value, label: str) -> Decimal:
    try:
        v = Decimal(str(value if value not in (None, "") else 0).replace(",", "."))
    except Exception:  # noqa: BLE001
        raise ValidationError(f"{label}: cantidad no válida.") from None
    if v < 0:
        raise ValidationError(f"{label}: no puede ser negativa.")
    return v


def who_of(report: ProductionReport) -> str:
    if report.employee_id:
        return report.employee.display_name
    if report.reported_by_id:
        return report.reported_by.get_full_name() or report.reported_by.get_username()
    return "Tablet de la línea" if report.source == "piso" else "—"


def _snapshot(report: ProductionReport) -> dict:
    from .plugins import registry

    fields = ("operation_id", "qty_good", "qty_scrap", "qty_extra", "extra_reason", "finished", "reported_by_id",
              "comment", "employee_id", "source", "ean", "qty_leftover", "edited_by_id", "edit_reason")
    rep = {f: (str(getattr(report, f)) if isinstance(getattr(report, f), Decimal) else getattr(report, f))
           for f in fields}
    rep["reported_at"] = report.reported_at.isoformat()
    rep["edited_at"] = report.edited_at.isoformat() if report.edited_at else None
    return {"report": rep, "plugins": registry.report_snapshot(report)}


@transaction.atomic
def void_report(report: ProductionReport, reason: str, user=None) -> VoidedReport:
    """Anula un reporte: sale de todas las sumas (operación, pedido, referencia, eficiencias) pero se guarda completo
    para poder reactivarlo. Exige el motivo."""
    reason = _reason(reason, "de la anulación")
    report = ProductionReport.objects.select_related("operation__order", "employee", "reported_by").get(pk=report.pk)
    op = Operation.objects.select_for_update().select_related("order__site").get(pk=report.operation_id)
    voided = VoidedReport.objects.create(
        order=op.order, operation=op, original_id=report.pk, operation_name=op.name, ean=report.ean,
        qty_good=report.qty_good + report.qty_extra, qty_scrap=report.qty_scrap, qty_leftover=report.qty_leftover,
        who=who_of(report)[:160], reported_at=report.reported_at, data=_snapshot(report), reason=reason,
        voided_by=_user(user))
    report.delete()  # los módulos quitan lo suyo (p. ej. las marcaciones de piso), ya guardado en el respaldo
    recompute_operation(op)
    recompute_order(op.order)
    _log(op.order, f"{op.order.number}: anuló el reporte #{voided.original_id} de «{op.name}» "
                   f"({_fmt(voided.qty_good)} buenas{', EAN ' + voided.ean if voided.ean else ''}). Motivo: {reason}",
         user)
    return voided


@transaction.atomic
def void_all_reports(order: WorkOrder, reason: str, user=None) -> int:
    reason = _reason(reason, "de la anulación")
    reps = list(ProductionReport.objects.filter(operation__order=order).order_by("pk"))
    for r in reps:
        void_report(r, reason, user)
    return len(reps)


@transaction.atomic
def reactivate_report(voided: VoidedReport, reason: str, user=None) -> ProductionReport:
    """Vuelve a poner en las sumas un reporte anulado, si todavía cabe en lo que falta (operación y referencia)."""
    from .plugins import registry

    reason = _reason(reason, "de la reactivación")
    voided = VoidedReport.objects.select_for_update().get(pk=voided.pk)
    if voided.operation_id is None:
        raise ValidationError("La operación de ese reporte ya no existe (se re-planeó el pedido): no se puede reactivar.")
    op = Operation.objects.select_for_update().select_related("order").get(pk=voided.operation_id)
    if op.order.status not in REPORTABLE:
        raise ValidationError(f"El pedido {op.order.number} está «{op.order.get_status_display()}»: no se puede reactivar.")
    if OrderClosure.objects.filter(order_id=op.order_id).exists():
        raise ValidationError("El pedido se cerró con faltante: deshaz el cierre antes de reactivar reportes.")
    d = voided.data.get("report", {})
    good, extra = Decimal(d.get("qty_good") or 0), Decimal(d.get("qty_extra") or 0)
    scrap, leftover = Decimal(d.get("qty_scrap") or 0), Decimal(d.get("qty_leftover") or 0)
    room = op.qty_pending
    if good + leftover > room:
        raise ValidationError(f"No se puede reactivar: este reporte ocupa {_fmt(good + leftover)} unidades y a "
                              f"«{op.name}» solo le faltan {_fmt(room)} ahora mismo. Corrige lo reportado antes.")
    ean = registry.check_report(op, ean=d.get("ean") or "", qty_good=good, qty_leftover=leftover).get(
        "ean", d.get("ean") or "")
    report = ProductionReport.objects.create(
        operation=op, qty_good=good, qty_scrap=scrap, qty_extra=extra, extra_reason=d.get("extra_reason") or "",
        finished=bool(d.get("finished")), reported_by_id=d.get("reported_by_id"), comment=d.get("comment") or "",
        employee_id=d.get("employee_id"), source=d.get("source") or "", ean=ean, qty_leftover=leftover,
        edited_by=_user(user), edited_at=timezone.now(), edit_reason=f"Reactivado: {reason}"[:255])
    from datetime import datetime

    ProductionReport.objects.filter(pk=report.pk).update(reported_at=datetime.fromisoformat(d["reported_at"]))
    report.refresh_from_db()
    registry.report_restore(report, voided.data.get("plugins") or {})
    number = voided.original_id
    voided.delete()
    recompute_operation(op)
    recompute_order(op.order)
    _log(op.order, f"{op.order.number}: reactivó el reporte #{number} de «{op.name}» ({_fmt(good + extra)} buenas). "
                   f"Motivo: {reason}", user)
    return report


@transaction.atomic
def purge_voided(voided: VoidedReport, user=None) -> None:
    """Elimina del todo un reporte anulado (solo los anulados se pueden eliminar)."""
    order, number, name = voided.order, voided.original_id, voided.operation_name
    voided.delete()
    _log(order, f"{order.number}: eliminó definitivamente el reporte anulado #{number} de «{name}»", user)


_KEEP = object()


@transaction.atomic
def edit_report(report: ProductionReport, *, reason: str, user=None, qty_good=_KEEP, qty_scrap=_KEEP,
                qty_leftover=_KEEP, ean=_KEEP, employee=_KEEP, operation=_KEEP, reported_at=_KEEP, finished=_KEEP,
                comment=_KEEP) -> ProductionReport:
    """Corrige un reporte con motivo obligatorio: cantidades, sobrantes, referencia, operario, pedido/etapa (otra
    operación) y fecha-hora. Revalida que quepa en lo que falta de la operación y de la referencia."""
    from .plugins import registry

    reason = _reason(reason, "de la edición")
    report = ProductionReport.objects.select_for_update().select_related("operation__order").get(pk=report.pk)
    old_op = Operation.objects.select_for_update().select_related("order__site").get(pk=report.operation_id)
    new_op = old_op
    if operation is not _KEEP and operation is not None and operation.pk != old_op.pk:
        new_op = Operation.objects.select_for_update().select_related("order__site").get(pk=operation.pk)
        if new_op.order.status not in REPORTABLE:
            raise ValidationError(f"El pedido {new_op.order.number} no está en planta: no se le pueden pasar reportes.")
    old = {"good": report.qty_good + report.qty_extra, "scrap": report.qty_scrap, "left": report.qty_leftover,
           "ean": report.ean, "emp": report.employee_id, "at": report.reported_at, "op": old_op}
    good = old["good"] if qty_good is _KEEP else _dec(qty_good, "Buenas")
    scrap = old["scrap"] if qty_scrap is _KEEP else _dec(qty_scrap, "Merma")
    left = old["left"] if qty_leftover is _KEEP else _dec(qty_leftover, "Sobrantes")
    others = new_op.reports.exclude(pk=report.pk).aggregate(g=Sum("qty_good"), l=Sum("qty_leftover"))
    room = new_op.qty - (others["g"] or ZERO) - (others["l"] or ZERO)
    if left > max(ZERO, room):
        raise ValidationError(f"Los sobrantes ({_fmt(left)}) superan lo que falta de «{new_op.name}» ({_fmt(max(ZERO, room))}).")
    extra = max(ZERO, good - max(ZERO, room - left))
    new_ean = report.ean if ean is _KEEP else str(ean or "").strip()
    new_ean = registry.check_report(new_op, ean=new_ean, qty_good=good - extra, qty_leftover=left,
                                    exclude_report=report.pk).get("ean", new_ean)
    report.operation = new_op
    report.qty_good, report.qty_extra, report.qty_scrap, report.qty_leftover = good - extra, extra, scrap, left
    if extra and not report.extra_reason:
        report.extra_reason = "Corrección de un reporte"
    report.ean = new_ean
    if employee is not _KEEP:
        report.employee = employee
    if finished is not _KEEP:
        report.finished = bool(finished)
    if comment is not _KEEP:
        report.comment = " ".join((comment or "").split())[:255]
    delta = None
    if reported_at is not _KEEP and reported_at is not None and reported_at != report.reported_at:
        if reported_at > timezone.now():
            raise ValidationError("La fecha del reporte no puede ser posterior a ahora.")
        delta = reported_at - report.reported_at
        report.reported_at = reported_at
    report.edited_by, report.edited_at, report.edit_reason = _user(user), timezone.now(), reason
    report.save()
    if delta is not None:  # auto_now_add no deja cambiar la fecha con save()
        ProductionReport.objects.filter(pk=report.pk).update(reported_at=report.reported_at)
    registry.report_edited(report, {"old_operation": old_op, "delta": delta, "good": (old["good"], good),
                                    "scrap": (old["scrap"], scrap), "old_employee_id": old["emp"]})
    recompute_operation(old_op)
    recompute_order(old_op.order)
    if new_op.pk != old_op.pk:
        recompute_operation(new_op)
        recompute_order(new_op.order)
    parts = []
    if good != old["good"] or scrap != old["scrap"]:
        parts.append(f"{_fmt(old['good'])} buenas / {_fmt(old['scrap'])} merma → {_fmt(good)} / {_fmt(scrap)}")
    if left != old["left"]:
        parts.append(f"sobrantes {_fmt(old['left'])} → {_fmt(left)}")
    if new_ean != old["ean"]:
        parts.append(f"EAN {old['ean'] or '—'} → {new_ean or '—'}")
    if report.employee_id != old["emp"]:
        parts.append("cambió el operario")
    if new_op.pk != old_op.pk:
        parts.append(f"pasó a {new_op.order.number} «{new_op.name}»")
    if delta is not None:
        parts.append(f"fecha {timezone.localtime(old['at']):%d/%m %H:%M} → {timezone.localtime(report.reported_at):%d/%m %H:%M}")
    _log(old_op.order, f"{old_op.order.number}: editó el reporte #{report.pk} de «{old_op.name}»: "
                       f"{'; '.join(parts) or 'sin cambios de cantidades'}. Motivo: {reason}", user)
    return report


# ---------------------------------------------------------------- cierre con faltante justificado
def compliance(order: WorkOrder) -> str:
    """«Cumple» (terminado completo), «No cumple» (cerrado con faltante) o "" si aún está abierto."""
    if order.status != WorkOrder.Status.DONE:
        return ""
    try:
        closure = order.closure
    except OrderClosure.DoesNotExist:
        closure = None
    return "No cumple" if closure is not None and closure.total > 0 else "Cumple"


def shortfall_preview(order: WorkOrder) -> list[dict]:
    """Lo que quedaría justificado si se cierra ahora: por operación abierta (y por referencia, si el pedido tiene)."""
    from .plugins import registry

    out = []
    for op in order.operations.exclude(status=Operation.Status.DONE).order_by("sequence"):
        pending = op.qty_pending
        lines = registry.shortfall_lines(op, pending) if pending > 0 else []
        out.append({"op": op, "pending": pending,
                    "lines": [{"ean": e, "qty": Decimal(q)} for e, q in lines] or ([{"ean": "", "qty": pending}]
                                                                               if pending > 0 else [])})
    return out


@transaction.atomic
def close_with_shortfall(order: WorkOrder, *, reason: str, justified_by: str, user=None,
                         expected_total=None) -> OrderClosure:
    """Cierra el pedido aunque falten unidades: lo que falta de cada etapa (y referencia) queda justificado con su
    motivo, las etapas abiertas se terminan y el pedido queda «Terminado · No cumple»."""
    from . import signals

    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    reason = _reason(reason, "del faltante")
    justified_by = " ".join(str(justified_by or "").split())[:120]
    if not justified_by:
        raise ValidationError("Escribe quién justifica el faltante.")
    if order.status not in (WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS):
        raise ValidationError(f"Solo se cierra con faltante un pedido en planta (este está «{order.get_status_display()}»).")
    if OrderClosure.objects.filter(order=order).exists():
        raise ValidationError("Este pedido ya se cerró con faltante.")
    preview = shortfall_preview(order)
    total = sum((p["pending"] for p in preview), ZERO)
    if total <= 0:
        raise ValidationError("Este pedido ya no tiene faltante: lo programado está completo.")
    if expected_total is not None and Decimal(str(expected_total)) != total:
        raise ValidationError(f"El faltante cambió (ahora es {_fmt(total)}; se mostraron {_fmt(expected_total)}): "
                              "alguien reportó unidades. Revisa antes de cerrar.")
    closure = OrderClosure.objects.create(order=order, reason=reason, justified_by=justified_by, user=_user(user),
                                          total=total)
    for p in preview:
        for line in p["lines"]:
            Shortfall.objects.create(closure=closure, operation=p["op"], ean=line["ean"], qty=line["qty"])
        if not p["lines"]:  # completa pero sin cerrar: se termina sin faltante
            Shortfall.objects.create(closure=closure, operation=p["op"], ean="", qty=ZERO)
    for p in preview:
        recompute_operation(p["op"])
    recompute_order(order)
    _log(order, f"{order.number}: cerrado con faltante de {_fmt(total)} unidades. Justificó {justified_by}: {reason}",
         user)
    order.refresh_from_db()
    if order.status == WorkOrder.Status.DONE:
        signals.order_completed.send(sender=WorkOrder, order=order)
    return closure


@transaction.atomic
def undo_closure(order: WorkOrder, reason: str, user=None) -> None:
    """Deshace el cierre con faltante (p. ej. llegó la mercancía que faltaba): las etapas vuelven a quedar abiertas."""
    reason = _reason(reason, "para reabrir el pedido")
    closure = OrderClosure.objects.select_for_update().filter(order=order).first()
    if closure is None:
        return
    ops = list(Operation.objects.filter(shortfalls__closure=closure).distinct())
    closure.delete()
    for op in ops:
        recompute_operation(op)
    recompute_order(order)
    _log(order, f"{order.number}: deshizo el cierre con faltante. Motivo: {reason}", user)
