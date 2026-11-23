"""Pantallas de planeación avanzada: calendario laboral, reglas, simulación, horas extra sugeridas,
mediano plazo y pronóstico, versiones del plan con aprobación, plan frente a lo real y promesa de entrega."""

from __future__ import annotations

import calendar as pycal
import json
from datetime import date, datetime, time, timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.views.decorators.http import require_POST

from . import horizon, planning, service_types, simulation, site_scope, versions, workcal
from .models import (
    CalendarException, ChangeoverTime, Operation, PlanningRules, PlanVersion, Product, ScheduleChange, Shift, Site,
    WorkCenter, WorkOrder,
)
from .plugins import registry
from .scheduling import site_shifts

PLAN = "mes_core.plan_workorder"
APPROVE = "mes_core.approve_plan"
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]
DOW = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


def _err(exc) -> str:
    return "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)


def needs(*perms):
    def deco(view):
        @wraps(view)
        @login_required
        def wrapper(request, *args, **kwargs):
            if not any(request.user.has_perm(p) for p in perms):
                messages.error(request, "No tienes permiso para esta pantalla. Pídelo a un administrador.")
                return redirect("mes_core:dashboard")
            return view(request, *args, **kwargs)
        return wrapper
    return deco


def _site(request):
    return site_scope.site_and_sites(request)


def _log(site, action, text, user):
    ScheduleChange.objects.create(site=site, action=action, description=text[:255],
                                  user=user if user and user.is_authenticated else None)


# --- calendario laboral -----------------------------------------------------------------------

def _exception_from_post(site, data) -> CalendarException:
    kind = data.get("kind")
    if kind not in CalendarException.Kind.values:
        raise ValidationError("Elige el tipo de excepción.")
    d = parse_date(data.get("date") or "")
    if not d:
        raise ValidationError("Indica la fecha.")
    exc = CalendarException(site=site, kind=kind, date=d, date_to=parse_date(data.get("date_to") or "") or None,
                            name=(data.get("name") or "").strip()[:120])
    if kind in (CalendarException.Kind.OVERTIME, CalendarException.Kind.CLOSURE) and data.get("shift"):
        exc.shift = get_object_or_404(Shift, pk=data["shift"], site=site)
    if kind in (CalendarException.Kind.OVERTIME, CalendarException.Kind.EXTRA_SHIFT):
        exc.start_time = parse_time(data.get("start_time") or "")
        exc.end_time = parse_time(data.get("end_time") or "")
        try:
            exc.operators = max(0, int(data.get("operators") or 0))
        except ValueError:
            raise ValidationError("Los operarios deben ser un número entero.")
    if exc.date_to == exc.date:
        exc.date_to = None
    exc.full_clean(exclude=["work_centers"])
    return exc


