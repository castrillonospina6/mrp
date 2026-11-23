from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from mes_core.models import Product, WorkOrder

from .models import Lot, YieldStandard, input_kg, yield_analysis


class LotForm(forms.ModelForm):
    class Meta:
        model = Lot
        fields = ["direction", "product", "quantity", "supplier_lot", "expires_on", "temperature_c"]
        widgets = {"expires_on": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, order: WorkOrder, **kwargs):
        super().__init__(*args, **kwargs)
        outputs = YieldStandard.objects.filter(input_product=order.product).values("output_product")
        self.fields["product"].queryset = Product.objects.filter(Q(pk=order.product_id) | Q(pk__in=outputs))


def _next_lot_code(order: WorkOrder, direction: str) -> str:
    prefix = "E" if direction == Lot.Direction.INPUT else "S"
    n = order.lots.filter(direction=direction).count() + 1
    return f"{order.number}-{prefix}{n:02d}"


@login_required
def order_yield(request, pk):
    order = get_object_or_404(WorkOrder.objects.select_related("product"), pk=pk)
    form = LotForm(request.POST or None, order=order)
    if request.method == "POST" and form.is_valid():
        lot = form.save(commit=False)
        lot.order = order
        lot.code = _next_lot_code(order, lot.direction)
        try:
            with transaction.atomic():
                lot.save()
        except IntegrityError:
            messages.error(request, "Choque de código de lote; intenta de nuevo.")
        else:
            messages.success(request, f"Lote {lot.code} registrado.")
        return redirect("food:order_yield", pk=order.pk)

    lines = yield_analysis(order)
    return render(request, "food/order_yield.html", {
        "order": order,
        "input_kg": input_kg(order),
        "lines": lines,
        "total_expected": sum(line.expected_percent for line in lines),
        "lots": order.lots.select_related("product"),
        "form": form,
    })


@login_required
def lot_list(request):
    q = request.GET.get("q", "").strip()
    lots = Lot.objects.select_related("product", "order")
    if q:
        lots = lots.filter(Q(code__icontains=q) | Q(supplier_lot__icontains=q) | Q(order__number__icontains=q))
    return render(request, "food/lot_list.html", {"lots": lots[:200], "q": q})
