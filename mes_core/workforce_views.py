"""Pantallas de la bolsa global de personal: capacidad global, reparto por bodega y quién está dónde."""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from . import workcal, workforce
from .capacity import fmt_h
from .models import Shift, Site, StaffAllocation, WorkCenter
from .scheduling import site_shifts


def _int_or_none(raw):
    raw = (raw or "").strip()
    if raw == "":
        return None
    try:
        v = int(raw)
    except ValueError:
        raise ValidationError(f"«{raw}» no es un número entero.") from None
    if v < 0:
        raise ValidationError("Las personas no pueden ser negativas.")
    return v


def _local_dt(raw):
    dt = parse_datetime(raw or "")
    if dt is None:
        return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


# ---------------------------------------------------------------- capacidad global
@login_required
@permission_required("mes_core.view_staffallocation", raise_exception=True)
def global_capacity(request):
    today = timezone.localdate()
    start = parse_date(request.GET.get("semana") or "") or today
    start -= timedelta(days=start.weekday())
    data = workforce.global_capacity(start, 7)
    today_tot = next((t for t in data["totals"] if t["day"] == today), data["totals"][0])
    return render(request, "mes_core/global_capacity.html", {
        **data, "start": start, "prev": start - timedelta(days=7), "next": start + timedelta(days=7),
        "today": today, "today_tot": today_tot, "fmt_week_cap": fmt_h(data["week_cap"]),
        "fmt_week_req": fmt_h(data["week_req"]),
    })


# ---------------------------------------------------------------- reparto por bodega
@login_required
@permission_required("mes_core.view_staffallocation", raise_exception=True)
def staff_split(request):
    can_edit = request.user.has_perm("mes_core.change_staffallocation")
    day = parse_date(request.GET.get("dia") or request.POST.get("dia") or "") or timezone.localdate()
    back = f"{reverse('mes_core:staff_split')}?dia={day.isoformat()}"
    if request.method == "POST":
        if not can_edit:
            messages.error(request, "No tienes permiso para cambiar el reparto.")
            return redirect(back)
        action = request.POST.get("action")
        try:
            if action == "base":
                values = {}
                for key, raw in request.POST.items():
                    if not key.startswith("b_"):
                        continue
                    _, site_id, shift_id = key.split("_")
                    values[(int(site_id), int(shift_id) or None)] = _int_or_none(raw)
                n = workforce.save_base_split(values, request.user)
                messages.success(request, f"Reparto base guardado ({n} cambio{'s' if n != 1 else ''}).")
            elif action == "add":
                site = get_object_or_404(Site, pk=request.POST.get("site"))
                shift = None
                if request.POST.get("shift"):
                    shift = get_object_or_404(Shift, pk=request.POST.get("shift"), site=site)
                d0 = parse_date(request.POST.get("date_from") or "")
                d1 = parse_date(request.POST.get("date_to") or "") or d0
                n = _int_or_none(request.POST.get("headcount"))
                if d0 is None or n is None:
                    raise ValidationError("Indica la fecha y las personas.")
                a = StaffAllocation(site=site, shift=shift, date_from=d0, date_to=d1, headcount=n,
                                    note=(request.POST.get("note") or "")[:200], created_by=request.user)
                a.full_clean()
                a.save()
                messages.success(request, f"Cambio guardado: {a}.")
            elif action == "del":
                a = get_object_or_404(StaffAllocation, pk=request.POST.get("pk"), date_from__isnull=False)
                a.delete()
                messages.success(request, "Cambio por fecha eliminado.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        workcal.invalidate()
        return redirect(back)

    sites = list(Site.objects.filter(active=True).order_by("code"))
    base = {(a.site_id, a.shift_id): a for a in StaffAllocation.objects.filter(date_from__isnull=True)}
    grid = []
    for s in sites:
        shifts = site_shifts(s)
        grid.append({"site": s, "rows": [{"shift": sh, "key": f"b_{s.pk}_{sh.pk}", "alloc": base.get((s.pk, sh.pk)),
                                          "fallback": sh.operators or s.operators_total or None} for sh in shifts]
                                        or [{"shift": None, "key": f"b_{s.pk}_0", "alloc": base.get((s.pk, None)),
                                             "fallback": s.operators_total or None}],
                     "site_wide": base.get((s.pk, None)) if shifts else None})
    changes = (StaffAllocation.objects.filter(date_from__isnull=False, date_to__gte=timezone.localdate() - timedelta(days=30))
               .select_related("site", "shift", "created_by").order_by("date_from", "site__code"))
    split = workforce.day_split(day, sites)
    return render(request, "mes_core/staff_split.html", {
        "grid": grid, "changes": changes, "split": split, "day": day, "can_edit": can_edit,
        "pool": split["pool"], "sites": sites,
        "shifts_by_site": {s.pk: [(sh.pk, sh.name) for sh in site_shifts(s)] for s in sites},
        "prev_day": day - timedelta(days=1), "next_day": day + timedelta(days=1),
    })


# ---------------------------------------------------------------- quién está dónde
@login_required
@permission_required("mes_core.view_staffmove", raise_exception=True)
def staff_location(request):
    can_move = request.user.has_perm("mes_core.add_staffmove")
    now = timezone.localtime().replace(second=0, microsecond=0)
    at = _local_dt(request.GET.get("en") or request.POST.get("en")) or now
    q = request.GET.get("q", "")
    back = f"{reverse('mes_core:staff_location')}?en={timezone.localtime(at):%Y-%m-%dT%H:%M}" + (f"&q={q}" if q else "")
    if request.method == "POST":
        if not can_move:
            messages.error(request, "No tienes permiso para mover personal.")
            return redirect(back)
        ids = [int(x) for x in request.POST.getlist("emp") if x.isdigit()]
        try:
            if request.POST.get("action") == "back":
                n = workforce.send_back(ids, at)
                messages.success(request, f"{n} traslado(s) terminados: vuelven a donde estaban.")
            else:
                site = get_object_or_404(Site, pk=request.POST.get("site"))
                wc = None
                if request.POST.get("line"):
                    wc = get_object_or_404(WorkCenter, pk=request.POST.get("line"))
                start = _local_dt(request.POST.get("start")) or at
                end = None if request.POST.get("until") == "nuevo" else _local_dt(request.POST.get("end"))
                if request.POST.get("until") != "nuevo" and end is None:
                    raise ValidationError("Indica hasta cuándo, o elige «hasta nuevo aviso».")
                n = workforce.move(ids, site, start, end, wc, request.POST.get("note") or "", request.user)
                where = f"{site.code}" + (f" · {wc.code}" if wc else "")
                messages.success(request, f"{n} persona(s) a {where} desde {timezone.localtime(start):%d/%m %H:%M}"
                                          + (f" hasta {timezone.localtime(end):%d/%m %H:%M}." if end else
                                             " (hasta nuevo aviso)."))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect(back)

    data = workforce.location_board(at, q)
    lines = [{"id": wc.pk, "site": wc.site_id, "label": f"{wc.code} · {wc.name}"}
             for wc in WorkCenter.objects.filter(active=True, site__active=True).order_by("code")]
    return render(request, "mes_core/staff_location.html", {
        **data, "can_move": can_move, "at_value": f"{timezone.localtime(at):%Y-%m-%dT%H:%M}",
        "is_now": abs((at - now).total_seconds()) < 60, "lines_json": lines,
        "sites": Site.objects.filter(active=True).order_by("code"),
        "end_default": f"{timezone.localtime(at) + timedelta(hours=4):%Y-%m-%dT%H:%M}",
    })
