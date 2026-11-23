from decimal import Decimal

from django.db import models

from mes_core.models import Product, WorkOrder


class PickLine(models.Model):
    order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="pick_lines", verbose_name="orden")
    location = models.CharField("ubicación", max_length=30, help_text="Ej. A-01-03-B")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name="producto")
    qty_requested = models.DecimalField("solicitado", max_digits=12, decimal_places=3)
    qty_picked = models.DecimalField("alistado", max_digits=12, decimal_places=3, default=Decimal("0"))

    class Meta:
        verbose_name = "línea de picking"
        verbose_name_plural = "líneas de picking"
        ordering = ["order", "location"]  # orden de recorrido por ubicación

    def __str__(self) -> str:
        return f"{self.location} · {self.product.sku}"

    @property
    def shortage(self) -> Decimal:
        return max(Decimal("0"), self.qty_requested - self.qty_picked)
