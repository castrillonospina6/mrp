"""Pantallas del equipo de insumos: pedidos por liberar, insumos de cada pedido, inventario y movimientos."""

from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services, site_scope, staffing, supplies
from .models import Product, SupplyMovement, SupplyRequirement, SupplyStock, Site, WorkOrder

RELEASE = "mes_core.release_to_plant"
VIEW = "mes_core.view_supplyrequirement"
MAX_UPLOAD_MB = 10


def _can_view(user):
    return user.has_perm(RELEASE) or user.has_perm(VIEW) or user.has_perm("mes_core.view_supplystock")


def _need_view(user):
    if not _can_view(user):
        raise PermissionDenied


def _need_edit(user):
    if not user.has_perm(RELEASE):
        raise PermissionDenied("Solo el equipo de insumos puede hacer esto.")


def _site(request):
    return site_scope.site_and_sites(request)


def _supervisors_of(order):
    seen = {}
    for op in order.operations.select_related("supervisor"):
        if op.supervisor_id and op.supervisor_id not in seen:
            seen[op.supervisor_id] = op.supervisor
    return list(seen.values())


# ================================================================ cola de pedidos
@login_required
def supplies_queue(request):
    _need_view(request.user)
    scope = site_scope.pick(request, allow_all=True)
    site, sites = scope.site, scope.sites
    if not sites:
        return render(request, "mes_core/supplies_queue.html", {"no_site": True})
    tab = request.GET.get("tab", "liberar")
    base = (WorkOrder.objects.filter(scope.q()).select_related("product", "customer", "site")
            .annotate(n_lines=Count("supply_lines", distinct=True)))
    to_release = base.filter(status=WorkOrder.Status.SUPPLIES, supplies_released_at__isnull=True)
    waiting = base.filter(status=WorkOrder.Status.SUPPLIES, supplies_released_at__isnull=False)  # falta logística
    in_plant = base.filter(status__in=[WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS])
    soon = base.filter(status__in=[WorkOrder.Status.DRAFT, WorkOrder.Status.PLANNED],
                       planned_start__lt=timezone.now() + timedelta(days=14))
    orders = {"liberar": to_release, "logistica": waiting, "planta": in_plant, "proximos": soon}.get(tab, to_release)
    q = request.GET.get("q", "").strip()
    if q:
        orders = orders.filter(Q(number__icontains=q) | Q(customer__name__icontains=q) | Q(product__sku__icontains=q)
                               | Q(product__name__icontains=q) | Q(external_op__icontains=q))
    orders = list(orders.order_by("planned_start", "due_date")[:300])
    for o in orders:
        o.pct, o.missing = supplies.completeness(o)
        o.supervisors = _supervisors_of(o)
    return render(request, "mes_core/supplies_queue.html", {
        "site": site, "sites": sites, "site_allow_all": True, "site_param": scope.param, "tab": tab, "q": q,
        "orders": orders,
        "n_release": to_release.count(), "n_waiting": waiting.count(), "n_plant": in_plant.count(), "n_soon": soon.count(),
        "auto_release": supplies.auto_release_on(site) if site else False,
        "can_edit": request.user.has_perm(RELEASE), "now": timezone.now(),
    })


# ================================================================ insumos de un pedido
def _rows_from_post(post) -> list[dict]:
    rows = []
    for key in post.getlist("line"):
        rows.append({"key": key, "stock": post.get(f"stock_{key}", ""), "req": post.get(f"req_{key}", ""),
                     "op": post.get(f"op_{key}", ""), "to": post.get(f"to_{key}", ""), "note": post.get(f"note_{key}", ""),
                     "now": post.get(f"now_{key}", ""), "delete": post.get(f"del_{key}") == "1"})
    return rows


def _int(v):
    return int(v) if v and str(v).isdigit() else None


