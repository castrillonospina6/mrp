"""Pantallas de validación EAN: la cola de pedidos y la carga de EAN de cada pedido."""

import csv
import datetime as dt
import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, F, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from mes_core import site_scope
from mes_core.models import WorkOrder

from . import eans
from .models import CustomerEanFormat, EanSheet

ST = EanSheet.Status
TABS = {"pendientes": "Pendientes", "cargados": "Con EAN", "no_aplica": "No aplica"}
PREVIEW_ROWS = 400
SESSION_KEY = "eanes_table_{}"


def can_view(user) -> bool:
    return any(user.has_perm(p) for p in (eans.PERM, "mes_core.view_workorder"))


def can_edit(user) -> bool:
    return user.has_perm(eans.PERM)


def _pending_q():
    return Q(ean_sheet__isnull=True) | Q(ean_sheet__status=ST.PENDING)


def _base(site):
    """Pedidos abiertos de esa planta (``None`` = de todas las bodegas)."""
    qs = WorkOrder.objects.filter(status__in=WorkOrder.OPEN_STATUSES)
    if site is not None:
        qs = qs.filter(site=site)
    return qs.select_related("product", "customer", "site", "ean_sheet", "ean_sheet__loaded_by")


def _waiting(since) -> str:
    if not since:
        return ""
    mins = int((timezone.now() - since).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)} min"
    if mins < 60 * 24:
        return f"{mins // 60} h"
    return f"{mins // (60 * 24)} d"


@login_required
def queue(request):
    if not can_view(request.user):
        raise PermissionDenied
    scope = site_scope.pick(request, allow_all=True)
    site, sites = scope.site, scope.sites
    if not sites:
        return render(request, "eanes/queue.html", {"no_site": True})
    tab = request.GET.get("tab") if request.GET.get("tab") in TABS else "pendientes"
    q = request.GET.get("q", "").strip()
    base = _base(site)
    counts = {"pendientes": base.filter(_pending_q()).count(),
              "cargados": base.filter(ean_sheet__status=ST.LOADED).count(),
              "no_aplica": base.filter(ean_sheet__status=ST.NOT_APPLY).count()}
    if tab == "pendientes":
        orders = base.filter(_pending_q()).order_by("created_at", "pk")
    elif tab == "cargados":
        orders = base.filter(ean_sheet__status=ST.LOADED).order_by(F("ean_sheet__loaded_at").desc(nulls_last=True))
    else:
        orders = base.filter(ean_sheet__status=ST.NOT_APPLY).order_by(F("ean_sheet__loaded_at").desc(nulls_last=True))
    if q:
        orders = orders.filter(Q(number__icontains=q) | Q(customer__name__icontains=q) | Q(product__sku__icontains=q)
                               | Q(product__name__icontains=q) | Q(external_op__icontains=q)
                               | Q(purchase_order__icontains=q) | Q(reference__icontains=q)
                               | Q(ean_lines__ean__contains=q.strip())).distinct()
    orders = list(orders.annotate(n_refs=Count("ean_lines"), ean_total=Sum("ean_lines__quantity"))[:200])
    for o in orders:
        o.waiting = _waiting(o.created_at)
        o.ean_ok = o.n_refs and o.ean_total is not None and Decimal(o.ean_total) == o.quantity
        o.sheet = getattr(o, "ean_sheet", None)  # sin hoja = pendiente
    oldest = base.filter(_pending_q()).order_by("created_at").first()
    return render(request, "eanes/queue.html", {
        "site": site, "sites": sites, "site_allow_all": True, "site_param": scope.param,
        "tab": tab, "tabs": TABS, "q": q, "orders": orders, "counts": counts,
        "can_edit": can_edit(request.user), "oldest_wait": _waiting(oldest.created_at) if oldest else "",
    })


def _next_pending(order):
    return (_base(order.site).filter(_pending_q()).exclude(pk=order.pk).order_by("created_at", "pk").first())


def _json_cell(v):
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    return str(v)


def _store_table(request, order, rows, filename):
    request.session[SESSION_KEY.format(order.pk)] = {
        "name": filename[:120], "rows": [[_json_cell(v) for v in r[:40]] for r in rows]}


