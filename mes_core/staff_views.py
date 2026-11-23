"""Personal: operarios (carga masiva), matriz de polivalencia, asignación de supervisores, asistencia y cuadrillas."""

import json
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from . import site_scope, staffing
from .forms import EmployeeForm
from .models import Attendance, CrewAssignment, Employee, EmployeeSkill, Operation, Shift, Site, Skill, WorkOrder
from .normalize import normalize_document, normalize_key, normalize_text
from .plugins import registry
from .scheduler_views import _json_api

LEVELS = EmployeeSkill.Level
MAX_UPLOAD_MB = 10
ROSTER_MAX = 600  # filas de la lista general de asistencia por página (con más, usar la búsqueda o el turno)


def _site(request):
    return site_scope.site_and_sites(request)


def _back(name, qs: str = "", **params):
    url = reverse(name, kwargs=params) if params else reverse(name)
    qs = (qs or "").replace("\n", "").replace("\r", "").lstrip("?")
    return redirect(f"{url}?{qs}" if qs else url)


def _day(value):
    return parse_date(value or "") or timezone.localdate()


def _day_bounds(day):
    tz = timezone.get_current_timezone()
    a = datetime.combine(day, time(0), tzinfo=tz)
    return a, a + timedelta(days=1)


# ================================================================ operarios
def _employee_filters(request, site=None):
    """Filtros de la lista de personal. El personal es general: no se filtra por planta."""
    f = {k: request.GET.get(k, "").strip() for k in ("q", "sup", "shift", "skill")}
    if "sup" not in request.GET and staffing.team_scope(request.user):
        f["sup"] = str(request.user.pk)  # el supervisor arranca viendo su equipo («all» = todo el personal)
    f["status"] = request.GET.get("status", "activos")
    qs = Employee.objects.all()
    if f["q"]:
        text, doc = normalize_text(f["q"]), normalize_document(f["q"])
        cond = Q(full_name__icontains=text) | Q(position__icontains=text)
        if doc:
            cond |= Q(document__icontains=doc)
        qs = qs.filter(cond)
    if f["sup"] == "none":
        qs = qs.filter(supervisor__isnull=True)
    elif f["sup"].isdigit():
        qs = qs.filter(supervisor_id=f["sup"])
    if f["shift"] == "none":
        qs = qs.filter(shift__isnull=True)
    elif f["shift"].isdigit():
        qs = qs.filter(shift_id=f["shift"])
    if f["skill"].isdigit():
        qs = qs.filter(skills__skill_id=f["skill"]).distinct()
    if f["status"] == "activos":
        qs = qs.filter(active=True)
    elif f["status"] == "inactivos":
        qs = qs.filter(active=False)
    return qs, f


def _employee_list_context(request, site, sites, **extra):
    qs, f = _employee_filters(request, site)
    page = Paginator(qs.select_related("shift", "supervisor", "home_supervisor", "crew_operation__order")
                     .prefetch_related("skills__skill")
                     .order_by("full_name"), 100).get_page(request.GET.get("page"))
    user = request.user
    rows = [{"emp": e, "skills": staffing.employee_skill_summary(e), "editable": staffing.can_edit_employee(user, e)}
            for e in page]
    manager = staffing.manages_all(user)
    base = Employee.objects.filter(active=True)
    if not manager:
        base = base.filter(supervisor=user)  # indicadores de su equipo
    sups = list(staffing.supervisors())
    shift_list = staffing.all_shifts()
    params = {k: v for k, v in {**f, "site": site.pk}.items() if v}
    return {
        "site": site, "sites": sites, "page": page, "rows": rows, "f": f, "qs": urlencode(params),
        "manager": manager, "is_supervisor": user.has_perm("mes_core.supervise_crew"),
        "supervisors": [(u, staffing.user_label(u)) for u in sups],
        # en las acciones en bloque un supervisor solo puede pasar operarios a su equipo
        "bulk_supervisors": [(u, staffing.user_label(u)) for u in sups] if manager else [(user, "mi equipo")],
        "shifts": [(s, staffing.shift_label(s, shift_list)) for s in shift_list],
        "skills": Skill.objects.order_by("name"),
        "kpi": {
            "active": base.count(),
            "no_sup": Employee.objects.filter(active=True, supervisor__isnull=True).count(),
            "no_skill": base.filter(skills__isnull=True).count(),
            "skills": Skill.objects.filter(holders__isnull=False).distinct().count(),
        },
        "can_load": can_load_staff(request.user),
        "can_change": request.user.has_perm("mes_core.change_employee"),
        "levels": LEVELS.choices,
        **extra,
    }


