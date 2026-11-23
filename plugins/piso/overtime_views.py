"""MES · Horas extra: planeación arma la convocatoria, la envía a las tablets y ve las respuestas."""

from __future__ import annotations

from datetime import datetime
from functools import wraps
from io import BytesIO

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from mes_core import site_scope
from mes_core.models import Shift, Site

from . import overtime
from .models import OvertimeCall, OvertimeRequest

VIEW_PERMS = ("mes_core.plan_workorder", "mes_core.approve_plan", "mes_core.supervise_crew", "mes_core.change_shift")
EDIT_PERMS = ("mes_core.plan_workorder", "mes_core.approve_plan", "mes_core.change_shift")


def _can(user, perms) -> bool:
    return user.is_superuser or any(user.has_perm(p) for p in perms)


def can_view(user) -> bool:
    return _can(user, VIEW_PERMS)


def can_edit(user) -> bool:
    return _can(user, EDIT_PERMS)


def viewer_required(fn):
    @wraps(fn)
    @login_required
    def inner(request, *args, **kwargs):
        if not can_view(request.user):
            raise PermissionDenied
        return fn(request, *args, **kwargs)
    return inner


@viewer_required
def overtime_list(request):
    scope = site_scope.pick(request, allow_all=True)
    site, sites = scope.site, scope.sites
    calls = OvertimeCall.objects.all()
    if site is not None:  # las de esa bodega y las que van para todas
        calls = calls.filter(pk__in=OvertimeCall.objects.filter(
            Q(site=site) | Q(sites=site) | Q(site__isnull=True, sites__isnull=True)).values("pk"))
    calls = (calls
             .annotate(n=Count("requests", distinct=True),
                       yes=Count("requests", filter=Q(requests__answer=OvertimeRequest.Answer.YES), distinct=True),
                       no=Count("requests", filter=Q(requests__answer=OvertimeRequest.Answer.NO), distinct=True),
                       pend=Count("requests", filter=Q(requests__answer=OvertimeRequest.Answer.PENDING), distinct=True))
             .prefetch_related("days", "sites", "shifts").order_by("-created_at")[:100])
    for c in calls:
        c.target = overtime.target_label(c)
    return render(request, "piso/overtime_list.html", {"site": site, "sites": sites, "site_allow_all": True,
                                                       "calls": calls, "can_edit": can_edit(request.user)})


def _parse_days(post) -> list[overtime.DaySpec]:
    out = []
    rows = zip(post.getlist("d_date"), post.getlist("d_start"), post.getlist("d_end"), post.getlist("d_needed"))
    for i, (d, s, e, n) in enumerate(rows, start=1):
        if not (d or s or e):
            continue
        try:
            day = datetime.strptime(d, "%Y-%m-%d").date()
            start = datetime.strptime(s[:5], "%H:%M").time()
            end = datetime.strptime(e[:5], "%H:%M").time()
        except ValueError:
            raise ValidationError(f"Revisa la fila {i}: fecha (aaaa-mm-dd) y horas (HH:MM).") from None
        out.append(overtime.DaySpec(day, start, end, int(n) if str(n).strip().isdigit() else 0))
    return out


@viewer_required
def overtime_edit(request, pk=None):
    if not can_edit(request.user):
        raise PermissionDenied
    call = get_object_or_404(OvertimeCall, pk=pk) if pk else None
    if call and call.status != OvertimeCall.Status.DRAFT:
        messages.info(request, "Esta convocatoria ya se envió: no se puede editar.")
        return redirect("piso:overtime_detail", pk=call.pk)
    all_sites = list(Site.objects.filter(active=True).order_by("code"))
    error = None
    if request.method == "POST":
        try:
            respond_by = None
            if request.POST.get("respond_by"):
                respond_by = timezone.make_aware(datetime.strptime(request.POST["respond_by"][:16], "%Y-%m-%dT%H:%M"))
            picked = [s for s in all_sites if str(s.pk) in request.POST.getlist("sites")]
            shifts = Shift.objects.filter(active=True, pk__in=[x for x in request.POST.getlist("shifts") if x.isdigit()])
            if request.POST.get("send") == "1" and not overtime.people([s.pk for s in picked],
                                                                       [s.pk for s in shifts]).exists():
                raise ValidationError("Con esas bodegas y turnos no le llega a nadie: elige «Todas» o cambia la selección.")
            call = overtime.save_call(call, sites=picked, title=request.POST.get("title", ""),
                                      note=request.POST.get("note", ""), shifts=shifts, respond_by=respond_by,
                                      days=_parse_days(request.POST), user=request.user)
            if request.POST.get("send") == "1":
                n = overtime.send(call)
                messages.success(request, f"Convocatoria enviada a {n} persona(s): ya les aparece en la tablet.")
            else:
                messages.success(request, "Convocatoria guardada como borrador.")
            return redirect("piso:overtime_detail", pk=call.pk)
        except ValidationError as exc:
            error = " ".join(exc.messages)
    days = ([{"date": d.date.isoformat(), "start": f"{d.start:%H:%M}", "end": f"{d.end:%H:%M}", "needed": d.needed}
             for d in call.days.all()] if call else [])
    if request.method == "POST" and error:
        days = [{"date": d, "start": s, "end": e, "needed": n} for d, s, e, n in zip(
            request.POST.getlist("d_date"), request.POST.getlist("d_start"), request.POST.getlist("d_end"),
            request.POST.getlist("d_needed"))]
    if request.method == "POST":
        chosen = {int(x) for x in request.POST.getlist("shifts") if x.isdigit()}
        chosen_sites = {int(x) for x in request.POST.getlist("sites") if x.isdigit()}
    else:
        chosen = set(call.shifts.values_list("pk", flat=True)) if call else set()
        chosen_sites = set(call.sites.values_list("pk", flat=True)) if call else set()
        if not call and request.GET.get("site", "").isdigit():  # «Nueva» desde la lista filtrada por una bodega
            chosen_sites = {int(request.GET["site"])}
    pmap = overtime.people_map()
    site_counts = {s.pk: sum(1 for e, _, ss in pmap if s.pk in (e, ss)) for s in all_sites}
    shift_counts = {}
    for _, sh, _ in pmap:
        shift_counts[sh] = shift_counts.get(sh, 0) + 1
    return render(request, "piso/overtime_form.html", {
        "call": call, "error": error, "days": days, "post": request.POST if request.method == "POST" else None,
        "site_rows": [(s, site_counts[s.pk]) for s in all_sites], "chosen_sites": chosen_sites,
        "shift_rows": [(s, shift_counts.get(s.pk, 0)) for s in overtime.shifts_of()], "chosen": chosen,
        "audience_all": len(pmap), "people_map": pmap,
    })