def _load_table(request, order):
    data = request.session.get(SESSION_KEY.format(order.pk))
    return (data["rows"], data["name"]) if data else (None, "")


def _mapping_from_post(post, width):
    out = {}
    for key in ("ean", "qty", "desc"):
        val = post.get(f"col_{key}", "")
        if val.isdigit() and int(val) < width:
            out[key] = int(val)
    return out


@login_required
def order_eans(request, pk):
    if not can_view(request.user):
        raise PermissionDenied
    order = get_object_or_404(WorkOrder.objects.select_related("product", "customer", "site"), pk=pk)
    edit = can_edit(request.user)
    ctx = {"tab": "pegar", "text": ""}
    if request.method == "POST":
        if not edit:
            raise PermissionDenied("Solo validación EAN carga los EAN de los pedidos.")
        action = request.POST.get("action", "")
        try:
            result = _handle_post(request, order, action, ctx)
            if result is not None:
                return result
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    return render(request, "eanes/order.html", _order_context(request, order, edit, ctx))


def _handle_post(request, order, action, ctx):
    post = request.POST
    saved_fmt = CustomerEanFormat.objects.filter(customer_id=order.customer_id).first() if order.customer_id else None
    if action == "preview_text":
        source = post.get("source") if post.get("source") in EanSheet.Source.values else EanSheet.Source.PASTE
        ctx.update(text=post.get("text", ""), tab="escanear" if source == EanSheet.Source.SCAN else "pegar")
        parsed = eans.parse_text(ctx["text"])
        _preview(ctx, order, parsed, source)
        if not parsed.rows and not parsed.errors:
            raise ValidationError("No encontré ningún EAN en lo que pegaste.")
        return None
    if action == "preview_file":
        upload = request.FILES.get("file")
        ctx["tab"] = "archivo"
        if not upload:
            raise ValidationError("Elige el archivo del cliente.")
        if upload.size > 15 * 1024 * 1024:
            raise ValidationError("El archivo pesa más de 15 MB: copia y pega solo las columnas del EAN y la cantidad.")
        rows = eans.read_table(upload)
        if not rows:
            raise ValidationError("El archivo está vacío.")
        _store_table(request, order, rows, upload.name)
        parsed = eans.parse_table(rows, saved=saved_fmt)
        ctx.update(file_name=upload.name, saved_fmt=saved_fmt)
        _preview(ctx, order, parsed, EanSheet.Source.FILE)
        return None
    if action == "remap":
        rows, name = _load_table(request, order)
        ctx["tab"] = "archivo"
        if rows is None:
            raise ValidationError("Vuelve a subir el archivo.")
        parsed = eans.parse_table(rows, mapping=_mapping_from_post(post, len(rows[0]) if rows else 0))
        ctx.update(file_name=name)
        _preview(ctx, order, parsed, EanSheet.Source.FILE)
        return None
    if action == "copy":
        ctx["tab"] = "copiar"
        src = WorkOrder.objects.filter(pk=post.get("from") or 0, ean_sheet__status=ST.LOADED).first()
        if src is None:
            raise ValidationError("Elige un pedido que ya tenga EAN cargados.")
        parsed = eans.Parsed(rows=[eans.Row(line.ean, line.quantity, line.description, i + 1)
                                   for i, line in enumerate(src.ean_lines.all())])
        ctx.update(copy_from=src, text=eans.rows_to_text(parsed.rows))
        _preview(ctx, order, parsed, EanSheet.Source.COPY)
        return None
    if action == "apply":
        try:
            payload = json.loads(post.get("payload") or "[]")
        except ValueError as exc:
            raise ValidationError("Los datos a guardar están dañados: vuelve a revisar la carga.") from exc
        if post.get("had_errors") and not post.get("ignore_errors"):
            raise ValidationError("Hay líneas con problema: corrígelas o marca que quieres guardar solo las válidas.")
        rows = eans.rows_from_payload(payload)
        source = post.get("source", "")
        summary = eans.apply(order, rows, user=request.user, mode="add" if post.get("mode") == "add" else "replace",
                             source=source, reason=post.get("reason", ""))
        if source == EanSheet.Source.FILE and post.get("remember"):
            table, _ = _load_table(request, order)
            if table:
                head, names, _auto = eans.detect_layout(table)
                eans.remember_format(order, names, _mapping_from_post(post, len(names)))
        request.session.pop(SESSION_KEY.format(order.pk), None)
        total = f"{summary['total']:,}".replace(",", ".")
        msg = (f"{order.number}: {summary['refs']} referencias guardadas · {total} unidades"
               + (f" · {summary['unlimited']} sin límite" if summary["unlimited"] else ""))
        if not summary["first"]:
            msg += f" (agregadas {summary['added']}, quitadas {summary['removed']}, cambiadas {summary['changed']})"
        messages.success(request, msg + ".")
        return _after_save(request, order)
    if action == "not_apply":
        eans.mark_not_apply(order, user=request.user, note=post.get("note", ""))
        messages.success(request, f"{order.number}: marcado «No aplica EAN». En planta no se pedirá escanear.")
        return _after_save(request, order)
    if action == "reopen":
        eans.reopen(order, user=request.user, reason=post.get("reason", ""))
        messages.info(request, f"{order.number} volvió a «Pendiente de EAN».")
        return redirect("eanes:order", pk=order.pk)
    if action == "edit":
        ctx.update(tab="pegar", text=eans.rows_to_text(list(order.ean_lines.all())))
        return None
    raise ValidationError("Acción no reconocida.")