@login_required
@permission_required("mes_core.view_employee", raise_exception=True)
def employee_list(request):
    site, sites = _site(request)
    if site is None:
        return render(request, "mes_core/staff_list.html", {"no_site": True})
    if request.method == "POST":
        return _employee_bulk(request, site)
    return render(request, "mes_core/staff_list.html", _employee_list_context(request, site, sites))


def _employee_bulk(request, site):
    if not request.user.has_perm("mes_core.change_employee"):
        raise PermissionDenied
    ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()]
    qs_back = request.POST.get("qs", "")
    emps = staffing.editable_employees(request.user, Employee.objects.filter(pk__in=ids))
    if not ids or not emps.exists():
        messages.warning(request, "Selecciona al menos un operario de tu equipo.")
        return _back("mes_core:employee_list", qs_back)
    changes, desc = {}, []
    sup = request.POST.get("set_supervisor", "")
    if sup.isdigit() and not staffing.manages_all(request.user) and int(sup) != request.user.pk:
        messages.error(request, "Solo puedes pasar operarios a tu propio equipo.")
        return _back("mes_core:employee_list", qs_back)
    if sup == "none":
        changes["supervisor"] = None
        desc.append("sin supervisor")
    elif sup.isdigit():
        user = staffing.supervisors().filter(pk=sup).first()
        if user is None:
            messages.error(request, "Ese usuario no tiene el rol Supervisor.")
            return _back("mes_core:employee_list", qs_back)
        changes["supervisor"] = user
        desc.append(f"supervisor {staffing.user_label(user)}")
    shift = request.POST.get("set_shift", "")
    if shift == "none":
        changes["shift"] = None
        desc.append("sin turno")
    elif shift.isdigit():
        sh = get_object_or_404(Shift, pk=shift)
        changes["shift"] = sh
        desc.append(f"turno {sh.name}")
    active = request.POST.get("set_active", "")
    if active in ("1", "0"):
        changes["active"] = active == "1"
        desc.append("activos" if active == "1" else "inactivos")
    if not changes:
        messages.warning(request, "Elige qué cambiar (supervisor, turno o estado).")
        return _back("mes_core:employee_list", qs_back)
    if "supervisor" in changes:  # elegido a mano: deja de ser un préstamo de cuadrilla
        changes.update(crew_borrowed=False, crew_operation=None, home_supervisor=None)
    with transaction.atomic():
        n = emps.update(**changes, updated_at=timezone.now())
        staffing._log(site, f"{n} operarios: " + ", ".join(desc), request.user)
    messages.success(request, f"{n} operario{'s' if n != 1 else ''} actualizado{'s' if n != 1 else ''}: {', '.join(desc)}.")
    return _back("mes_core:employee_list", qs_back)


# El personal se carga SOLO desde el panel de administración (uno a uno o desde Excel). Estas direcciones
# antiguas llevan allá a quien tiene acceso al panel.
def can_load_staff(user) -> bool:
    return user.is_active and user.is_staff and user.has_perm("mes_core.add_employee")


def _to_admin(request, name, site_param="site"):
    if not can_load_staff(request.user):
        raise PermissionDenied("El personal se carga solo desde el panel de administración.")
    site, _ = _site(request)
    url = reverse(name)
    return redirect(f"{url}?{site_param}={site.pk}" if site else url)


@login_required
def employee_import(request):
    return _to_admin(request, "admin:mes_core_employee_import")


@login_required
def employee_template(request):
    return _to_admin(request, "admin:mes_core_employee_template")


