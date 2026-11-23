"""EAN de cada pedido: qué referencias trae y cuántas unidades de cada una.

Lo carga una persona aparte (rol «Validación EAN»), no planeación. Planta luego solo podrá trabajar esas referencias y
esas cantidades. Ver ``eans.py`` para las reglas."""

from django.conf import settings
from django.db import models

from mes_core.models import Customer, WorkOrder


class EanSheet(models.Model):
    """Estado de la carga de EAN de un pedido (uno por pedido)."""

    class Status(models.TextChoices):
        PENDING = "pendiente", "Pendiente de EAN"
        LOADED = "cargado", "EAN cargados"
        NOT_APPLY = "no_aplica", "No aplica EAN"

    class Source(models.TextChoices):
        PASTE = "pegado", "Pegado desde Excel"
        FILE = "archivo", "Archivo del cliente"
        SCAN = "escaneo", "Escaneo con pistola"
        COPY = "copia", "Copiado de otro pedido"
        MANUAL = "manual", "Escrito a mano"

    order = models.OneToOneField(WorkOrder, on_delete=models.CASCADE, related_name="ean_sheet", verbose_name="pedido")
    status = models.CharField("estado", max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    source = models.CharField("cómo se cargó", max_length=10, choices=Source.choices, blank=True)
    note = models.CharField("nota", max_length=255, blank=True)
    loaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+", verbose_name="cargado por")
    loaded_at = models.DateTimeField("cargado el", null=True, blank=True)
    updated_at = models.DateTimeField("actualizado", auto_now=True)

    class Meta:
        verbose_name = "EAN de pedido"
        verbose_name_plural = "EAN de pedidos"
        permissions = [("manage_eans", "Puede cargar y corregir los EAN de los pedidos (validación EAN)")]

    def __str__(self) -> str:
        return f"{self.order.number} · {self.get_status_display()}"


class EanLine(models.Model):
    """Una referencia del pedido: su EAN y cuántas unidades hay que trabajar. Sin cantidad = sin límite."""

    order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="ean_lines", verbose_name="pedido")
    ean = models.CharField("EAN", max_length=14)
    description = models.CharField("descripción", max_length=200, blank=True)
    quantity = models.PositiveIntegerField("unidades", null=True, blank=True,
                                           help_text="Vacío = sin límite (acepta cualquier cantidad).")
    position = models.PositiveIntegerField("orden", default=0)

    class Meta:
        verbose_name = "referencia (EAN)"
        verbose_name_plural = "referencias (EAN)"
        ordering = ["order", "position", "pk"]
        constraints = [models.UniqueConstraint(fields=["order", "ean"], name="uniq_eanline_order_ean")]

    def __str__(self) -> str:
        return f"{self.ean} × {self.quantity if self.quantity is not None else 'sin límite'}"


class EanLog(models.Model):
    """Historial: quién cargó, cambió o marcó los EAN de cada pedido, y por qué."""

    order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="ean_logs", verbose_name="pedido")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="+", verbose_name="usuario")
    action = models.CharField("acción", max_length=40)
    detail = models.TextField("detalle", blank=True)
    reason = models.CharField("motivo", max_length=255, blank=True)
    at = models.DateTimeField("cuándo", auto_now_add=True)

    class Meta:
        verbose_name = "cambio de EAN"
        verbose_name_plural = "historial de EAN"
        ordering = ["-at", "-pk"]

    def __str__(self) -> str:
        return f"{self.order.number} · {self.action}"


class CustomerEanFormat(models.Model):
    """Cómo viene el archivo de cada cliente: qué columna es el EAN, cuál la cantidad y cuál la descripción.
    Se recuerda la primera vez para no volver a preguntarlo."""

    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name="ean_format",
                                    verbose_name="cliente")
    ean_column = models.CharField("columna del EAN", max_length=120)
    qty_column = models.CharField("columna de la cantidad", max_length=120)
    desc_column = models.CharField("columna de la descripción", max_length=120, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "formato de archivo de EAN del cliente"
        verbose_name_plural = "formatos de archivo de EAN por cliente"

    def __str__(self) -> str:
        return f"{self.customer} · EAN «{self.ean_column}», cantidad «{self.qty_column}»"


class EanControl(models.Model):
    """Lo que decide el supernumerario en planta para una referencia en una etapa (o en una operación sin etapa):
    si se le pueden enviar datos y en qué orden se trabaja. Sin registro = abierta y en el orden del pedido."""

    order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="ean_controls", verbose_name="pedido")
    stage_key = models.CharField("etapa", max_length=24, help_text="s<etapa> o o<operación> si no tiene etapa.")
    ean = models.CharField("EAN", max_length=14)
    enabled = models.BooleanField("abierta para reportar", default=True)
    position = models.PositiveIntegerField("orden de trabajo", default=0)
    changed_by = models.CharField("último cambio por", max_length=120, blank=True)
    updated_at = models.DateTimeField("actualizado", auto_now=True)

    class Meta:
        verbose_name = "referencia en planta (abierta / orden)"
        verbose_name_plural = "referencias en planta (abiertas / orden)"
        ordering = ["order", "stage_key", "position"]
        constraints = [models.UniqueConstraint(fields=["order", "stage_key", "ean"], name="uniq_eancontrol")]

    def __str__(self) -> str:
        return f"{self.order.number} · {self.stage_key} · {self.ean} · {'abierta' if self.enabled else 'cerrada'}"