@needs(PLAN, "mes_core.view_calendarexception")
def work_calendar(request):
    site, sites = _site(request)
    if site is None:
        return render(request, "mes_core/work_calendar.html", {"no_site": True})
    can_edit = request.user.has_perm(PLAN)
    if request.method == "POST":
        if not can_edit:
            messages.error(request, "No tienes permiso para cambiar el calendario.")
            return redirect(f"{request.path}?site={site.pk}")
        action = request.POST.get("action")
        back = f"{request.path}?site={site.pk}&month={request.POST.get('month', '')}"
        try:
            if action == "create":
                with transaction.atomic():
                    exc = _exception_from_post(site, request.POST)
                    exc.created_by = request.user
                    exc.save()
                    lines = request.POST.getlist("lines")
                    if lines:
                        exc.work_centers.set(WorkCenter.objects.filter(site=site, pk__in=lines))
                    _log(site, ScheduleChange.Action.CALENDAR, f"Calendario: {exc.get_kind_display()} {exc.date:%d/%m/%Y} · {exc.summary}", request.user)
                workcal.invalidate(site.pk)
                messages.success(request, f"{exc.get_kind_display()} agregado: {exc.summary} ({exc.date:%d/%m/%Y}"
                                          + (f" a {exc.date_to:%d/%m/%Y}" if exc.date_to else "") + ").")
            elif action == "delete":
                exc = get_object_or_404(CalendarException, pk=request.POST.get("id"), site=site)
                text = f"{exc.get_kind_display()} {exc.date:%d/%m/%Y} · {exc.summary}"
                exc.delete()
                workcal.invalidate(site.pk)
                _log(site, ScheduleChange.Action.CALENDAR, f"Calendario: eliminado {text}", request.user)
                messages.success(request, f"Eliminado: {text}.")
            elif action == "holidays":
                year = int(request.POST.get("year") or timezone.localdate().year)
                n = workcal.load_colombia_holidays(site, year, request.user)
                _log(site, ScheduleChange.Action.CALENDAR, f"Calendario: {n} festivos de Colombia {year} cargados", request.user)
                messages.success(request, f"{n} festivos de Colombia {year} cargados." if n else f"Los festivos de {year} ya estaban cargados.")
        except ValidationError as exc:
            messages.error(request, _err(exc))
        except ValueError:
            messages.error(request, "Revisa los datos del formulario.")
        return redirect(back)

    today = timezone.localdate()
    try:
        y, m = map(int, (request.GET.get("month") or "").split("-"))
        first = date(y, m, 1)
    except ValueError:
        first = today.replace(day=1)
    ndays = pycal.monthrange(first.year, first.month)[1]
    grid_start = first - timedelta(days=first.weekday())
    last = first.replace(day=ndays)
    grid_end = last + timedelta(days=6 - last.weekday())
    cal = workcal.get_calendar(site.pk)
    shifts = site_shifts(site)
    tz = timezone.get_current_timezone()
    weeks, cur, week = [], grid_start, []
    month_hours = 0.0
    while cur <= grid_end:
        a = datetime.combine(cur, time(0), tzinfo=tz)
        wins = cal.windows(shifts, a, a + timedelta(days=1)) if shifts else []
        hours = sum((e - s).total_seconds() for s, e in wins) / 3600
        if cur.month == first.month:
            month_hours += hours
        off_shifts = [s.name for s in shifts if not s.works_on_weekday(cur.weekday())]
        week.append({"date": cur, "in_month": cur.month == first.month, "today": cur == today,
                     "holiday": cal.holiday(cur), "exceptions": [e for e in cal.exceptions_on(cur) if e.kind != "festivo"],
                     "hours": hours, "off_all": bool(shifts) and len(off_shifts) == len(shifts),
                     "off_shifts": off_shifts if len(off_shifts) != len(shifts) else []})
        if len(week) == 7:
            weeks.append(week)
            week = []
        cur += timedelta(days=1)
    upcoming = (CalendarException.objects.filter(site=site, date__gte=today - timedelta(days=1),
                                                 date__lte=today + timedelta(days=90))
                .select_related("shift").prefetch_related("work_centers").order_by("date"))
    prev_m = (first - timedelta(days=1)).replace(day=1)
    next_m = (last + timedelta(days=1))
    return render(request, "mes_core/work_calendar.html", {
        "site": site, "sites": sites, "weeks": weeks, "dow": DOW, "month_label": f"{MONTHS[first.month - 1].capitalize()} {first.year}",
        "month": first.strftime("%Y-%m"), "prev": prev_m.strftime("%Y-%m"), "next": next_m.strftime("%Y-%m"),
        "shifts": shifts, "lines": WorkCenter.objects.filter(site=site, active=True).order_by("code"),
        "kinds": CalendarException.Kind.choices, "upcoming": upcoming, "can_edit": can_edit,
        "month_hours": month_hours, "year": first.year,
        "holidays_loaded": CalendarException.objects.filter(site=site, kind="festivo", date__year=first.year).count(),
    })


# --- reglas de planeación ---------------------------------------------------------------------

RULE_FIELDS = ("w_due", "w_tier", "w_age", "w_priority", "frozen_hours", "default_changeover_minutes", "split_threshold_hours",
               "risk_tolerance_pct", "attendance_grace_minutes")
RULE_BOOLS = ("group_changeovers", "allow_split", "require_approval", "auto_release")