@login_required
def supply_order(request, pk):
    _need_view(request.user)
    order = get_object_or_404(WorkOrder.objects.select_related("site", "product", "customer"), pk=pk)
    can_edit = request.user.has_perm(RELEASE) and order.status in supplies.DELIVER_IN
    posted_rows = None
    if request.method == "POST":
        _need_edit(request.user)
        action = request.POST.get("action")
        if action == "bom":
            if order.status not in supplies.DELIVER_IN:
                raise PermissionDenied
            with transaction.atomic():
                n = supplies.add_bom_lines(order, request.user)
                supplies._refresh_pct(order)
            if n:
                messages.success(request, f"{n} insumo(s) agregado(s) desde la lista de materiales de {order.product.sku}.")
            else:
                messages.info(request, "La lista de materiales no tiene nada nuevo para este pedido.")
            return redirect("mes_core:supply_order", pk=order.pk)
        if action == "return":
            try:
                supplies.return_to_planning(order, request.POST.get("reason", ""), request.user)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(request, f"{order.number} devuelto a planeación con el motivo indicado.")
                return redirect("mes_core:supplies_queue")
            return redirect("mes_core:supply_order", pk=order.pk)
        rows = _rows_from_post(request.POST)
        status_before = order.status
        try:
            with transaction.atomic():
                lines = [supplies.LineInput(
                    pk=_int(r["key"]), stock_id=_int(r["stock"]), qty_required=r["req"], operation_id=_int(r["op"]),
                    deliver_to_id=_int(r["to"]), note=r["note"], delete=r["delete"]) for r in rows
                    if r["key"].isdigit() or r["stock"]]
                saved = supplies.save_lines(order, lines, request.user)
                by_key = {}
                for r in rows:  # cada fila enviada → su línea guardada (las nuevas en el mismo orden)
                    if r["delete"] or not (r["key"].isdigit() or r["stock"]):
                        continue
                    by_key[r["key"]] = saved.pop(0) if saved else None
                if action in ("deliver", "release"):
                    qty = {by_key[r["key"]].pk: r["now"] for r in rows if r["key"] in by_key and by_key[r["key"]]
                           and (r["now"] or "").strip()}
                    supplies.deliver(order, qty, request.user, request.POST.get("deliver_note", ""))
                if action == "release":
                    supplies.release_to_plant(order, request.user, partial_ok=request.POST.get("partial") == "on",
                                              note=request.POST.get("partial_note", ""))
        except ValidationError as exc:
            for m in exc.messages:
                messages.error(request, m)
            posted_rows = rows
        else:
            was_ready = supplies.supplies_ready(order)
            order.refresh_from_db()
            msg = {"save": "Insumos del pedido guardados.", "deliver": "Entrega registrada: el inventario se descontó.",
                   "release": "Insumos liberados."}.get(action, "Guardado.")
            if action != "release" and not was_ready and supplies.supplies_ready(order):
                msg += " Con el 100 % entregado, los insumos se liberaron solos."
            if order.status == WorkOrder.Status.RELEASED and order.status != status_before:
                msg += f" Logística ya lo mercó: {order.number} pasó a planta y el supervisor ya puede ejecutarlo."
            elif supplies.supplies_ready(order) and not supplies.goods_ready(order):
                msg += " Falta que logística merque la mercancía; el pedido pasa a planta solo cuando la marquen."
            messages.success(request, msg)
            if action == "release" or (supplies.supplies_ready(order) and not was_ready):
                return redirect("mes_core:supplies_queue")
            return redirect("mes_core:supply_order", pk=order.pk)

    order.refresh_from_db()
    lines = list(order.supply_lines.select_related("stock__product", "operation", "deliver_to"))
    for ln in lines:
        ln.state = supplies.stock_state(ln.stock, ln.qty_pending)
        ln.suggest = min(ln.qty_pending, max(Decimal("0"), ln.stock.on_hand))
    if posted_rows is None:
        posted_rows = [{"key": str(ln.pk), "stock": str(ln.stock_id), "req": supplies.fmt(ln.qty_required),
                        "op": str(ln.operation_id or ""), "to": str(ln.deliver_to_id or ""), "note": ln.note,
                        "now": supplies.fmt(ln.suggest) if ln.suggest else "", "delete": False} for ln in lines]
    line_by_key = {str(ln.pk): ln for ln in lines}
    for r in posted_rows:
        r["line"] = line_by_key.get(r["key"])
    pct, missing = supplies.completeness(order)
    stocks = list(SupplyStock.objects.filter(site=order.site, active=True).select_related("product").order_by("product__name"))
    return render(request, "mes_core/supply_order.html", {
        "order": order, "rows": posted_rows, "new_rows": [r for r in posted_rows if not r["key"].isdigit()],
        "stocks": stocks, "operations": list(order.operations.select_related("work_center", "supervisor")),
        "supervisors": [(u.pk, staffing.user_label(u)) for u in staffing.supervisors()],
        "order_supervisors": _supervisors_of(order), "pct": pct, "missing": missing, "can_edit": can_edit,
        "can_release": can_edit and order.status == WorkOrder.Status.SUPPLIES and not supplies.supplies_ready(order),
        "can_return": can_edit and order.status == WorkOrder.Status.SUPPLIES,
        "supplies_ready": supplies.supplies_ready(order), "goods_ready": supplies.goods_ready(order),
        "auto_release": supplies.auto_release_on(order.site),
        "bom_count": order.product.bom_lines.count(),
        "movements": order.supply_movements.select_related("stock__product", "user", "deliver_to")[:50],
    })


