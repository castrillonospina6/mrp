"""Ventas: carga masiva de pedidos desde Excel."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from . import order_import
from .models import Site

MAX_UPLOAD_MB = 10


def _site(request):
    from . import site_scope

    return site_scope.site_and_sites(request)


@login_required
@permission_required("mes_core.add_workorder", raise_exception=True)
def order_import_page(request):
    site, sites = _site(request)
    result, name, error = None, "", ""
    if request.method == "POST":
        upload = request.FILES.get("file")
        if site is None or upload is None:
            error = "Elige la planta y el archivo de Excel."
        elif upload.size > MAX_UPLOAD_MB * 1024 * 1024:
            error = f"El archivo pesa más de {MAX_UPLOAD_MB} MB."
        else:
            name = upload.name
            result = order_import.import_orders(upload.read(), upload.name, site, request.user)
            if result.ok:
                if not result.created:
                    messages.warning(request, "El archivo no tiene pedidos debajo de los encabezados.")
                    return redirect("mes_core:order_import")
                msg = (f"{len(result.created)} pedido(s) cargados y «Por planear»: "
                       f"{result.created[0].number} a {result.created[-1].number}. Planeación ya recibió el aviso.")
                if result.new_customers:
                    msg += f" Clientes nuevos: {', '.join(sorted(set(result.new_customers)))}."
                if result.new_products:
                    msg += f" Productos nuevos: {', '.join(sorted(set(result.new_products))[:10])}."
                if result.new_services:
                    msg += f" Tipos de servicio nuevos: {', '.join(sorted(set(result.new_services)))}."
                messages.success(request, msg)
                return redirect(f"{reverse('mes_core:order_list')}?status=por_planear")
    return render(request, "mes_core/order_import.html", {
        "site": site, "sites": sites, "result": result, "upload_name": name, "error": error,
        "max_mb": MAX_UPLOAD_MB, "max_rows": order_import.MAX_ROWS,
    })


@login_required
@permission_required("mes_core.add_workorder", raise_exception=True)
def order_import_template(request):
    site, _ = _site(request)
    resp = HttpResponse(order_import.template(site),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = 'attachment; filename="plantilla_pedidos.xlsx"'
    return resp
