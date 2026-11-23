"""Listas de materiales: consulta, edición por producto y carga desde Excel."""

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import bom
from .models import Operation, Product, Site, WorkCenter
from .supplies import fmt

VIEW, EDIT = "mes_core.view_bomline", "mes_core.change_bomline"
MAX_UPLOAD_MB = 10


def _need(user, perm):
    if not user.has_perm(perm):
        raise PermissionDenied


@login_required
def bom_list(request):
    _need(request.user, VIEW)
    q = request.GET.get("q", "").strip()
    qs = Product.objects.filter(kind=Product.Kind.PRODUCT).annotate(n=Count("bom_lines"), last=Max("bom_lines__updated_at"))
    if q:
        qs = qs.filter(Q(sku__icontains=q) | Q(name__icontains=q))
    else:
        qs = qs.filter(n__gt=0)
    page = Paginator(qs.order_by("-n", "sku") if q else qs.order_by("sku"), 100).get_page(request.GET.get("page"))
    return render(request, "mes_core/bom_list.html", {
        "page": page, "q": q, "can_edit": request.user.has_perm(EDIT),
        "total": Product.objects.filter(bom_lines__isnull=False).distinct().count(),
        "sites": Site.objects.filter(active=True),
    })


def _component_choices():
    """Insumos sugeridos: los que están en algún inventario de insumos."""
    rows = (Product.objects.filter(kind=Product.Kind.SUPPLY, active=True).order_by("sku")
            .values_list("sku", "name"))
    return [f"{sku} · {name}" for sku, name in rows]


def _stage_choices():
    kinds = WorkCenter.objects.exclude(kind="").values_list("kind", flat=True).distinct()
    names = Operation.objects.values_list("name", flat=True).distinct()[:200]
    return sorted({bom.normalize_key(x) for x in [*kinds, *names] if x})


@login_required
def bom_edit(request, pk):
    _need(request.user, VIEW)
    product = get_object_or_404(Product, pk=pk, kind=Product.Kind.PRODUCT)  # los insumos no llevan lista
    can_edit = request.user.has_perm(EDIT)
    posted = None
    if request.method == "POST":
        _need(request.user, EDIT)
        keys = request.POST.getlist("row")
        lines = []
        posted = []
        for k in keys:
            r = {f: request.POST.get(f"{f}_{k}", "") for f in ("comp", "qty", "scrap", "stage", "note")}
            r["key"], r["delete"] = k, request.POST.get(f"del_{k}") == "on"
            posted.append(r)
            if not k.isdigit() and not (r["comp"].strip() or r["qty"].strip()):
                continue  # fila nueva vacía
            lines.append(bom.LineInput(int(k) if k.isdigit() else None, r["comp"], r["qty"], r["scrap"], r["stage"],
                                       r["note"], r["delete"]))
        try:
            n = bom.save_lines(product, lines, request.user)
        except ValidationError as exc:
            for m in exc.messages:
                messages.error(request, m)
        else:
            messages.success(request, f"Lista de materiales de {product.sku} guardada: {n} insumo(s).")
            return redirect("mes_core:bom_edit", pk=product.pk)
    try:
        sample = Decimal(str(request.GET.get("cant") or "100").replace(",", "."))
    except ArithmeticError:
        sample = Decimal("100")
    if not sample.is_finite() or sample <= 0 or sample > 10**9:
        sample = Decimal("100")
    lines = list(product.bom_lines.select_related("component"))
    if posted is None:
        posted = [{"key": str(ln.pk), "comp": f"{ln.component.sku} · {ln.component.name}", "qty": fmt(ln.qty_per_unit),
                   "scrap": fmt(ln.scrap_pct) if ln.scrap_pct else "", "stage": ln.stage, "note": ln.note,
                   "delete": False} for ln in lines]
        posted += [{"key": f"n{i}", "comp": "", "qty": "", "scrap": "", "stage": "", "note": "", "delete": False}
                   for i in range(1, 4 if lines else 6)]
    by_pk = {str(ln.pk): ln for ln in lines}
    for r in posted:
        ln = by_pk.get(r["key"])
        r["line"] = ln
        if ln:
            r["sample"] = bom._round(sample * ln.qty_per_unit * (1 + ln.scrap_pct / 100), ln.component)
    return render(request, "mes_core/bom_edit.html", {
        "product": product, "rows": posted, "can_edit": can_edit, "sample": sample,
        "components": _component_choices(), "stages": _stage_choices(),
    })


@login_required
@require_POST
def bom_import(request):
    _need(request.user, EDIT)
    upload = request.FILES.get("file")
    if upload is None:
        messages.error(request, "Elige el archivo de Excel.")
        return redirect("mes_core:bom_list")
    if upload.size > MAX_UPLOAD_MB * 1024 * 1024:
        messages.error(request, f"El archivo pesa más de {MAX_UPLOAD_MB} MB.")
        return redirect("mes_core:bom_list")
    site = Site.objects.filter(active=True).first()
    res = bom.import_bom(upload.read(), upload.name, site, request.user)
    if res.ok:
        messages.success(request, f"Listas de materiales cargadas: {res.products} producto(s), {res.lines} línea(s).")
    else:
        messages.error(request, f"No se cargó «{upload.name}». Corrige y vuelve a subirlo (no se guardó nada):")
        for e in res.errors[:30]:
            messages.error(request, e)
        if len(res.errors) > 30:
            messages.error(request, f"… y {len(res.errors) - 30} errores más.")
    return redirect("mes_core:bom_list")


@login_required
def bom_template(request):
    _need(request.user, VIEW)
    resp = HttpResponse(bom.template(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = 'attachment; filename="plantilla_lista_de_materiales.xlsx"'
    return resp