# ================================================================ enviar a insumos (planeación)
@login_required
@require_POST
def order_send_supplies(request, pk):
    order = get_object_or_404(WorkOrder, pk=pk)
    if not request.user.has_perm("mes_core.plan_workorder"):
        messages.error(request, "Solo planeación envía pedidos a insumos.")
        return redirect(order)
    from .views import _approval_required

    if _approval_required(order, request.user):
        messages.error(request, "Esta planta exige plan aprobado: propone una versión del plan para que el jefe de planta "
                                "la apruebe; al aprobarla el pedido pasa a insumos.")
        return redirect(order)
    try:
        supplies.send_to_supplies(order, request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, f"{order.number} enviado al equipo de insumos. Ellos lo liberan a planta.")
    return redirect(order)


# ================================================================ inventario
@login_required
def supply_inventory(request):
    _need_view(request.user)
    site, sites = _site(request)
    if site is None:
        return render(request, "mes_core/supply_inventory.html", {"no_site": True})
    if request.method == "POST":
        _need_edit(request.user)
        _inventory_action(request, site)
        return redirect(f"{reverse('mes_core:supply_inventory')}?site={site.pk}&{request.POST.get('qs', '')}")
    q = request.GET.get("q", "").strip()
    state_f = request.GET.get("estado", "")
    stocks = SupplyStock.objects.filter(site=site).select_related("product").order_by("product__name")
    if request.GET.get("inactivos") != "1":
        stocks = stocks.filter(active=True)
    if q:
        stocks = stocks.filter(Q(product__sku__icontains=q) | Q(product__name__icontains=q) | Q(location__icontains=q))
    pending = supplies.committed(site)
    rows = []
    for s in stocks:
        p = pending.get(s.pk, Decimal("0"))
        cls, label = supplies.stock_state(s, p)
        rows.append({"s": s, "committed": p, "free": s.on_hand - p, "cls": cls, "label": label})
    kpi = {"items": len(rows), "low": sum(1 for r in rows if r["cls"] == "warn"),
           "out": sum(1 for r in rows if r["cls"] == "bad"),
           "orders": SupplyRequirement.objects.filter(stock__site=site, order__status=WorkOrder.Status.SUPPLIES)
           .values("order").distinct().count()}
    if state_f == "reponer":
        rows = [r for r in rows if r["cls"] != "ok"]
    return render(request, "mes_core/supply_inventory.html", {
        "site": site, "sites": sites, "rows": rows, "q": q, "state_f": state_f, "kpi": kpi,
        "can_edit": request.user.has_perm(RELEASE), "units": Product.Unit.choices,
        "qs": request.GET.urlencode().replace(f"site={site.pk}", "").strip("&"),
    })


