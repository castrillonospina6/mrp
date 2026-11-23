from dataclasses import dataclass
from decimal import Decimal

from django.db import models
from django.db.models import Sum

from mes_core.models import Product, WorkOrder


class YieldStandard(models.Model):
    """Rendimiento esperado: de 100 kg de ``input_product`` salen ``expected_percent`` kg de ``output_product``."""

    input_product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="yield_outputs", verbose_name="materia prima")
    output_product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="yield_inputs", verbose_name="corte / subproducto")
    expected_percent = models.DecimalField("% esperado", max_digits=5, decimal_places=2)
    tolerance_percent = models.DecimalField(
        "tolerancia (± puntos)", max_digits=5, decimal_places=2, default=Decimal("2"),
        help_text="Desviación aceptable antes de marcar alerta.",
    )

    class Meta:
        verbose_name = "rendimiento estándar"
        verbose_name_plural = "rendimientos estándar"
        constraints = [models.UniqueConstraint(fields=["input_product", "output_product"], name="uniq_yield_pair")]

    def __str__(self) -> str:
        return f"{self.input_product.sku} → {self.output_product.sku}: {self.expected_percent}%"


class Lot(models.Model):
    class Direction(models.TextChoices):
        INPUT = "entrada", "Entrada (materia prima)"
        OUTPUT = "salida", "Salida (producto)"

    code = models.CharField("código de lote", max_length=40, unique=True)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name="producto")
    order = models.ForeignKey(WorkOrder, on_delete=models.PROTECT, related_name="lots", verbose_name="orden")
    direction = models.CharField("tipo", max_length=10, choices=Direction.choices)
    quantity = models.DecimalField("cantidad (kg)", max_digits=12, decimal_places=3)
    supplier_lot = models.CharField("lote proveedor", max_length=40, blank=True)
    produced_at = models.DateTimeField("fecha", auto_now_add=True)
    expires_on = models.DateField("vence", null=True, blank=True)
    temperature_c = models.DecimalField("temperatura (°C)", max_digits=4, decimal_places=1, null=True, blank=True)

    class Meta:
        verbose_name = "lote"
        verbose_name_plural = "lotes"
        ordering = ["-produced_at"]

    def __str__(self) -> str:
        return f"{self.code} ({self.product.sku})"


@dataclass
class YieldLine:
    product: Product
    expected_percent: Decimal
    tolerance: Decimal
    expected_kg: Decimal
    actual_kg: Decimal
    actual_percent: Decimal | None

    @property
    def deviation(self) -> Decimal | None:
        return None if self.actual_percent is None else self.actual_percent - self.expected_percent

    @property
    def out_of_tolerance(self) -> bool:
        return self.deviation is not None and abs(self.deviation) > self.tolerance


def input_kg(order: WorkOrder) -> Decimal:
    """Kg reales de entrada: lotes de entrada registrados; si no hay, lo bueno de la 1.ª operación; si no, lo planeado."""
    lots = order.lots.filter(direction=Lot.Direction.INPUT).aggregate(t=Sum("quantity"))["t"]
    if lots:
        return lots
    first = order.operations.order_by("sequence").first()
    if first and first.qty_good:
        return first.qty_good
    return order.quantity


def yield_analysis(order: WorkOrder) -> list[YieldLine]:
    base = input_kg(order)
    outputs = dict(
        order.lots.filter(direction=Lot.Direction.OUTPUT).values("product").annotate(t=Sum("quantity")).values_list("product", "t")
    )
    lines = []
    for std in YieldStandard.objects.filter(input_product=order.product).select_related("output_product"):
        actual = outputs.get(std.output_product_id, Decimal("0"))
        lines.append(YieldLine(
            product=std.output_product,
            expected_percent=std.expected_percent,
            tolerance=std.tolerance_percent,
            expected_kg=(base * std.expected_percent / 100).quantize(Decimal("0.001")),
            actual_kg=actual,
            actual_percent=(actual * 100 / base).quantize(Decimal("0.01")) if actual and base else None,
        ))
    return lines