@needs(PLAN)
def planning_rules(request):
    site, sites = _site(request)
    rules = PlanningRules.objects.filter(site=site).first() or PlanningRules(site=site)
    if request.method == "POST":
        action = request.POST.get("action")
        back = f"{request.path}?site={site.pk}"
        try:
            if action == "rules":
                for f in RULE_FIELDS:
                    if f in request.POST:  # un campo que no viene en el formulario conserva su valor
                        setattr(rules, f, int(request.POST.get(f) or 0))
                for f in ("default_efficiency", "overtime_efficiency"):  # rendimiento: solo si viene en el formulario
                    if request.POST.get(f):
                        setattr(rules, f, int(request.POST[f]))
                for f in RULE_BOOLS:
                    setattr(rules, f, request.POST.get(f) == "on")
                rules.updated_by = request.user
                rules.full_clean()
                rules.save()
                _log(site, ScheduleChange.Action.RULES, "Reglas de planeación actualizadas", request.user)
                messages.success(request, "Reglas guardadas. El próximo «Plan automático» las usa.")
            elif action == "changeover_add":
                co = ChangeoverTime(site=site, work_center_kind=(request.POST.get("kind") or "").strip(),
                                    from_family=(request.POST.get("from_family") or "").strip(),
                                    to_family=(request.POST.get("to_family") or "").strip(),
                                    minutes=int(request.POST.get("minutes") or 0))
                if not co.from_family or not co.to_family:
                    raise ValidationError("Indica la familia de origen y la de destino.")
                co.full_clean()
                co.save()
                messages.success(request, f"Tiempo de cambio agregado: {co}.")
            elif action == "changeover_delete":
                get_object_or_404(ChangeoverTime, pk=request.POST.get("id"), site=site).delete()
                messages.success(request, "Tiempo de cambio eliminado.")
            elif action == "families":
                n = 0
                for key, value in request.POST.items():
                    if key.startswith("fam_"):
                        pk = int(key[4:])
                        n += Product.objects.filter(pk=pk).exclude(family=value.strip()[:60]).update(family=value.strip()[:60])
                messages.success(request, f"Familias actualizadas ({n} productos).")
        except ValidationError as exc:
            messages.error(request, _err(exc))
        except (ValueError, IntegrityError):
            messages.error(request, "Revisa los datos: ese tiempo de cambio ya existe o hay valores inválidos.")
        return redirect(back)

    now = timezone.now()
    open_orders = list(WorkOrder.objects.filter(site=site, status__in=WorkOrder.OPEN_STATUSES)
                       .select_related("customer", "product"))
    ranked = sorted(({"order": o, **planning.priority_score(o, rules if rules.pk else None, now)} for o in open_orders),
                    key=lambda r: -r["score"])[:15]
    product_ids = WorkOrder.objects.filter(site=site).values_list("product_id", flat=True).distinct()
    return render(request, "mes_core/planning_rules.html", {
        "site": site, "sites": sites, "rules": rules, "configured": bool(rules.pk), "ranked": ranked,
        "changeovers": ChangeoverTime.objects.filter(site=site),
        "kinds": sorted(set(WorkCenter.objects.filter(site=site).values_list("kind", flat=True))),
        "products": Product.objects.filter(pk__in=product_ids).order_by("family", "sku"),
        "families": sorted({f for f in Product.objects.exclude(family="").values_list("family", flat=True)}),
    })


# --- simulación «qué pasa si» --------------------------------------------------------------------

def _scenarios(request, site) -> list[dict]:
    data = request.session.get("sim", {})
    return data.get(str(site.pk)) or [{"name": "Escenario 1", "changes": []}]


def _save_scenarios(request, site, scenarios):
    data = request.session.get("sim", {})
    data[str(site.pk)] = scenarios
    request.session["sim"] = data
    request.session.modified = True


