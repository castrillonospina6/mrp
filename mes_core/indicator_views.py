"""Pantalla «Indicadores»: OEE, OLE, paros, pausas, OTIF, adherencia al plan, puntualidad y horas extra."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.utils import timezone

from . import indicators as ind
from . import site_scope


def can_view(user) -> bool:
    return user.has_perm("mes_core.plan_workorder") or user.has_perm("mes_core.approve_plan")


@login_required
def indicators(request):
    if not can_view(request.user):
        raise PermissionDenied
    scope = site_scope.pick(request, allow_all=True)
    sites = scope.sites if scope.all else [scope.site]
    today = timezone.localdate()
    per, period_warn = ind.parse_period(request.GET, today)
    a, b = per.bounds()
    eff = ind.effectiveness(sites, a, b)
    tr = ind.series(sites, per, today)
    pp = per.previous()
    prev = ind.point(sites, pp.start, pp.end)

    def delta(cur, key):
        p = prev.get(key) if prev else None
        return None if cur is None or p is None else cur - p

    lines = eff["lines"]
    setup_rows = sorted((r for r in lines if r["n_setup"] or r["setup_min"] or r["n_setup_plan"]),
                        key=lambda r: (-r["setup_min"], -r["setup_plan_min"]))
    rel_rows = sorted((r for r in lines if r["n_fail"]), key=lambda r: (r["mtbf_h"] is None, r["mtbf_h"] or 0))
    teep_rows = sorted((r for r in lines if r["planned_h"]), key=lambda r: -(r["load"] or 0))
    o = eff["oee"]
    cal = o["calendar_h"] or 0

    def bar(h):
        return round(h / cal * 100, 1) if cal else 0

    teep_steps = [
        {"name": "Calendario (24 h todos los días)", "h": cal, "bar": 100 if cal else 0, "pct": 100 if cal else None},
        {"name": "Horas de turno", "h": o["shift_h"], "bar": bar(o["shift_h"]), "pct": o["utilization"],
         "help": "utilización del calendario"},
        {"name": "Con trabajo programado", "h": o["planned_h"], "bar": bar(o["planned_h"]), "pct": o["load"],
         "help": "carga del turno"},
        {"name": "Produciendo", "h": o["run_h"], "bar": bar(o["run_h"]), "pct": o["availability"],
         "help": "disponibilidad"},
        {"name": "A ritmo estándar y sin defectos", "h": cal * (o["teep"] or 0) / 100,
         "bar": o["teep"] or 0, "pct": o["teep"], "help": "TEEP"},
    ]

    return render(request, "mes_core/indicators.html", {
        "sites": scope.sites, "site": scope.site, "site_allow_all": True, "scope": scope,
        "per": per, "prev_per": pp, "next_per": per.next() if per.end < today else None, "period_warn": period_warn,
        "presets": ind.PRESETS, "today": today, "now": timezone.localtime(), "max_days": ind.MAX_DAYS,
        "trend_title": trend_title(per, tr),
        "eff": eff, "trend": tr,
        "oee_delta": delta(eff["oee"]["value"], "oee"), "ole_delta": delta(eff["ole"]["value"], "ole"),
        "spark_oee": ind.spark(tr, "oee"), "spark_ole": ind.spark(tr, "ole"),
        "stops": ind.stops_pareto(sites, a, b), "breaks": ind.exceeded_breaks(sites, a, b),
        "otif": ind.otif(sites, a, b), "adherence": ind.plan_adherence(sites, a, b),
        "punctuality": ind.punctuality(sites, a, b), "overtime": ind.overtime_response(sites, a, b),
        "level_label": ind.LEVEL_LABEL,
        "setup_rows": setup_rows, "rel_rows": rel_rows, "teep_rows": teep_rows, "teep_steps": teep_steps,
        "teep_delta": delta(o["teep"], "teep"),
        "setup_cause": next((c for c in _stop_causes() if ind.is_setup(c)), "Alistamiento"),
    })


def trend_title(per, tr) -> str:
    if not tr:
        return ""
    if per.is_week:
        return "últimas 8 semanas"
    if per.days <= 7:
        return "últimos 8 días"
    unit = "día a día" if per.days <= 31 else "semana a semana" if per.days <= 26 * 7 else "mes a mes"
    return f"{unit} en el periodo"


def _stop_causes():
    try:
        from plugins.piso.floor import STOP_CAUSES
    except ImportError:
        return ()
    return STOP_CAUSES
