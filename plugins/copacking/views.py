from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from mes_core.models import WorkOrder

from .models import PackagingSpec, material_requirements


@login_required
def spec_list(request):
    specs = PackagingSpec.objects.select_related("product").prefetch_related("materials__material")
    return render(request, "copacking/spec_list.html", {"specs": specs})


@login_required
def order_materials(request, pk):
    order = get_object_or_404(WorkOrder.objects.select_related("product"), pk=pk)
    return render(request, "copacking/order_materials.html", {
        "order": order,
        "requirements": material_requirements(order),
    })
