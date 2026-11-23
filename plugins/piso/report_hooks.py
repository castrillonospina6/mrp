"""Las marcaciones de piso siguen a su reporte de producción cuando en el MES se anula, se reactiva o se corrige: así la
eficiencia, los puntos y el histórico de la tablet cuadran siempre con lo que quedó en el pedido."""

from datetime import datetime
from decimal import Decimal

from .live import bump
from .models import IntervalReport, Mark
from .reporting import split_quantity


def _iso(dt):
    return dt.isoformat() if dt else None


def _dt(text):
    return datetime.fromisoformat(text) if text else None


def snapshot(report) -> dict:
    marks = [{"wc": m.work_center_id, "emp": m.employee_id, "kind": m.kind, "qty": str(m.qty),
              "mpu": str(m.minutes_per_unit), "at": _iso(m.at), "since": _iso(m.since), "interval": m.interval_id}
             for m in Mark.objects.filter(report=report)]
    intervals = [{"id": i.pk, "wc": i.work_center_id, "emp": i.employee_id, "kind": i.kind, "split": i.split_mode,
                  "start": _iso(i.period_start), "end": _iso(i.period_end), "good": str(i.qty_good),
                  "scrap": str(i.qty_scrap), "people": i.people}
                 for i in IntervalReport.objects.filter(report=report)]
    return {"marks": marks, "intervals": intervals} if marks or intervals else {}


def restore(report, data: dict) -> None:
    if not data:
        return
    op = report.operation
    new_ids = {}
    for i in data.get("intervals") or []:
        ir = IntervalReport.objects.create(
            operation=op, work_center_id=i["wc"], employee_id=i["emp"], kind=i["kind"], split_mode=i["split"] or "",
            period_start=_dt(i["start"]), period_end=_dt(i["end"]), qty_good=Decimal(i["good"]),
            qty_scrap=Decimal(i["scrap"]), people=i["people"] or 1, report=report)
        new_ids[i["id"]] = ir.pk
    Mark.objects.bulk_create([
        Mark(operation=op, work_center_id=m["wc"], employee_id=m["emp"], kind=m["kind"], qty=Decimal(m["qty"]),
             minutes_per_unit=Decimal(m["mpu"]), at=_dt(m["at"]), since=_dt(m["since"]), report=report,
             interval_id=new_ids.get(m["interval"])) for m in data.get("marks") or []])
    bump("g", "b")


def _rescale(report, kind, old, new) -> None:
    ms = list(Mark.objects.filter(report=report, kind=kind))
    if old == new:
        return
    if new <= 0:
        Mark.objects.filter(pk__in=[m.pk for m in ms]).delete()
        return
    if not ms:
        op = report.operation
        Mark.objects.create(operation=op, work_center=op.work_center, employee=report.employee, kind=kind, qty=new,
                            minutes_per_unit=op.minutes_per_unit, at=report.reported_at, report=report)
        return
    shares = split_quantity(Decimal(new), {m.pk: float(m.qty) for m in ms})
    drop = []
    for m in ms:
        q = shares.get(m.pk, Decimal(0))
        if q <= 0:
            drop.append(m.pk)
        elif q != m.qty:
            m.qty = q
            m.save(update_fields=["qty"])
    if drop:
        Mark.objects.filter(pk__in=drop).delete()


def edited(report, change: dict) -> None:
    op = report.operation
    marks = Mark.objects.filter(report=report)
    intervals = IntervalReport.objects.filter(report=report)
    if change["old_operation"].pk != op.pk:
        marks.update(operation=op, minutes_per_unit=op.minutes_per_unit)
        intervals.update(operation=op)
    delta = change.get("delta")
    if delta:
        for m in marks:
            m.at += delta
            if m.since:
                m.since += delta
            m.save(update_fields=["at", "since"])
        for i in intervals:
            i.period_start += delta
            i.period_end += delta
            i.save(update_fields=["period_start", "period_end"])
    old_emp = change.get("old_employee_id")
    if old_emp != report.employee_id:
        marks.filter(employee_id=old_emp).update(employee=report.employee)
        intervals.filter(employee_id=old_emp, kind=IntervalReport.Kind.OWN).update(employee=report.employee)
    (og, ng), (ox, nx) = change["good"], change["scrap"]
    _rescale(report, Mark.Kind.GOOD, Decimal(og), Decimal(ng))
    _rescale(report, Mark.Kind.SCRAP, Decimal(ox), Decimal(nx))
    if og != ng or ox != nx:
        intervals.update(qty_good=ng, qty_scrap=nx)
    bump("g", "b")
