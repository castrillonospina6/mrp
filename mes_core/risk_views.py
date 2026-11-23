"""Pantalla «Pedidos en riesgo»: operaciones cuyo avance reportado va por debajo de lo programado."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from . import risk, site_scope


@login_required
def at_risk(request):
    user = request.user
    if not (user.has_perm("mes_core.view_operation") or user.has_perm("mes_core.supervise_crew")):
        raise PermissionDenied
    scope = site_scope.pick(request, allow_all=True)
    site, sites = scope.site, scope.sites
    if not sites:
        return render(request, "mes_core/at_risk.html", {"no_site": True})
    rows = []
    for s in ([site] if site else sites):  # «Todas»: el riesgo de cada bodega, juntos
        rows += risk.site_risks(s)
    rows.sort(key=lambda r: (not r.overdue, -r.behind_pct))
    mine = request.GET.get("mios") == "1" or (user.has_perm("mes_core.supervise_crew")
                                              and not user.has_perm("mes_core.plan_workorder")
                                              and "mios" not in request.GET)
    if mine:
        rows = [r for r in rows if r.op.supervisor_id == user.pk]
    return render(request, "mes_core/at_risk.html", {
        "site": site, "sites": sites, "site_allow_all": True, "site_param": scope.param, "rows": rows, "mine": mine,
        "tolerance": risk.tolerance(site) if site else risk.tolerance(site_scope.busiest(sites)),
        "can_plan": user.has_perm("mes_core.plan_workorder"),
    })