@viewer_required
def overtime_detail(request, pk):
    call = get_object_or_404(OvertimeCall.objects.select_related("site", "created_by"), pk=pk)
    if request.method == "POST":
        if not can_edit(request.user):
            raise PermissionDenied
        action = request.POST.get("action")
        try:
            if action == "send":
                n = overtime.send(call)
                messages.success(request, f"Enviada a {n} persona(s) más." if n else "No hay personas nuevas por convocar.")
            elif action == "close":
                overtime.close(call)
                messages.success(request, "Convocatoria cerrada: ya no recibe respuestas ni bloquea las tablets.")
            elif action == "delete" and call.status == OvertimeCall.Status.DRAFT:
                call.delete()
                messages.success(request, "Borrador eliminado.")
                return redirect(reverse("piso:overtime"))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("piso:overtime_detail", pk=call.pk)
    s = overtime.summary(call)
    have = set(call.requests.values_list("employee_id", flat=True))
    new_people = (sum(1 for pk in overtime.audience(call).values_list("pk", flat=True) if pk not in have)
                  if call.status == OvertimeCall.Status.SENT else 0)
    return render(request, "piso/overtime_detail.html", {
        "call": call, "s": s, "can_edit": can_edit(request.user), "shifts": list(call.shifts.all()),
        "new_people": new_people, "target": overtime.target_label(call),
    })


@viewer_required
def overtime_xlsx(request, pk):
    """Para planear: quién va cada día y hasta qué hora, y las respuestas de todos."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    call = get_object_or_404(OvertimeCall, pk=pk)
    s = overtime.summary(call)
    wb = Workbook()
    ws = wb.active
    ws.title = "Por día"
    ws.append(["Fecha", "Día", "Tipo", "Desde", "Hasta (franja)", "Personas necesarias", "Persona", "Documento",
               "Turno", "Hasta qué hora puede", "Horas"])
    for row in s["days"]:
        d = row["day"]
        base = [d.date, row["label"].split(" ")[0], d.kind, d.start.strftime("%H:%M"), d.end.strftime("%H:%M"),
                d.needed or ""]
        if not row["people"]:
            ws.append(base + ["(nadie)", "", "", "", 0])
        for emp, until, h in row["people"]:
            ws.append(base + [emp.display_name, emp.document, emp.shift.name if emp.shift_id else "",
                              until.strftime("%H:%M"), h])
    ws2 = wb.create_sheet("Respuestas")
    ws2.append(["Persona", "Documento", "Turno", "Respuesta", "Motivo", "Días elegidos", "Respondió"])
    for r in s["yes"] + s["no"] + s["pending"]:
        days = ", ".join(f"{overtime.day_label(a.day.date)} hasta {a.until:%H:%M}" for a in r.answers.all())
        ws2.append([r.employee.display_name, r.employee.document, r.employee.shift.name if r.employee.shift_id else "",
                    r.get_answer_display(), r.reason, days,
                    timezone.localtime(r.answered_at).strftime("%d/%m/%Y %H:%M") if r.answered_at else ""])
    for sheet in (ws, ws2):
        for c in sheet[1]:
            c.font = Font(bold=True)
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = max(10, min(40, max(len(str(c.value or "")) for c in col) + 2))
        sheet.freeze_panes = "A2"
    buf = BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="horas-extra-{call.pk}.xlsx"'
    return resp
