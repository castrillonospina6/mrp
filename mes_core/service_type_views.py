"""Configuración → Tipos de servicio: activar / desactivar, renombrar, crear y borrar (los manuales sin pedidos)."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from . import service_types
from .models import ServiceType, WorkOrder
from .normalize import normalize_key


@login_required
@permission_required("mes_core.view_servicetype", raise_exception=True)
def service_type_list(request):
    service_types.sync()
    user = request.user
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save":
            if not user.has_perm("mes_core.change_servicetype"):
                raise PermissionDenied
            _save_all(request)
        elif action == "add":
            if not user.has_perm("mes_core.add_servicetype"):
                raise PermissionDenied
            _add(request)
        elif action == "delete":
            if not user.has_perm("mes_core.delete_servicetype"):
                raise PermissionDenied
            _delete(request)
        return redirect("mes_core:service_type_list")

    counts = dict(WorkOrder.objects.values_list("process_type").annotate(n=Count("id")))
    rows = [{"st": st, "orders": counts.get(st.key, 0)}
            for st in ServiceType.objects.order_by("-active", "name")]
    return render(request, "mes_core/service_types.html", {
        "rows": rows, "n_active": sum(1 for r in rows if r["st"].active), "no_type": counts.get("", 0),
        "can_edit": user.has_perm("mes_core.change_servicetype"), "can_add": user.has_perm("mes_core.add_servicetype"),
        "can_delete": user.has_perm("mes_core.delete_servicetype"),
    })


def _save_all(request):
    ids = [int(i) for i in request.POST.getlist("st") if i.isdigit()]
    items = {st.pk: st for st in ServiceType.objects.filter(pk__in=ids)}
    seen: dict[str, str] = {}
    errors, changed = [], []
    for pk, st in items.items():
        name = service_types.clean_name(request.POST.get(f"name_{pk}", st.name)) or st.name
        active = request.POST.get(f"active_{pk}") == "on"
        norm = normalize_key(name)
        if norm in seen:
            errors.append(f"«{name}» está repetido (igual que «{seen[norm]}»).")
            continue
        seen[norm] = name
        if (name, active) != (st.name, st.active):
            st.name, st.active = name, active
            changed.append(st)
    if errors:
        messages.error(request, " ".join(errors) + " No se guardó nada.")
        return
    with transaction.atomic():
        for st in changed:
            st.save(update_fields=["name", "active"])
    if changed:
        messages.success(request, f"{len(changed)} tipo{'s' if len(changed) != 1 else ''} de servicio actualizado"
                                  f"{'s' if len(changed) != 1 else ''}.")
    else:
        messages.info(request, "Sin cambios.")


def _add(request):
    name = service_types.clean_name(request.POST.get("name"))
    if not name:
        messages.error(request, "Escribe el nombre del tipo de servicio.")
        return
    existing = service_types.find(name)
    if existing:
        if not existing.active:
            existing.active = True
            existing.save(update_fields=["active"])
            messages.success(request, f"«{existing.name}» ya existía y estaba inactivo: quedó activo.")
        else:
            messages.info(request, f"«{existing.name}» ya existe.")
        return
    st, _ = service_types.resolve(name)
    messages.success(request, f"Tipo de servicio «{st.name}» creado. Ya aparece en «Nuevo pedido».")


def _delete(request):
    st = get_object_or_404(ServiceType, pk=request.POST.get("id"))
    if not st.is_manual:
        messages.error(request, f"«{st.name}» lo aporta el módulo {st.plugin}: desactívalo en vez de borrarlo.")
        return
    n = WorkOrder.objects.filter(process_type=st.key).count()
    if n:
        messages.error(request, f"«{st.name}» tiene {n} pedido{'s' if n != 1 else ''}: desactívalo en vez de borrarlo.")
        return
    st.delete()
    messages.success(request, f"Tipo de servicio «{st.name}» borrado.")
