import math
from dataclasses import dataclass
from decimal import Decimal

from django.db import models

from mes_core.models import Product, WorkOrder


class PackagingSpec(models.Model):
    """Ficha de acondicionado: cómo se presenta un producto terminado."""

    product = models.OneToOneField(Product, on_delete=models.CASCADE, related_name="packaging_spec", verbose_name="producto",
                                   limit_choices_to={"kind": "producto"})
    units_per_box = models.PositiveIntegerField("unidades por caja", default=1)
    instructions = models.TextField("instrucciones", blank=True)

    class Meta:
        verbose_name = "ficha de acondicionado"
        verbose_name_plural = "fichas de acondicionado"

    def __str__(self) -> str:
        return f"Ficha {self.product.sku}"


class PackagingMaterial(models.Model):
    class Kind(models.TextChoices):
        LABEL = "etiqueta", "Etiqueta"
        HOOK = "gancho", "Gancho"
        BAG = "bolsa", "Bolsa"
        BOX = "caja", "Caja"
        OTHER = "otro", "Otro"

    spec = models.ForeignKey(PackagingSpec, on_delete=models.CASCADE, related_name="materials", verbose_name="ficha")
    kind = models.CharField("tipo", max_length=20, choices=Kind.choices)
    material = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+", verbose_name="insumo",
                                 limit_choices_to={"kind": "insumo"})
    qty_per_unit = models.DecimalField(
        "cantidad por unidad", max_digits=10, decimal_places=4, default=Decimal("1"),
        help_text="Para cajas se ignora: se calcula con 'unidades por caja'.",
    )
    waste_percent = models.DecimalField("% desperdicio", max_digits=5, decimal_places=2, default=Decimal("0"))

    class Meta:
        verbose_name = "insumo de acondicionado"
        verbose_name_plural = "insumos de acondicionado"

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.material.sku}"


@dataclass
class MaterialRequirement:
    material: Product
    kind: str
    quantity: Decimal


def material_requirements(order: WorkOrder) -> list[MaterialRequirement]:
    """Insumos necesarios para acondicionar la cantidad de la orden (incluye desperdicio)."""
    spec = PackagingSpec.objects.filter(product=order.product).prefetch_related("materials__material").first()
    if spec is None:
        return []
    result = []
    for m in spec.materials.all():
        if m.kind == PackagingMaterial.Kind.BOX:
            base = Decimal(math.ceil(order.quantity / spec.units_per_box))
        else:
            base = order.quantity * m.qty_per_unit
        qty = base * (1 + m.waste_percent / 100)
        result.append(MaterialRequirement(m.material, m.get_kind_display(), qty.quantize(Decimal("1"), rounding="ROUND_CEILING")))
    return result