@login_required
def employee_form(request, pk=None):
    if pk is None:
        return _to_admin(request, "admin:mes_core_employee_add")
    emp = get_object_or_404(Employee.objects.select_related("site", "shift", "supervisor"), pk=pk)
    if not request.user.has_perm("mes_core.change_employee"):
        raise PermissionDenied
    if not staffing.can_edit_employee(request.user, emp):
        raise PermissionDenied(f"{emp.display_name} es del equipo de {staffing.user_label(emp.supervisor)}.")
    team_of = staffing.team_scope(request.user)
    can_skills = request.user.has_perm("mes_core.change_employeeskill")
    form = EmployeeForm(request.POST or None, instance=emp, team_of=team_of)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                emp = form.save(commit=False)
                if team_of is not None:
                    emp.supervisor = team_of  # lo que carga un supervisor queda en su equipo
                if "supervisor" in form.changed_data or (team_of is not None and emp.crew_borrowed
                                                         and emp.home_supervisor_id != team_of.pk):
                    emp.crew_borrowed, emp.crew_operation, emp.home_supervisor = False, None, None
                emp.save()
                if can_skills:
                    _save_employee_skills(request, emp)
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, f"Operario {emp.display_name} ({emp.document}) guardado.")
            return redirect(f"{reverse('mes_core:employee_list')}?site={emp.site_id}")
    skills = list(emp.skills.select_related("skill").order_by("skill__name")) if emp else []
    today = timezone.localdate()
    return render(request, "mes_core/staff_form.html", {
        "form": form, "emp": emp, "skills": skills, "levels": LEVELS.choices, "can_skills": can_skills,
        "all_skills": Skill.objects.order_by("name").values_list("name", flat=True),
        "new_rows": range(1, 4),
        "attendance": emp.attendance.select_related("shift", "confirmed_by").order_by("-day")[:10] if emp else [],
        "assignments": (emp.assignments.select_related("operation", "operation__order", "operation__work_center")
                        .filter(Q(operation__planned_end__date__gte=today) | Q(operation__planned_end__isnull=True))
                        .order_by("operation__planned_start")[:10]) if emp else [],
    })


def _save_employee_skills(request, emp):
    for es in list(emp.skills.all()):
        raw = request.POST.get(f"lvl_{es.skill_id}")
        if raw is None:
            continue
        if raw in ("", "0"):
            es.delete()
        elif raw.isdigit() and int(raw) in LEVELS.values and int(raw) != es.level:
            es.level, es.updated_by = int(raw), request.user
            es.save(update_fields=["level", "updated_by", "updated_at"])
    for i in range(1, 4):
        name = normalize_key(request.POST.get(f"new_skill_{i}"))
        raw = request.POST.get(f"new_level_{i}", "")
        if not name:
            continue
        if not raw.isdigit() or int(raw) not in LEVELS.values:
            raise ValidationError(f"Indica el nivel de «{name}».")
        skill, _ = Skill.objects.get_or_create(name=name)
        EmployeeSkill.objects.update_or_create(employee=emp, skill=skill,
                                               defaults={"level": int(raw), "updated_by": request.user})


# ================================================================ matriz de polivalencia
@login_required
@permission_required("mes_core.view_employee", raise_exception=True)
def skills_matrix(request):
    site, sites = _site(request)
    if site is None:
        return render(request, "mes_core/skills_matrix.html", {"no_site": True})
    user = request.user
    if request.method == "POST":
        if not user.has_perm("mes_core.add_skill"):
            raise PermissionDenied
        name = normalize_key(request.POST.get("name"))
        if not name:
            messages.error(request, "Escribe el nombre de la competencia.")
        else:
            skill, created = Skill.objects.get_or_create(name=name)
            if created:
                messages.success(request, f"Competencia «{skill.name}» creada. Asígnale niveles en la matriz.")
            else:
                messages.info(request, f"La competencia «{skill.name}» ya existía.")
        return _back("mes_core:skills_matrix", request.POST.get("qs", ""))

    has_team = Employee.objects.filter(supervisor=user, active=True).exists()
    scope = request.GET.get("scope") or ("mine" if has_team or staffing.team_scope(user) else "all")
    qs, f = _employee_filters(request, site)
    if scope == "mine":
        qs = qs.filter(supervisor=user)
    page = Paginator(qs.order_by("full_name").select_related("shift", "supervisor"), 60).get_page(request.GET.get("page"))
    skills = list(Skill.objects.order_by("name"))
    cells = {(es.employee_id, es.skill_id): es.level
             for es in EmployeeSkill.objects.filter(employee__in=list(page))}
    coverage = {}
    for row in (EmployeeSkill.objects.filter(employee__active=True)
                .values("skill_id", "level").annotate(n=Count("id"))):
        coverage.setdefault(row["skill_id"], {1: 0, 2: 0, 3: 0})[row["level"]] = row["n"]
    cols = [{"skill": s, "cov": coverage.get(s.pk, {1: 0, 2: 0, 3: 0})} for s in skills]
    rows = [{"emp": e, "cells": [(s.pk, cells.get((e.pk, s.pk))) for s in skills],
             "n": sum(1 for s in skills if (e.pk, s.pk) in cells),
             "editable": staffing.can_edit_employee(user, e)} for e in page]
    params = {k: v for k, v in {**f, "site": site.pk, "scope": scope}.items() if v}
    return render(request, "mes_core/skills_matrix.html", {
        "site": site, "sites": sites, "page": page, "rows": rows, "cols": cols, "f": f, "scope": scope,
        "has_team": has_team, "qs": urlencode(params), "levels": LEVELS.choices,
        "shifts": [(s, staffing.shift_label(s)) for s in staffing.all_shifts()],
        "can_edit": user.has_perm("mes_core.change_employeeskill"), "can_add_skill": user.has_perm("mes_core.add_skill"),
        "api_url": reverse("mes_core:api_skill_level"),
    })