def _after_save(request, order):
    if request.POST.get("next"):
        nxt = _next_pending(order)
        if nxt:
            return redirect("eanes:order", pk=nxt.pk)
        messages.info(request, "No quedan pedidos pendientes de EAN en esta planta.")
        return redirect(reverse("eanes:queue"))  # vuelve a la bodega (o «Todas») que estaba mirando
    return redirect("eanes:order", pk=order.pk)


def _preview(ctx, order, parsed, source):
    rv = eans.review(order, parsed)
    ctx.update(review=rv, source=source, payload=json.dumps(eans.rows_to_payload(rv.rows)),
               preview_rows=rv.rows[:PREVIEW_ROWS], preview_more=max(0, len(rv.rows) - PREVIEW_ROWS),
               columns=parsed.columns, mapping=parsed.mapping)
    if not ctx.get("text") and source in (EanSheet.Source.FILE, EanSheet.Source.COPY):
        ctx["text"] = eans.rows_to_text(rv.rows) if len(rv.rows) <= 3000 else ""


def _order_context(request, order, edit, ctx):
    lines = list(order.ean_lines.all())
    total = sum(line.quantity for line in lines if line.quantity is not None)
    unlimited = sum(1 for line in lines if line.quantity is None)
    try:
        sheet = order.ean_sheet
    except EanSheet.DoesNotExist:
        sheet = None
    status = sheet.status if sheet else ST.PENDING
    diff = Decimal(total) - Decimal(order.quantity) if lines and not (unlimited and total == 0) else None
    saved_fmt = CustomerEanFormat.objects.filter(customer_id=order.customer_id).first() if order.customer_id else None
    return {
        **ctx, "order": order, "sheet": sheet, "status": status, "status_label": ST(status).label,
        "lines": lines, "total": total, "unlimited": unlimited, "diff": diff, "can_edit": edit,
        "diff_abs": abs(diff) if diff is not None else None,
        "in_production": eans.in_production(order), "logs": list(order.ean_logs.select_related("user")[:40]),
        "copy_sources": eans.copy_sources(order) if edit else [], "saved_fmt": ctx.get("saved_fmt") or saved_fmt,
        "next_pending": _next_pending(order) if edit else None, "cancelled": order.status == WorkOrder.Status.CANCELLED,
        "scan_key": f"eanes-scan-{order.pk}",
        "progress": eans.order_progress(order) if lines and order.operations.exists() else None,
    }


@login_required
def order_csv(request, pk):
    if not can_view(request.user):
        raise PermissionDenied
    order = get_object_or_404(WorkOrder, pk=pk)
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="EAN {order.number}.csv"'
    resp.write("﻿")
    w = csv.writer(resp, delimiter=";")
    w.writerow(["EAN", "Cantidad", "Descripción"])
    for line in order.ean_lines.all():
        w.writerow([line.ean, line.quantity if line.quantity is not None else "sin límite", line.description])
    return resp