def _change_from_post(site, post) -> dict:
    kind = post.get("kind")
    if kind not in simulation.CHANGE_KINDS:
        raise ValidationError("Elige qué cambia.")
    ch = {"kind": kind}
    if kind == "pedido":
        pt = registry.get_process_type(post.get("process_type") or "")
        if not pt:
            raise ValidationError("Elige el tipo de proceso del pedido.")
        qty = float(post.get("quantity") or 0)
        if qty <= 0:
            raise ValidationError("La cantidad debe ser mayor que cero.")
        ch.update(process_type=pt.key, process_label=service_types.label_for(pt.key), quantity=qty, product=(post.get("product") or "").strip(),
                  priority=int(post.get("priority") or 1), due=post.get("due") or "")
    elif kind == "linea_parada":
        wc = get_object_or_404(WorkCenter, pk=post.get("line"), site=site)
        if not post.get("start") or not post.get("end"):
            raise ValidationError("Indica desde y hasta cuándo se para la línea.")
        ch.update(line=wc.pk, line_label=wc.code, start=post["start"], end=post["end"], reason=post.get("reason") or "")
    elif kind in ("horas_extra", "turno_adicional", "festivo"):
        if not parse_date(post.get("date") or ""):
            raise ValidationError("Indica la fecha.")
        ch.update(date=post["date"])
        if kind != "festivo":
            if not post.get("start_time") or not post.get("end_time"):
                raise ValidationError("Indica la hora de inicio y de fin.")
            ch.update(start_time=post["start_time"], end_time=post["end_time"], operators=int(post.get("operators") or 0))
        if kind == "horas_extra":
            sh = get_object_or_404(Shift, pk=post.get("shift"), site=site)
            ch.update(shift=sh.pk, shift_label=sh.name)
        if kind == "turno_adicional":
            ch.update(name=post.get("name") or "")
    elif kind == "prioridad":
        o = get_object_or_404(WorkOrder, pk=post.get("order"), site=site)
        if not post.get("priority") and not post.get("due"):
            raise ValidationError("Indica la nueva prioridad o el nuevo compromiso.")
        ch.update(order=o.pk, order_label=o.number, priority=post.get("priority") or "", due=post.get("due") or "")
    ch["label"] = simulation.describe(ch)
    return ch


@needs(PLAN)
def what_if(request):
    site, sites = _site(request)
    scenarios = _scenarios(request, site)
    result = None
    if request.method == "POST":
        action = request.POST.get("action")
        idx = int(request.POST.get("scenario") or 0)
        try:
            if action == "add_change":
                scenarios[idx]["changes"].append(_change_from_post(site, request.POST))
            elif action == "remove_change":
                scenarios[idx]["changes"].pop(int(request.POST.get("change")))
            elif action == "add_scenario" and len(scenarios) < 3:
                scenarios.append({"name": f"Escenario {len(scenarios) + 1}", "changes": []})
            elif action == "remove_scenario" and len(scenarios) > 1:
                scenarios.pop(idx)
            elif action == "rename":
                scenarios[idx]["name"] = (request.POST.get("name") or "").strip()[:60] or f"Escenario {idx + 1}"
            elif action == "reset":
                scenarios = [{"name": "Escenario 1", "changes": []}]
            elif action == "apply":
                sc = scenarios[idx]
                if not sc["changes"]:
                    raise ValidationError("Ese escenario no tiene cambios.")
                res = simulation.apply_scenario(site, sc["changes"], request.user)
                messages.success(request, f"«{sc['name']}» aplicado: {res.scheduled_operations} operaciones reprogramadas, "
                                          f"{len(res.late_orders)} pedidos atrasados.")
                scenarios.pop(idx)
                if not scenarios:
                    scenarios = [{"name": "Escenario 1", "changes": []}]
                _save_scenarios(request, site, scenarios)
                return redirect(f"{request.path}?site={site.pk}")
            _save_scenarios(request, site, scenarios)
            if action == "run":
                result = simulation.compare(site, scenarios, request.user)
            else:
                return redirect(f"{request.path}?site={site.pk}#esc-{idx}")
        except ValidationError as exc:
            messages.error(request, _err(exc))
            return redirect(f"{request.path}?site={site.pk}")
        except (ValueError, IndexError):
            messages.error(request, "Revisa los datos del cambio.")
            return redirect(f"{request.path}?site={site.pk}")
    scenarios = [dict(sc) for sc in scenarios]  # copia: el resultado no va a la sesión
    if result:
        for i, r in enumerate(result["results"]):
            scenarios[i]["outcome"] = r
    return render(request, "mes_core/what_if.html", {
        "site": site, "sites": sites, "scenarios": scenarios, "result": result,
        "kinds": simulation.CHANGE_KINDS.items(), "process_types": service_types.route_choices(),
        "lines": WorkCenter.objects.filter(site=site, active=True).order_by("code"), "shifts": site_shifts(site),
        "orders": WorkOrder.objects.filter(site=site, status__in=WorkOrder.OPEN_STATUSES).select_related("product").order_by("-pk")[:200],
        "has_changes": any(sc["changes"] for sc in scenarios),
    })