@login_required
@require_POST
@_json_api
def api_skill_level(request):
    if not request.user.has_perm("mes_core.change_employeeskill"):
        return JsonResponse({"error": "No tienes permiso para editar la matriz de polivalencia."}, status=403)
    data = json.loads(request.body or b"{}")
    emp = get_object_or_404(Employee, pk=int(data["employee"]))
    if not staffing.can_edit_employee(request.user, emp):
        return JsonResponse({"error": f"{emp.display_name} es del equipo de {staffing.user_label(emp.supervisor)}: "
                                      "solo su supervisor o planeación pueden cambiar sus niveles."}, status=403)
    skill = get_object_or_404(Skill, pk=int(data["skill"]))
    level = data.get("level")
    level = int(level) if level not in (None, "", 0, "0") else None
    if level is not None and level not in LEVELS.values:
        raise ValidationError("Nivel inválido.")
    if level is None:
        EmployeeSkill.objects.filter(employee=emp, skill=skill).delete()
    else:
        EmployeeSkill.objects.update_or_create(employee=emp, skill=skill,
                                               defaults={"level": level, "updated_by": request.user})
    return JsonResponse({"ok": True, "level": level, "label": LEVELS(level).label if level else ""})


# ================================================================ sugerencias de nivel (polivalencia)
@login_required
@permission_required("mes_core.view_employeeskill", raise_exception=True)
def skill_suggestions(request):
    from . import skill_growth

    site, sites = _site(request)
    if site is None:
        return render(request, "mes_core/skill_suggestions.html", {"no_site": True})
    user = request.user
    can_decide = user.has_perm("mes_core.change_employeeskill")
    if request.method == "POST":
        if not can_decide:
            raise PermissionDenied
        emp = get_object_or_404(Employee, pk=request.POST.get("employee"))
        if not staffing.can_edit_employee(user, emp):
            raise PermissionDenied(f"{emp.display_name} es del equipo de {staffing.user_label(emp.supervisor)}.")
        skill = get_object_or_404(Skill, pk=request.POST.get("skill"))
        approve = request.POST.get("decision") == "aprobar"
        try:
            skill_growth.decide(emp, skill, int(request.POST.get("level") or 0), approve,
                                request.POST.get("hours") or 0, user)
        except (ValueError, ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            if approve:
                messages.success(request, f"{emp.display_name} ahora es {EmployeeSkill.Level(int(request.POST['level'])).label.lower()} "
                                          f"en {skill.name}.")
            else:
                messages.info(request, f"Sugerencia descartada para {emp.display_name} ({skill.name}).")
        return _back("mes_core:skill_suggestions", f"site={site.pk}")
    team = None if staffing.manages_all(user) else user
    rows = skill_growth.suggestions(site, supervisor=team)
    return render(request, "mes_core/skill_suggestions.html", {
        "site": site, "sites": sites, "rows": rows, "can_decide": can_decide, "team": team,
        "thresholds": sorted((EmployeeSkill.Level(k).label, v) for k, v in skill_growth.thresholds().items()),
    })


# ================================================================ reglas: supervisor por línea y turno
@login_required
@permission_required("mes_core.view_linesupervisor", raise_exception=True)
def line_supervisors(request):
    from .models import LineSupervisor, WorkCenter

    site, sites = _site(request)
    if site is None:
        return render(request, "mes_core/line_supervisors.html", {"no_site": True})
    lines = list(WorkCenter.objects.filter(site=site, active=True).order_by("kind", "code"))
    shifts = list(site.shifts.filter(active=True).order_by("start_time"))
    sups = list(staffing.supervisors())
    can_edit = request.user.has_perm("mes_core.change_linesupervisor")
    current = {(r.work_center_id, r.shift_id): r for r in LineSupervisor.objects.filter(work_center__site=site)}
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied
        valid = {u.pk: u for u in sups}
        changed = 0
        with transaction.atomic():
            for wc in lines:
                for sh in shifts:
                    raw = request.POST.get(f"r_{wc.pk}_{sh.pk}", "")
                    rule = current.get((wc.pk, sh.pk))
                    if not raw:
                        if rule:
                            rule.delete()
                            changed += 1
                        continue
                    user = valid.get(int(raw)) if raw.isdigit() else None
                    if user is None:
                        continue
                    if rule is None:
                        LineSupervisor.objects.create(work_center=wc, shift=sh, supervisor=user, updated_by=request.user)
                        changed += 1
                    elif rule.supervisor_id != user.pk:
                        rule.supervisor, rule.updated_by = user, request.user
                        rule.save()
                        changed += 1
            applied = staffing.apply_rules_to_site(site, request.user)
            if changed:
                staffing._log(site, f"Supervisores por línea y turno: {changed} regla(s) cambiadas", request.user)
        msg = f"Reglas guardadas ({changed} cambio{'s' if changed != 1 else ''})."
        if applied:
            msg += f" {applied} operación(es) programada(s) quedaron con su supervisor."
        messages.success(request, msg)
        return _back("mes_core:line_supervisors", f"site={site.pk}")
    grid = [{"line": wc, "cells": [{"shift": sh, "rule": current.get((wc.pk, sh.pk))} for sh in shifts]} for wc in lines]
    return render(request, "mes_core/line_supervisors.html", {
        "site": site, "sites": sites, "grid": grid, "shifts": shifts, "can_edit": can_edit,
        "supervisors": [(u.pk, staffing.user_label(u)) for u in sups], "n_rules": len(current),
    })


# ================================================================ planeación: supervisores responsables
@login_required
@permission_required("mes_core.assign_supervisor", raise_exception=True)
def supervisor_assign(request):
    scope = site_scope.pick(request, allow_all=True)
    site, sites = scope.site, scope.sites
    if not sites:
        return render(request, "mes_core/supervisor_assign.html", {"no_site": True})
    sups = list(staffing.supervisors())
    if request.method == "POST":
        return _supervisor_assign_save(request, scope, sups)

    today = timezone.localdate()
    d_from = parse_date(request.GET.get("desde") or "") or today
    d_to = parse_date(request.GET.get("hasta") or "") or d_from + timedelta(days=6)
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    d_to = min(d_to, d_from + timedelta(days=62))
    missing = request.GET.get("sin") == "1"
    unscheduled = request.GET.get("sinprog") == "1"
    sup_f = request.GET.get("sup", "")
    q = request.GET.get("q", "").strip()
    a, _ = _day_bounds(d_from)
    _, b = _day_bounds(d_to)
    win = Q(planned_start__lt=b, planned_end__gt=a)
    if unscheduled:
        win |= Q(planned_start__isnull=True)
    ops = (Operation.objects.filter(scope.q("order__site"), order__status__in=WorkOrder.OPEN_STATUSES)
           .exclude(status=Operation.Status.DONE).filter(win)
           .select_related("order", "order__product", "order__customer", "work_center", "supervisor")
           .annotate(n_crew=Count("crew_assignments")))
    if missing:
        ops = ops.filter(supervisor__isnull=True)
    if sup_f.isdigit():
        ops = ops.filter(supervisor_id=sup_f)
    if q:
        ops = ops.filter(Q(order__number__icontains=q) | Q(order__customer__name__icontains=q)
                         | Q(order__product__sku__icontains=q) | Q(order__product__name__icontains=q)
                         | Q(name__icontains=q) | Q(work_center__code__icontains=q) | Q(order__external_op__icontains=q))
    ops = list(ops.order_by("planned_start", "order__number", "sequence")[:600])
    groups, index = [], {}
    for op in ops:
        if op.order_id not in index:
            index[op.order_id] = {"order": op.order, "ops": [], "crew": 0, "missing": 0}
            groups.append(index[op.order_id])
        g = index[op.order_id]
        g["ops"].append(op)
        g["crew"] += op.crew
        g["missing"] += op.supervisor_id is None
    load = {u.pk: {"user": u, "label": staffing.user_label(u), "ops": 0, "crew": 0, "staffed": 0} for u in sups}
    for op in (Operation.objects.filter(scope.q("order__site"), supervisor__isnull=False, planned_start__lt=b,
                                        planned_end__gt=a)
               .exclude(status=Operation.Status.DONE).annotate(n_crew=Count("crew_assignments"))):
        if op.supervisor_id in load:
            row = load[op.supervisor_id]
            row["ops"] += 1
            row["crew"] += op.crew
            row["staffed"] += min(op.n_crew, op.crew)
    params = {"site": scope.param, "desde": d_from.isoformat(), "hasta": d_to.isoformat(),
              "sin": "1" if missing else "", "sinprog": "1" if unscheduled else "", "sup": sup_f, "q": q}
    return render(request, "mes_core/supervisor_assign.html", {
        "site": site, "sites": sites, "site_allow_all": True, "site_param": scope.param, "groups": groups, "n_ops": len(ops), "supervisors": load.values(),
        "sup_choices": [(u.pk, staffing.user_label(u)) for u in sups], "d_from": d_from, "d_to": d_to,
        "missing": missing, "unscheduled": unscheduled, "sup_f": sup_f, "q": q,
        "n_missing": sum(g["missing"] for g in groups), "qs": urlencode({k: v for k, v in params.items() if v}),
        "truncated": len(ops) >= 600,
    })


def _supervisor_assign_save(request, scope, sups):
    by_pk = {u.pk: u for u in sups}
    ids = [int(i) for i in request.POST.getlist("op") if i.isdigit()]
    ops = {op.pk: op for op in Operation.objects.filter(scope.q("order__site"), pk__in=ids)
           .select_related("order", "order__site")}
    plan: dict = {}
    for pk, op in ops.items():
        raw = request.POST.get(f"sup_{pk}", "")
        target = int(raw) if raw.isdigit() else None
        if raw and target not in by_pk:
            messages.error(request, "Uno de los supervisores elegidos ya no tiene el rol Supervisor.")
            return _back("mes_core:supervisor_assign", request.POST.get("qs", ""))
        if op.supervisor_id != target:
            plan.setdefault(target, []).append(op)
    total = 0
    try:
        with transaction.atomic():
            for target, items in plan.items():
                total += staffing.assign_supervisor(items, by_pk.get(target), user=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        if total:
            messages.success(request, f"Supervisor actualizado en {total} operación{'es' if total != 1 else ''}.")
        else:
            messages.info(request, "Sin cambios.")
    return _back("mes_core:supervisor_assign", request.POST.get("qs", ""))


# ================================================================ supervisor: mi turno (asistencia + operaciones)
def _can_supervise(user):
    return user.has_perm("mes_core.supervise_crew") or user.has_perm("mes_core.assign_supervisor")


@login_required
def supervision_home(request):
    user = request.user
    if not _can_supervise(user):
        raise PermissionDenied
    scope = site_scope.pick(request, allow_all=True)
    site, sites = scope.site, scope.sites
    if not sites:
        return render(request, "mes_core/supervision.html", {"no_site": True})
    if request.method == "POST":
        return _save_roster(request, scope)
    day = _day(request.GET.get("day"))
    see_all = user.has_perm("mes_core.assign_supervisor")
    has_team = Employee.objects.filter(supervisor=user, active=True).exists()
    # Asistencia: lista GENERAL (todo el personal, de todas las plantas). Cada supervisor marca a los que tiene.
    roster_f = request.GET.get("lista", "todos")
    if roster_f not in ("todos", "mios", "pendientes", "equipo"):
        roster_f = "todos"
    shift_f = request.GET.get("shift", "")
    q = request.GET.get("q", "").strip()

    day_att = Attendance.objects.filter(day=day)
    emps = Employee.objects.filter(active=True).select_related("shift", "shift__site", "supervisor")
    if roster_f == "mios":
        emps = emps.filter(pk__in=day_att.filter(confirmed_by=user).values("employee_id"))
    elif roster_f == "pendientes":
        emps = emps.exclude(pk__in=day_att.values("employee_id"))
    elif roster_f == "equipo":
        emps = emps.filter(supervisor=user)
    if shift_f.isdigit():
        emps = emps.filter(shift_id=shift_f)
    elif shift_f == "none":
        emps = emps.filter(shift__isnull=True)
    if q:
        text, doc = normalize_text(q), normalize_document(q)
        emps = emps.filter(Q(full_name__icontains=text) | Q(position__icontains=text)
                           | (Q(document__icontains=doc) if doc else Q(pk__in=[])))
    total_filtered = emps.count()
    emps = list(emps.order_by("full_name")[:ROSTER_MAX])
    att = {a.employee_id: a for a in Attendance.objects.filter(day=day, employee__in=emps).select_related("confirmed_by")}
    a, b = _day_bounds(day)
    crew_today = {}
    for ca in (CrewAssignment.objects.filter(employee__in=emps, operation__planned_start__lt=b, operation__planned_end__gt=a)
               .select_related("operation", "operation__order", "operation__work_center")):
        crew_today.setdefault(ca.employee_id, []).append(ca.operation)
    roster = [{"emp": e, "att": att.get(e.pk), "ops": crew_today.get(e.pk, [])} for e in emps]
    # Indicadores generales del día (todo el personal activo), no solo lo filtrado
    all_active = Employee.objects.filter(active=True)
    general = day_att.filter(employee__in=all_active)
    n_total = all_active.count()
    n_present = general.filter(status__in=Attendance.WORKING).count()
    n_absent = general.exclude(status__in=Attendance.WORKING).count()
    n_mine = general.filter(confirmed_by=user, status__in=Attendance.WORKING).count()

    # Operaciones: las del supervisor; planeación y jefe pueden ver las de toda la planta
    ops_f = request.GET.get("ops") or ("todas" if see_all and not Operation.objects.filter(
        scope.q("order__site"), supervisor=user, planned_start__lt=b, planned_end__gt=a).exists() else "mias")
    if not see_all:
        ops_f = "mias"
    ops = (Operation.objects.filter(scope.q("order__site")).exclude(status=Operation.Status.DONE)
           .filter(Q(planned_start__lt=b, planned_end__gt=a))
           .select_related("order", "order__product", "order__customer", "work_center", "supervisor")
           .annotate(n_crew=Count("crew_assignments")).order_by("planned_start", "order__number", "sequence"))
    if ops_f == "mias":
        ops = ops.filter(supervisor=user)
    ops = list(ops[:200])
    unscheduled = []
    if ops_f == "mias":
        unscheduled = list(Operation.objects.filter(scope.q("order__site"), supervisor=user, planned_start__isnull=True)
                           .exclude(status=Operation.Status.DONE).select_related("order", "order__product")
                           .annotate(n_crew=Count("crew_assignments"))[:50])
    required = sum(op.crew for op in ops)
    staffed = sum(min(op.n_crew, op.crew) for op in ops)
    params = {"site": scope.param, "day": day.isoformat(), "lista": roster_f, "ops": ops_f, "shift": shift_f, "q": q}
    shift_list = staffing.all_shifts()
    return render(request, "mes_core/supervision.html", {
        "site": site, "sites": sites, "site_allow_all": True, "site_param": scope.param, "day": day, "prev": day - timedelta(days=1), "next": day + timedelta(days=1),
        "today": timezone.localdate(), "roster_f": roster_f, "ops_f": ops_f, "has_team": has_team, "see_all": see_all,
        "shift_f": shift_f, "q": q, "shifts": [(s, staffing.shift_label(s, shift_list)) for s in shift_list],
        "roster": roster, "total_filtered": total_filtered, "roster_max": ROSTER_MAX,
        "n_team": n_total, "n_present": n_present, "n_absent": n_absent, "n_mine": n_mine,
        "n_pending": n_total - n_present - n_absent, "statuses": Attendance.Status.choices,
        "bulk_present": bool(q or shift_f or roster_f == "equipo"),
        "ops": ops, "unscheduled": unscheduled, "required": required, "staffed": staffed,
        "qs": urlencode({k: v for k, v in params.items() if v}),
        "base_qs": urlencode({"site": scope.param, "day": day.isoformat(), "ops": ops_f}),
    })


def _save_roster(request, scope):
    day = parse_date(request.POST.get("day") or "")
    if day is None:
        messages.error(request, "Fecha inválida.")
        return _back("mes_core:supervision_home", f"site={scope.param}")
    ids = [int(i) for i in request.POST.getlist("emp") if i.isdigit()]
    emps = Employee.objects.filter(pk__in=ids).select_related("shift")  # personal general
    entries = []
    for e in emps:
        status = request.POST.get(f"st_{e.pk}")
        if status is None:
            continue
        entries.append(staffing.AttendanceEntry(employee=e, status=status, notes=request.POST.get(f"nt_{e.pk}", "")))
    try:
        counts = staffing.save_attendance(day, entries, user=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        saved, cleared = counts["saved"], counts["cleared"]
        if saved or cleared:
            extra = f", {cleared} sin confirmar" if cleared else ""
            messages.success(request, f"Asistencia del {day:%d/%m/%Y} guardada ({saved} confirmados{extra}).")
        else:
            messages.info(request, "Sin cambios en la asistencia.")
    return _back("mes_core:supervision_home", request.POST.get("qs", ""))


# ================================================================ supervisor: armar la cuadrilla de una operación
@login_required
def crew_assign(request, pk):
    user = request.user
    op = get_object_or_404(Operation.objects.select_related(
        "order", "order__site", "order__product", "order__customer", "work_center", "stage", "supervisor"), pk=pk)
    is_planner = user.has_perm("mes_core.assign_supervisor")
    if not (is_planner or (user.has_perm("mes_core.supervise_crew") and op.supervisor_id == user.pk)):
        raise PermissionDenied("Esta operación está asignada a otro supervisor.")
    skills = list(Skill.objects.order_by("name"))
    skill_param = request.GET.get("skill", "")
    auto_skill = staffing.match_skill(op, skills)
    if skill_param == "none":
        skill = None
    elif skill_param.isdigit():
        skill = next((s for s in skills if s.pk == int(skill_param)), auto_skill)
    else:
        skill = auto_skill
    posted = None
    if request.method == "POST":
        posted = {int(i) for i in request.POST.getlist("emp") if i.isdigit()}
        try:
            with transaction.atomic():
                added, removed = staffing.set_crew(op, posted, user=user)
                extra = registry.crew_save(op, request.POST, user)  # p. ej. el patinador (control de piso)
        except ValidationError as exc:
            for m in exc.messages:
                messages.error(request, m)
        else:
            for m in extra:
                messages.info(request, m)
            n = len(posted)
            msg = f"Cuadrilla guardada: {n} de {op.crew} operarios"
            if n < op.crew:
                msg += f" (faltan {op.crew - n})"
            elif n > op.crew:
                msg += f" ({n - op.crew} más de lo planeado)"
            boss = op.supervisor if op.supervisor_id else (user if user.has_perm("mes_core.supervise_crew") else None)
            if added and boss is not None:
                msg += (f". Quedan con {staffing.user_label(boss)} mientras dure la operación; al terminarla vuelven a "
                        "quedar libres")
            messages.success(request, msg + ".")
            return redirect(request.get_full_path())
    candidates = staffing.crew_candidates(op, skill, user)
    if posted is not None:
        for c in candidates:
            c.assigned = c.employee.pk in posted
    shift, day = staffing.shift_day_of(op.order.site, op.planned_start)
    n_assigned = sum(1 for c in candidates if c.assigned)
    return render(request, "mes_core/crew_assign.html", {
        "op": op, "candidates": candidates, "skills": skills, "skill": skill, "auto_skill": auto_skill,
        "skill_param": skill_param, "shift": shift, "day": day, "n_assigned": n_assigned,
        "n_available": sum(1 for c in candidates if c.available and not c.assigned),
        "n_unconfirmed": sum(1 for c in candidates if c.attendance is None),
        "is_planner": is_planner, "levels": dict(LEVELS.choices), "crew_panels": registry.crew_panels(op, request),
        "back": f"{reverse('mes_core:supervision_home')}?site={op.order.site_id}" + (f"&day={day.isoformat()}" if day else ""),
    })
