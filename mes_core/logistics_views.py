"""Pantalla de logística: tarjetas de pedidos por mercar. Solo consulta + marcar como mercado."""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import logistics, site_scope
from .models import Operation, WorkOrder

PERM = logistics.PERM
GS = WorkOrder.GoodsStatus


def can_view(user) -> bool:
    return user.has_perm(PERM) or user.has_perm("mes_core.view_workorder")


def only_logistics(user) -> bool:
    """Usuario cuyo único trabajo es mercar: entra directo a su pantalla."""
    return (user.has_perm(PERM) and not user.is_superuser
            and not any(user.has_perm(p) for p in ("mes_core.plan_workorder", "mes_core.release_to_plant",
                                                   "mes_core.supervise_crew", "mes_core.add_workorder")))


@login_required
def goods_queue(request):
    if not can_view(request.user):
        raise PermissionDenied
    scope = site_scope.pick(request, allow_all=True)
    site, sites = scope.site, scope.sites
    if not sites:
        return render(request, "mes_core/goods_queue.html", {"no_site": True})
    tab = "mercados" if request.GET.get("tab") == "mercados" else "mercar"
    q = request.GET.get("q", "").strip()
    ops = Operation.objects.select_related("work_center", "supervisor").order_by("sequence")
    base = (WorkOrder.objects.filter(scope.q(), status__in=logistics.QUEUE_STATUSES)
            .select_related("product", "customer", "goods_marked_by", "site"))
    pending, picked = base.filter(goods_status=GS.PENDING), base.filter(goods_status=GS.PICKED)
    orders = pending if tab == "mercar" else picked
    if q:
        orders = orders.filter(Q(number__icontains=q) | Q(customer__name__icontains=q) | Q(product__sku__icontains=q)
                               | Q(product__name__icontains=q) | Q(external_op__icontains=q)
                               | Q(purchase_order__icontains=q) | Q(reference__icontains=q)
                               | Q(goods_location__icontains=q))
    if tab == "mercar":
        orders = orders.order_by(F("planned_start").asc(nulls_last=True), F("due_date").asc(nulls_last=True), "pk")
    else:
        orders = orders.order_by(F("goods_marked_at").desc(nulls_last=True), "-pk")
    orders = list(orders.prefetch_related(Prefetch("operations", queryset=ops))[:200])
    for o in orders:
        lines, sups = {}, {}
        for op in o.operations.all():
            if op.work_center_id:
                lines.setdefault(op.work_center_id, op.work_center.name)
            if op.supervisor_id:
                sups.setdefault(op.supervisor_id, op.supervisor)
        o.lines, o.sups = list(lines.values()), list(sups.values())
    user = request.user
    recent = (WorkOrder.objects.filter(scope.q()).exclude(goods_location="")
              .order_by(F("goods_marked_at").desc(nulls_last=True)).values_list("goods_location", flat=True)[:200])
    locations = list(dict.fromkeys(recent))[:30]  # sugerencias: ubicaciones usadas hace poco
    return render(request, "mes_core/goods_queue.html", {
        "site": site, "sites": sites, "site_allow_all": True, "site_param": scope.param, "tab": tab, "q": q, "orders": orders, "locations": locations,
        "n_pending": pending.count(), "n_picked": picked.count(),
        "can_mark": user.has_perm(PERM), "my_name": user.get_full_name() or user.get_username(),
    })


@login_required
@require_POST
def goods_mark(request, pk):
    if not request.user.has_perm(PERM):
        raise PermissionDenied("Solo logística marca pedidos como mercados.")
    order = get_object_or_404(WorkOrder, pk=pk)
    params = {k: request.POST.get(k, "") for k in ("tab", "q", "site")}
    back = f"{reverse('mes_core:goods_queue')}?{urlencode({k: v for k, v in params.items() if v})}"
    try:
        if request.POST.get("action") == "unmark":
            logistics.unmark(order, request.POST.get("reason", ""), request.user)
            messages.info(request, f"{order.number} volvió a «Por mercar».")
        else:
            first = logistics.mark_picked(order, request.POST.get("picked_by", ""), request.POST.get("location", ""),
                                          request.POST.get("note", ""), request.user)
            order.refresh_from_db()
            msg = f"{order.number} {'mercado' if first else 'actualizado'}. Mercancía en: {order.goods_location}."
            if first and order.status == WorkOrder.Status.RELEASED:
                msg += " Insumos ya estaba listo: el pedido pasó a planta."
            elif first and order.status == WorkOrder.Status.SUPPLIES:
                msg += " Pasa a planta cuando insumos libere su parte."
            messages.success(request, msg)
    except ValidationError as exc:
        messages.error(request, f"{order.number}: " + " ".join(exc.messages))
    return redirect(back)