def _inventory_action(request, site):
    action = request.POST.get("action")
    try:
        if action == "new":
            text = " ".join((request.POST.get("product") or "").split())
            product, created = services.resolve_product(text, kind=Product.Kind.SUPPLY)
            unit = request.POST.get("unit")
            if created and unit in Product.Unit.values:
                product.unit = unit
                product.save(update_fields=["unit"])
            stock, new = SupplyStock.objects.get_or_create(site=site, product=product)
            stock.safety_stock = supplies._dec(request.POST.get("safety") or 0, "Stock de seguridad")
            stock.location = (request.POST.get("location") or "").strip()[:60]
            stock.active = True
            stock.save()
            if (request.POST.get("on_hand") or "").strip():
                supplies.adjust(stock, request.POST["on_hand"], request.user, "Existencia inicial")
            messages.success(request, f"Insumo {product.sku} · {product.name} {'agregado' if new else 'actualizado'} en {site.code}.")
            return
        stock = get_object_or_404(SupplyStock.objects.select_related("product"), pk=request.POST.get("id"), site=site)
        note = (request.POST.get("note") or "").strip()
        if action == "receive":
            supplies.receive(stock, request.POST.get("qty"), request.user, note or "Entrada")
            stock.refresh_from_db()
            messages.success(request, f"Entrada registrada: {stock.product.name} queda en {supplies.fmt(stock.on_hand)}.")
        elif action == "adjust":
            mv = supplies.adjust(stock, request.POST.get("qty"), request.user, note)
            stock.refresh_from_db()
            messages.success(request, f"Ajuste registrado: {stock.product.name} queda en {supplies.fmt(stock.on_hand)}."
                             if mv else "La existencia ya era esa: sin cambios.")
        elif action == "edit":
            stock.safety_stock = supplies._dec(request.POST.get("safety") or 0, "Stock de seguridad")
            stock.location = (request.POST.get("location") or "").strip()[:60]
            stock.active = request.POST.get("active") == "on"
            stock.save(update_fields=["safety_stock", "location", "active", "updated_at"])
            messages.success(request, f"{stock.product.name} actualizado.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))


@login_required
@require_POST
def supply_import(request):
    _need_edit(request.user)
    site, sites = _site(request)
    upload = request.FILES.get("file")
    back = f"{reverse('mes_core:supply_inventory')}?site={site.pk if site else ''}"
    if site is None or upload is None:
        messages.error(request, "Elige la planta y el archivo de Excel.")
        return redirect(back)
    if upload.size > MAX_UPLOAD_MB * 1024 * 1024:
        messages.error(request, f"El archivo pesa más de {MAX_UPLOAD_MB} MB.")
        return redirect(back)
    res = supplies.import_inventory(upload.read(), upload.name, site, request.user)
    if res.ok:
        messages.success(request, f"Inventario cargado: {res.created} insumos nuevos, {res.updated} actualizados, "
                                  f"{res.adjusted} existencias ajustadas.")
    else:
        for e in res.errors[:30]:
            messages.error(request, e)
        if len(res.errors) > 30:
            messages.error(request, f"… y {len(res.errors) - 30} errores más. No se cargó nada.")
    return redirect(back)


@login_required
def supply_template(request):
    _need_view(request.user)
    site, _ = _site(request)
    resp = HttpResponse(supplies.inventory_template(site),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="plantilla_insumos_{site.code if site else "mes"}.xlsx"'
    return resp


@login_required
def supply_movements(request):
    _need_view(request.user)
    site, sites = _site(request)
    if site is None:
        return render(request, "mes_core/supply_movements.html", {"no_site": True})
    qs = SupplyMovement.objects.filter(stock__site=site).select_related("stock__product", "order", "user", "deliver_to")
    stock_f, kind_f = request.GET.get("insumo", ""), request.GET.get("tipo", "")
    if stock_f.isdigit():
        qs = qs.filter(stock_id=stock_f)
    if kind_f in SupplyMovement.Kind.values:
        qs = qs.filter(kind=kind_f)
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "mes_core/supply_movements.html", {
        "site": site, "sites": sites, "page": page, "stock_f": stock_f, "kind_f": kind_f, "kinds": SupplyMovement.Kind.choices,
        "stocks": SupplyStock.objects.filter(site=site).select_related("product").order_by("product__name"),
        "qs": params.urlencode(),
    })
