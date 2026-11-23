"""Horas extra: convocatorias de planeación y respuestas del personal en la tablet.

Planeación arma en el MES una convocatoria con los días habilitados (entre semana después del turno, sábados,
domingos y festivos), cada uno con su franja. Al enviarla, a cada persona convocada le aparece en la tablet una
pantalla que no la deja seguir hasta que responda:

* NO → escribe el motivo.
* SÍ → elige los días que puede (solo los habilitados) y, para cada uno, hasta qué hora.

En el MES se ve cuántos respondieron, cuántos faltan, cuántos SÍ y cuántos NO, y por día quiénes van y hasta qué hora
(para planear fines de semana, festivos y extras entre semana). En la tablet, la pestaña «Extras» muestra los días y
quién va a cada uno.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from mes_core.models import CalendarException, Employee, Shift, Site

from .live import bump
from .models import OvertimeAnswer, OvertimeCall, OvertimeDay, OvertimeRequest

WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
BLOCKED_MSG = "Primero responde la convocatoria de horas extra."
REASONS = ("Compromiso familiar", "Estudio", "Salud / cita médica", "Transporte", "Otro trabajo", "Otro")


@dataclass
class DaySpec:
    date: date
    start: time
    end: time
    needed: int = 0


def _key(emp_id: int) -> str:
    return f"piso:ot:{emp_id}"


def _forget(emp_ids) -> None:
    cache.delete_many([_key(i) for i in emp_ids])


def is_holiday(site_ids, d: date) -> bool:
    """¿Es festivo en el calendario laboral de alguna de esas bodegas (o de cualquiera, si son todas)?"""
    qs = CalendarException.objects.filter(kind=CalendarException.Kind.HOLIDAY)
    if isinstance(site_ids, Site):
        site_ids = [site_ids.pk]
    if site_ids:
        qs = qs.filter(site_id__in=list(site_ids))
    return qs.filter(Q(date=d) | Q(date__lte=d, date_to__gte=d)).exists()


def day_label(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()].capitalize()} {d:%d/%m}"


def hours(start: time, end: time) -> float:
    a = datetime.combine(date.min, start)
    b = datetime.combine(date.min, end)
    if b <= a:
        b += timedelta(days=1)
    return round((b - a).total_seconds() / 3600, 2)


# ---------------------------------------------------------------- MES: armar y enviar
def audience(call: OvertimeCall):
    """Personas convocadas: activas y con acceso a la tablet. Sin bodegas ni turnos elegidos = TODO el personal de
    todas las bodegas (envío masivo); con bodegas, solo su gente; con turnos, solo la de esos turnos."""
    if not call.pk:
        return people()
    return people(list(call.sites.values_list("pk", flat=True)), list(call.shifts.values_list("pk", flat=True)))


def people(site_ids=(), shift_ids=()):
    qs = Employee.objects.filter(active=True, floor_access__active=True)
    if site_ids:  # la bodega de la persona (o la de su turno)
        qs = qs.filter(Q(site_id__in=site_ids) | Q(shift__site_id__in=site_ids))
    if shift_ids:
        qs = qs.filter(shift_id__in=shift_ids)
    return qs.distinct().order_by("full_name")


def people_map() -> list[list[int]]:
    """Para contar en vivo en el formulario a cuántos les llega: [bodega, turno, bodega del turno] por persona."""
    return [[s or 0, sh or 0, ss or 0] for s, sh, ss in
            Employee.objects.filter(active=True, floor_access__active=True)
            .values_list("site_id", "shift_id", "shift__site_id")]


def target_label(call: OvertimeCall) -> str:
    sites = [s.name for s in call.sites.all()]
    shifts = [s.name for s in call.shifts.all()]
    out = ", ".join(sites) if sites else "Todas las bodegas"
    return out + (f" · turnos {', '.join(shifts)}" if shifts else "")


@transaction.atomic
def save_call(call: OvertimeCall | None, *, sites=(), title: str, note: str, shifts, respond_by, days: list[DaySpec],
              user=None, site: Site | None = None) -> OvertimeCall:
    title = " ".join((title or "").split())[:120]
    if not title:
        raise ValidationError("Ponle un nombre a la convocatoria (p. ej. «Extras semana 48»).")
    if not days:
        raise ValidationError("Agrega al menos un día habilitado para horas extra.")
    seen = set()
    today = timezone.localdate()
    for d in days:
        if d.date in seen:
            raise ValidationError(f"El {day_label(d.date)} está dos veces.")
        seen.add(d.date)
        if d.date < today:
            raise ValidationError(f"El {day_label(d.date)} ya pasó.")
        if d.start == d.end:
            raise ValidationError(f"La franja del {day_label(d.date)} no dura nada.")
    if call is not None and call.status != OvertimeCall.Status.DRAFT:
        raise ValidationError("Solo se edita una convocatoria en borrador (esta ya se envió).")
    sites = list(sites or ([site] if site else []))
    call = call or OvertimeCall(created_by=user if getattr(user, "is_authenticated", False) else None)
    call.site = sites[0] if len(sites) == 1 else None  # None = varias o todas las bodegas
    call.title, call.note, call.respond_by = title, (note or "").strip()[:2000], respond_by
    call.save()
    call.sites.set(sites)
    call.shifts.set(shifts or [])
    call.days.all().delete()
    ids = [s.pk for s in sites]
    OvertimeDay.objects.bulk_create([OvertimeDay(call=call, date=d.date, start=d.start, end=d.end, needed=d.needed or 0,
                                                 holiday=is_holiday(ids, d.date)) for d in sorted(days, key=lambda x: x.date)])
    return call


@transaction.atomic
def send(call: OvertimeCall) -> int:
    """Envía la convocatoria (o la manda a quienes se sumaron después): a cada persona le aparece en la tablet."""
    if call.status == OvertimeCall.Status.CLOSED:
        raise ValidationError("La convocatoria está cerrada.")
    if not call.days.exists():
        raise ValidationError("La convocatoria no tiene días.")
    have = set(call.requests.values_list("employee_id", flat=True))
    new = [e for e in audience(call) if e.pk not in have]
    OvertimeRequest.objects.bulk_create([OvertimeRequest(call=call, employee=e) for e in new])
    if call.status == OvertimeCall.Status.DRAFT:
        call.status, call.sent_at = OvertimeCall.Status.SENT, timezone.now()
        call.save(update_fields=["status", "sent_at"])
    ids = [e.pk for e in new]
    _forget(ids)
    for i in range(0, len(ids), 300):  # cada tablet convocada se entera al instante (en su próxima consulta, ~1,5 s)
        chunk = ids[i:i + 300]
        transaction.on_commit(lambda c=chunk: bump(*[f"e{x}" for x in c]))
    return len(new)


@transaction.atomic
def close(call: OvertimeCall) -> None:
    call.status, call.closed_at = OvertimeCall.Status.CLOSED, timezone.now()
    call.save(update_fields=["status", "closed_at"])
    ids = list(call.requests.values_list("employee_id", flat=True))
    _forget(ids)
    for i in range(0, len(ids), 300):
        chunk = ids[i:i + 300]
        transaction.on_commit(lambda c=chunk: bump(*[f"e{x}" for x in c]))


# ---------------------------------------------------------------- tablet
def _open_calls(today: date):
    return OvertimeCall.objects.filter(status=OvertimeCall.Status.SENT, days__date__gte=today).distinct()


def _can_answer(call: OvertimeCall, now) -> bool:
    return call.status == OvertimeCall.Status.SENT and (call.respond_by is None or now <= call.respond_by)


def pending_for(emp: Employee, now=None) -> OvertimeRequest | None:
    """La convocatoria que la persona aún no responde (bloquea la tablet hasta que responda)."""
    now = now or timezone.now()
    cached = cache.get(_key(emp.pk))
    if cached == 0:
        return None
    req = (OvertimeRequest.objects.filter(employee=emp, answer=OvertimeRequest.Answer.PENDING,
                                          call__in=_open_calls(timezone.localdate(now)))
           .select_related("call").order_by("call__sent_at").first())
    if req is not None and not _can_answer(req.call, now):
        req = None
    cache.set(_key(emp.pk), 1 if req else 0, 120)
    return req


def _parse_time(raw) -> time | None:
    try:
        return datetime.strptime(str(raw or "")[:5], "%H:%M").time()
    except ValueError:
        return None


def _within(day: OvertimeDay, t: time) -> bool:
    """¿La hora cae en la franja del día (también si la franja pasa la medianoche)?"""
    s, e = day.start, day.end
    return (s < t <= e) if s < e else (t > s or t <= e)


@transaction.atomic
def answer(req: OvertimeRequest, *, yes: bool, reason: str = "", picks=None, now=None) -> OvertimeRequest:
    now = now or timezone.now()
    req = OvertimeRequest.objects.select_for_update().select_related("call").get(pk=req.pk)
    if not _can_answer(req.call, now):
        raise ValidationError("Esta convocatoria ya no recibe respuestas.")
    if yes:
        days = {d.pk: d for d in req.call.days.filter(date__gte=timezone.localdate(now))}
        chosen = []
        for p in picks or []:
            try:
                day = days[int(p.get("d"))]
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ValidationError("Uno de los días no está habilitado.") from None
            until = _parse_time(p.get("h")) or day.end
            if not _within(day, until):
                raise ValidationError(f"El {day_label(day.date)} la franja es de {day.start:%H:%M} a {day.end:%H:%M}: "
                                      f"elige una hora dentro de ella.")
            chosen.append(OvertimeAnswer(request=req, day=day, until=until))
        if not chosen:
            raise ValidationError("Elige al menos un día en que puedas hacer horas extra.")
        req.answers.all().delete()
        OvertimeAnswer.objects.bulk_create(chosen)
        req.answer, req.reason = OvertimeRequest.Answer.YES, ""
    else:
        reason = " ".join((reason or "").split())[:255]
        if len(reason) < 3:
            raise ValidationError("Escribe el motivo por el que no puedes.")
        req.answers.all().delete()
        req.answer, req.reason = OvertimeRequest.Answer.NO, reason
    req.answered_at = now
    req.save(update_fields=["answer", "reason", "answered_at"])
    _forget([req.employee_id])
    transaction.on_commit(lambda: bump(f"e{req.employee_id}"))  # los demás ven quién va en su próxima recarga
    return req


def _call_json(call: OvertimeCall, mine: OvertimeRequest | None, now, today: date) -> dict:
    my_until = {a.day_id: a.until for a in mine.answers.all()} if mine else {}
    days = []
    for d in call.days.all():
        if d.date < today:
            continue
        going = sorted(((a.request.employee.display_name, a.until) for a in d.answers.all()
                        if a.request.answer == OvertimeRequest.Answer.YES), key=lambda x: x[0])
        days.append({"id": d.pk, "date": d.date.isoformat(), "label": day_label(d.date), "kind": d.kind,
                     "start": f"{d.start:%H:%M}", "end": f"{d.end:%H:%M}", "needed": d.needed,
                     "going": [{"name": n, "until": f"{u:%H:%M}"} for n, u in going], "count": len(going),
                     "mine": f"{my_until[d.pk]:%H:%M}" if d.pk in my_until else None})
    return {"id": call.pk, "req": mine.pk if mine else None, "title": call.title, "note": call.note,
            "respond_by": call.respond_by.isoformat() if call.respond_by else None, "days": days,
            "answer": mine.answer if mine else None, "reason": mine.reason if mine else "",
            "can_change": bool(mine) and _can_answer(call, now)}


def tablet_state(emp: Employee, now=None) -> dict:
    """Para la tablet: la convocatoria por responder (si hay) y las vigentes con quién va cada día."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    reqs = list(OvertimeRequest.objects.filter(employee=emp, call__in=_open_calls(today))
                .select_related("call").prefetch_related("answers", "call__days__answers__request__employee")
                .order_by("call__sent_at"))
    calls = [_call_json(r.call, r, now, today) for r in reqs]
    pend = next((c for c, r in zip(calls, reqs) if r.answer == OvertimeRequest.Answer.PENDING and c["can_change"]), None)
    return {"pending": pend, "calls": calls, "reasons": list(REASONS)}