# --- horas extra sugeridas -------------------------------------------------------------------------

@needs(PLAN)
def overtime_suggestions(request):
    site, sites = _site(request)
    if request.method == "POST":
        try:
            change = json.loads(request.POST.get("change") or "{}")
            obj = simulation.apply_change(site, change, request.user)
            _log(site, ScheduleChange.Action.CALENDAR, f"Calendario: {simulation.describe(change)}", request.user)
            msg = f"Agregado al calendario: {simulation.describe(change)}."
            if request.POST.get("replan") == "on":
                res = planning.generate_plan(site, user=request.user)
                msg += f" Plan reprogramado: {len(res.late_orders)} pedidos atrasados."
            messages.success(request, msg)
            return redirect(f"{reverse('mes_core:work_calendar')}?site={site.pk}&month={change.get('date', '')[:7]}")
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            messages.error(request, _err(exc) if isinstance(exc, ValidationError) else "No se pudo aplicar la sugerencia.")
            return redirect(f"{request.path}?site={site.pk}")
    data = simulation.suggestions(site, request.user)
    for opt in data["options"]:
        opt["json"] = json.dumps(opt["change"])
    return render(request, "mes_core/overtime.html", {"site": site, "sites": sites, **data})


# --- mediano plazo y pronóstico --------------------------------------------------------------------

@needs(PLAN, APPROVE)
def medium_term(request):
    site, sites = _site(request)
    weeks = 4 if request.GET.get("weeks") == "4" else 8
    with_fc = request.GET.get("forecast", "1") != "0"
    data = horizon.medium_term(site, weeks=weeks, include_forecast=with_fc)
    return render(request, "mes_core/medium_term.html", {"site": site, "sites": sites, "n_weeks": weeks,
                                                          "with_fc": with_fc, **data})


# --- versiones del plan y aprobación ---------------------------------------------------------------

@needs(PLAN, APPROVE)
def plan_versions(request):
    site, sites = _site(request)
    can_plan, can_approve = request.user.has_perm(PLAN), request.user.has_perm(APPROVE)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "snapshot" and can_plan:
                v = versions.snapshot(site, name=request.POST.get("name", ""), note=request.POST.get("note", ""),
                                      user=request.user, propose=request.POST.get("propose") == "on")
                messages.success(request, f"Versión v{v.number} guardada" + (" y propuesta para aprobación." if v.status == "propuesta" else "."))
            else:
                v = get_object_or_404(PlanVersion, pk=request.POST.get("id"), site=site)
                if action == "propose" and can_plan:
                    versions.propose(v, request.user)
                    messages.success(request, f"v{v.number} propuesta para aprobación.")
                elif action == "approve" and can_approve:
                    res = versions.approve(v, request.user, request.POST.get("note", ""))
                    messages.success(request, f"v{v.number} aprobada. Enviados a insumos: {len(res['released'])} pedidos (insumos los libera a planta).")
                elif action == "reject" and can_approve:
                    versions.reject(v, request.user, request.POST.get("note", ""))
                    messages.success(request, f"v{v.number} rechazada.")
                else:
                    messages.error(request, "No tienes permiso para esta acción.")
        except ValidationError as exc:
            messages.error(request, _err(exc))
        nxt = request.POST.get("next")
        return redirect(nxt if nxt and nxt.startswith("/") else f"{request.path}?site={site.pk}")
    current = versions.summarize(versions.current_plan(site))
    return render(request, "mes_core/plan_versions.html", {
        "site": site, "sites": sites, "versions": PlanVersion.objects.filter(site=site).select_related("created_by", "decided_by")[:50],
        "current": current, "can_plan": can_plan, "can_approve": can_approve,
        "rules": PlanningRules.objects.filter(site=site).first(),
    })


@needs(PLAN, APPROVE)
def plan_version_detail(request, pk):
    v = get_object_or_404(PlanVersion.objects.select_related("site", "created_by", "decided_by"), pk=pk)
    other_id = request.GET.get("vs")
    other = PlanVersion.objects.filter(site=v.site, pk=other_id).first() if other_id else None
    if other:
        diff = versions.compare(other.data, v.data)
        label_a, label_b = f"v{other.number}", f"v{v.number}"
    else:
        diff = versions.compare(v.data, versions.current_plan(v.site))
        label_a, label_b = f"v{v.number}", "plan actual"
    return render(request, "mes_core/plan_version_detail.html", {
        "v": v, "site": v.site, "other": other, "diff": diff, "label_a": label_a, "label_b": label_b,
        "others": PlanVersion.objects.filter(site=v.site).exclude(pk=v.pk)[:30],
        "can_plan": request.user.has_perm(PLAN), "can_approve": request.user.has_perm(APPROVE),
    })


# --- plan frente a lo real -------------------------------------------------------------------------

@needs(PLAN, APPROVE)
def plan_adherence(request):
    site, sites = _site(request)
    day = parse_date(request.GET.get("day") or "") or timezone.localdate()
    shifts = site_shifts(site)
    shift = next((s for s in shifts if str(s.pk) == request.GET.get("shift")), None)
    tz = timezone.get_current_timezone()
    if shift:
        from .scheduling import shift_window
        start, end = shift_window(shift, day, tz)
    else:
        start = datetime.combine(day, time(0), tzinfo=tz)
        end = start + timedelta(days=1)
    version = PlanVersion.objects.filter(site=site, pk=request.GET.get("version")).first() if request.GET.get("version") else None
    data = versions.adherence(site, start, end, version)
    return render(request, "mes_core/plan_adherence.html", {
        "site": site, "sites": sites, "day": day, "shift": shift, "shifts": shifts, "start": start, "end": end,
        "all_versions": PlanVersion.objects.filter(site=site)[:40], "sel_version": version,
        "prev_day": day - timedelta(days=1), "next_day": day + timedelta(days=1), **data,
    })


# --- promesa de entrega (API para Nuevo pedido) ---------------------------------------------------

@login_required
@require_POST
def api_promise(request):
    if not (request.user.has_perm("mes_core.add_workorder") or request.user.has_perm(PLAN)):
        return JsonResponse({"error": "Sin permiso."}, status=403)
    try:
        data = json.loads(request.body or b"{}")
        site = get_object_or_404(Site, pk=data.get("site"))
        text = (data.get("process_type") or "").strip()
        st = service_types.find(text) if text else None
        if not text:
            return JsonResponse({"error": "Sin tipo de servicio: la promesa se calcula cuando planeación defina las etapas.",
                                 "info": True}, status=400)
        if st is None or not st.has_route:
            return JsonResponse({"error": "Este servicio no tiene ruta estándar: la promesa se calcula cuando planeación "
                                          "defina sus etapas.", "info": True}, status=400)
        qty = float(data.get("quantity") or 0)
        if qty <= 0:
            return JsonResponse({"error": "Indica las unidades."}, status=400)
        res = simulation.promise(site, process_type=st.key, quantity=qty,
                                 product_text=(data.get("product") or "").strip(), priority=int(data.get("priority") or 3),
                                 transit_days=int(data.get("transit_days") or 0))
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "Datos inválidos."}, status=400)
    if res.get("error"):
        return JsonResponse({"error": res["error"]}, status=400)
    wanted = parse_date(data.get("delivery_date") or "")
    out = {"start": timezone.localtime(res["start"]).isoformat() if res["start"] else None,
           "end": timezone.localtime(res["end"]).isoformat(), "dispatch": res["dispatch"].isoformat(),
           "delivery": res["delivery"].isoformat(), "man_hours": res["man_hours"], "ops": res["ops"]}
    if wanted:
        diff = (res["delivery"] - wanted).days
        out["ok"] = diff <= 0
        out["days_late"] = max(0, diff)
        out["days_early"] = max(0, -diff)
    return JsonResponse(out)