# ---------------------------------------------------------------- MES: resultados
def summary(call: OvertimeCall) -> dict:
    reqs = list(call.requests.select_related("employee__shift").prefetch_related("answers__day")
                .order_by("employee__full_name"))
    yes = [r for r in reqs if r.answer == OvertimeRequest.Answer.YES]
    no = [r for r in reqs if r.answer == OvertimeRequest.Answer.NO]
    pending = [r for r in reqs if r.answer == OvertimeRequest.Answer.PENDING]
    days = []
    for d in call.days.prefetch_related("answers__request__employee"):
        people = sorted(((a.request.employee, a.until, hours(d.start, a.until)) for a in d.answers.all()
                         if a.request.answer == OvertimeRequest.Answer.YES), key=lambda x: x[0].full_name)
        total = round(sum(h for _, _, h in people), 2)
        days.append({"day": d, "label": day_label(d.date), "people": people, "count": len(people),
                     "hours": total, "window_hours": hours(d.start, d.end),
                     "missing": max(0, d.needed - len(people)) if d.needed else None})
    by_kind: dict[str, dict] = {}
    for row in days:
        k = by_kind.setdefault(row["day"].kind, {"kind": row["day"].kind, "days": 0, "people": 0, "hours": 0.0})
        k["days"] += 1
        k["people"] += row["count"]
        k["hours"] = round(k["hours"] + row["hours"], 2)
    return {"total": len(reqs), "answered": len(yes) + len(no), "pending": pending, "yes": yes, "no": no,
            "days": days, "by_kind": list(by_kind.values()),
            "pct": round((len(yes) + len(no)) * 100 / len(reqs)) if reqs else 0}


def shifts_of(site: Site | None = None):
    qs = Shift.objects.filter(active=True).select_related("site")
    if site is not None:
        qs = qs.filter(site=site)
    return qs.order_by("site__code", "start_time")
