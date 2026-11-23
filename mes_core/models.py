from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse

from .plugins import registry


class Site(models.Model):
    class Kind(models.TextChoices):
        LOGISTICS = "logistica", "Centro logístico"
        FOOD = "alimentos", "Planta de alimentos"
        OTHER = "otro", "Otro"

    code = models.CharField("código", max_length=20, unique=True)
    name = models.CharField("nombre", max_length=120)
    kind = models.CharField("tipo", max_length=20, choices=Kind.choices, default=Kind.LOGISTICS)
    operators_total = models.PositiveIntegerField(
        "total de operarios", default=0,
        help_text="Personas disponibles para producir en un turno. Un turno con su propio número lo reemplaza. 0 = sin límite.",
    )
    active = models.BooleanField("activa", default=True)

    class Meta:
        verbose_name = "planta / sede"
        verbose_name_plural = "plantas / sedes"
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"


MAX_BREAK_GROUPS = 12


def parse_break_slots(raw, strict: bool = False) -> list[tuple]:
    """[["08:00","08:20"], …] → [(time, time), …]. Con strict=True lanza ValueError si algo no se entiende."""
    from datetime import datetime as _dt

    out = []
    for item in raw or []:
        try:
            a, b = (_dt.strptime(str(x).strip()[:5], "%H:%M").time() for x in item)
        except (TypeError, ValueError):
            if strict:
                raise ValueError(f"horario inválido {item!r} (usa HH:MM)") from None
            continue
        if a == b:
            if strict:
                raise ValueError(f"el horario {a:%H:%M}–{b:%H:%M} no dura nada")
            continue
        out.append((a, b))
    return out


def equivalent_break(groups) -> tuple | None:
    """Descanso equivalente de varios grupos escalonados: empieza con el primer grupo y dura lo que dura un grupo
    (promedio). Cada persona descansa una vez, así que la capacidad del turno baja exactamente eso."""
    if not groups:
        return None
    from datetime import datetime as _dt, timedelta as _td

    a = groups[0][0]
    avg = round(sum(Shift.span_minutes(x, y) for x, y in groups) / len(groups))
    return a, (_dt.combine(_dt(2000, 1, 1), a) + _td(minutes=avg)).time()


class Shift(models.Model):
    """Turno de una planta. Si termina antes (o a la misma hora) de lo que empieza, cruza la
    medianoche y pertenece al día en que TERMINA (Noche 22:00-06:00 del lunes = domingo 22:00 → lunes 06:00)."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="shifts", verbose_name="planta")
    name = models.CharField("nombre", max_length=40)
    start_time = models.TimeField("inicio")
    end_time = models.TimeField("fin")
    color = models.CharField("color", max_length=7, default="#3b82f6", help_text="Hex, p. ej. #3b82f6")
    breakfast_start = models.TimeField("desayuno desde", null=True, blank=True)
    breakfast_end = models.TimeField("desayuno hasta", null=True, blank=True)
    lunch_start = models.TimeField("almuerzo desde", null=True, blank=True)
    lunch_end = models.TimeField("almuerzo hasta", null=True, blank=True)
    operators = models.PositiveIntegerField(
        "operarios disponibles", default=0,
        help_text="Personas disponibles en este turno. 0 = usa el total de operarios de la planta.",
    )
    weekdays = models.CharField(
        "días laborables", max_length=7, default="0123456",
        help_text="Días de la semana en que trabaja el turno: 0=lunes … 6=domingo (p. ej. 012345 = lunes a sábado).")
    is_overlap = models.BooleanField(
        "turno de traslape", default=False,
        help_text="Se cruza con otros turnos (p. ej. 11:00–19:00 para cubrir descansos y cambios de turno). "
                  "La configuración rápida de turnos no lo modifica.")
    # Desayunos / almuerzos escalonados: la gente sale por grupos y cada persona toma UNO de estos horarios.
    # Formato: [["08:00", "08:20"], ["08:20", "08:40"]]. Vacío = un solo horario (breakfast_start/end) o ninguno.
    breakfast_slots = models.JSONField("horarios de desayuno", default=list, blank=True)
    lunch_slots = models.JSONField("horarios de almuerzo", default=list, blank=True)
    checkin_deadline = models.TimeField(
        "llegada hasta (hora máxima en la terminal)", null=True, blank=True,
        help_text="Hora máxima para confirmar la llegada en la tablet de planta. Quien confirme después queda «Llegó "
                  "tarde» de forma automática. Vacío = sin límite (siempre «Presente»).")
    active = models.BooleanField("activo", default=True)

    class Meta:
        verbose_name = "turno"
        verbose_name_plural = "turnos"
        ordering = ["site", "start_time"]
        constraints = [models.UniqueConstraint(fields=["site", "name"], name="uniq_shift_site_name")]

    def __str__(self) -> str:
        return f"{self.name} ({self.start_time:%H:%M}-{self.end_time:%H:%M})"

    WEEKDAY_NAMES = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
    WEEKDAY_SHORT = ("L", "M", "X", "J", "V", "S", "D")

    def works_on_weekday(self, weekday: int) -> bool:
        return str(weekday) in (self.weekdays if self.weekdays is not None else "0123456")

    @property
    def weekdays_label(self) -> str:
        days = [i for i in range(7) if self.works_on_weekday(i)]
        if days == list(range(7)):
            return "Todos los días"
        if days == list(range(5)):
            return "Lunes a viernes"
        if days == list(range(6)):
            return "Lunes a sábado"
        return ", ".join(self.WEEKDAY_SHORT[i] for i in days) or "Ningún día"

    @property
    def crosses_midnight(self) -> bool:
        """Termina después de medianoche (22:00–06:00). Uno que termina justo a las 00:00 no cruza."""
        return self.end_time <= self.start_time and self.end_time.hour * 60 + self.end_time.minute != 0

    @staticmethod
    def span_minutes(a, b) -> int:
        """Minutos de a hasta b en reloj de 24 h (b <= a cruza medianoche)."""
        m = (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)
        return m if m > 0 else m + 24 * 60

    @property
    def duration_minutes(self) -> int:
        return self.span_minutes(self.start_time, self.end_time)

    @property
    def breaks(self) -> list[tuple]:
        """Descansos que se descuentan del tiempo hábil. Con grupos escalonados es el tiempo de UN grupo (lo que pierde
        cada persona), ubicado en el primer grupo: la capacidad total del turno queda igual a la real."""
        return [(a, b) for a, b in ((self.breakfast_start, self.breakfast_end), (self.lunch_start, self.lunch_end)) if a and b]

    # --- desayunos / almuerzos por grupos ---
    def _groups(self, slots, a, b) -> list[tuple]:
        parsed = parse_break_slots(slots)
        if parsed:
            return parsed
        return [(a, b)] if a and b else []

    @property
    def breakfast_groups(self) -> list[tuple]:
        return self._groups(self.breakfast_slots, self.breakfast_start, self.breakfast_end)

    @property
    def lunch_groups(self) -> list[tuple]:
        return self._groups(self.lunch_slots, self.lunch_start, self.lunch_end)

    def set_break_groups(self, kind: str, groups) -> None:
        """kind = "breakfast" o "lunch"; groups = [(desde, hasta), …] (vacío = sin ese descanso)."""
        groups = sorted(groups, key=lambda g: self.span_minutes(self.start_time, g[0]) % (24 * 60)) if self.start_time \
            else sorted(groups)
        setattr(self, f"{kind}_slots", [[f"{a:%H:%M}", f"{b:%H:%M}"] for a, b in groups] if len(groups) > 1 else [])
        a, b = equivalent_break(groups) or (None, None)
        setattr(self, f"{kind}_start", a)
        setattr(self, f"{kind}_end", b)

    @staticmethod
    def groups_label(groups) -> str:
        if not groups:
            return "—"
        if len(groups) == 1:
            a, b = groups[0]
            return f"{a:%H:%M}–{b:%H:%M}"
        mins = {Shift.span_minutes(a, b) for a, b in groups}
        dur = f" · {mins.pop()} min" if len(mins) == 1 else ""
        return f"{len(groups)} grupos: " + ", ".join(f"{a:%H:%M}" for a, _ in groups) + dur

    @property
    def breakfast_label(self) -> str:
        return self.groups_label(self.breakfast_groups)

    @property
    def lunch_label(self) -> str:
        return self.groups_label(self.lunch_groups)

    def save(self, *args, **kwargs):
        for kind in ("breakfast", "lunch"):  # los grupos mandan: recalcula el descanso equivalente
            groups = parse_break_slots(getattr(self, f"{kind}_slots"))
            if groups:
                self.set_break_groups(kind, groups)
        super().save(*args, **kwargs)

    @property
    def break_minutes(self) -> int:
        return sum(self.span_minutes(a, b) for a, b in self.breaks)

    @property
    def effective_minutes(self) -> int:
        return max(0, self.duration_minutes - self.break_minutes)

    def clean(self) -> None:
        offset = lambda t: self.span_minutes(self.start_time, t) % (24 * 60)  # minutos desde el inicio del turno
        for label, kind in (("desayuno", "breakfast"), ("almuerzo", "lunch")):
            try:
                slots = parse_break_slots(getattr(self, f"{kind}_slots"), strict=True)
            except ValueError as exc:
                raise ValidationError(f"Horarios de {label} de {self.name}: {exc}") from None
            if len(slots) > MAX_BREAK_GROUPS:
                raise ValidationError(f"Máximo {MAX_BREAK_GROUPS} grupos de {label} por turno.")
            pairs = slots or [(getattr(self, f"{kind}_start"), getattr(self, f"{kind}_end"))]
            for a, b in pairs:
                if bool(a) != bool(b):
                    raise ValidationError(f"Indica inicio y fin del {label}, o deja ambos vacíos.")
                if a and not (0 < offset(a) < offset(b) <= self.duration_minutes):
                    raise ValidationError(f"El {label} {a:%H:%M}–{b:%H:%M} debe quedar dentro del turno {self.name} "
                                          f"({self.start_time:%H:%M}–{self.end_time:%H:%M}).")


class Customer(models.Model):
    class Tier(models.TextChoices):
        BRONZE = "bronce", "Bronce"
        SILVER = "plata", "Plata"
        GOLD = "oro", "Oro"
        VIP = "vip", "VIP"

    TIER_RANK = {Tier.VIP: 4, Tier.GOLD: 3, Tier.SILVER: 2, Tier.BRONZE: 1}

    name = models.CharField("nombre", max_length=120, unique=True)
    tier = models.CharField("nivel", max_length=10, choices=Tier.choices, default=Tier.BRONZE)
    active = models.BooleanField("activo", default=True)

    class Meta:
        verbose_name = "cliente"
        verbose_name_plural = "clientes"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class WorkCenter(models.Model):
    """Línea, mesa o estación. ``kind`` agrupa centros equivalentes (p. ej. "etiquetado")."""

    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="work_centers", verbose_name="planta")
    code = models.CharField("código", max_length=20)
    name = models.CharField("nombre", max_length=120)
    kind = models.CharField(
        "tipo de centro", max_length=40, db_index=True,
        help_text="Clave que usan las rutas de proceso para asignar el centro (p. ej. picking, etiquetado, deshuese).",
    )
    capacity = models.PositiveIntegerField(
        "estaciones en paralelo", default=1,
        help_text="Cuántas operaciones puede atender a la vez (mesas, operarios, líneas).",
    )
    shifts = models.ManyToManyField(
        Shift, blank=True, related_name="work_centers", verbose_name="turnos",
        help_text="Turnos en que trabaja. Vacío = todos los turnos activos de la planta.",
    )
    efficiency = models.PositiveSmallIntegerField(
        "rendimiento medido (%)", null=True, blank=True, validators=[MinValueValidator(10), MaxValueValidator(150)],
        help_text="Rendimiento real de la gente en esta línea frente al tiempo estándar. Vacío = el de la planta.")
    active = models.BooleanField("activo", default=True)

    class Meta:
        verbose_name = "centro de trabajo"
        verbose_name_plural = "centros de trabajo"
        ordering = ["site__code", "code"]
        constraints = [models.UniqueConstraint(fields=["site", "code"], name="uniq_workcenter_site_code")]

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"

    def clean(self) -> None:
        if self.capacity < 1:
            raise ValidationError({"capacity": "Debe ser al menos 1."})


class Product(models.Model):
    """Artículo del catálogo. Un PRODUCTO es lo que se procesa en las órdenes de producción; un INSUMO es lo que se
    consume para procesarlo (etiquetas, bolsas, cajas…). Nunca se mezclan: un pedido solo lleva productos y el
    inventario y las listas de materiales solo llevan insumos."""

    class Unit(models.TextChoices):
        UNIT = "UN", "Unidad"
        KG = "KG", "Kilogramo"
        BOX = "CJ", "Caja"

    class Kind(models.TextChoices):
        PRODUCT = "producto", "Producto (se procesa)"
        SUPPLY = "insumo", "Insumo (se consume)"

    sku = models.CharField("SKU", max_length=40, unique=True)
    name = models.CharField("nombre", max_length=200)
    kind = models.CharField("tipo", max_length=10, choices=Kind.choices, default=Kind.PRODUCT, db_index=True)
    unit = models.CharField("unidad", max_length=4, choices=Unit.choices, default=Unit.UNIT)
    family = models.CharField(
        "familia", max_length=60, blank=True,
        help_text="Agrupa productos que se procesan igual (misma etiqueta, empaque…). El plan los junta para perder menos tiempo en cambios.")
    active = models.BooleanField("activo", default=True)

    class Meta:
        verbose_name = "producto"
        verbose_name_plural = "productos"
        ordering = ["sku"]

    @property
    def is_supply(self) -> bool:
        return self.kind == self.Kind.SUPPLY

    def kind_error(self, expected: str) -> str:
        """Mensaje cuando se usa un artículo del otro tipo; '' si es del tipo esperado."""
        if self.kind == expected:
            return ""
        if expected == self.Kind.SUPPLY:
            return f"«{self.sku} · {self.name}» es un producto (se procesa en las órdenes), no un insumo."
        return f"«{self.sku} · {self.name}» es un insumo, no un producto para procesar."

    def __str__(self) -> str:
        return f"{self.sku} - {self.name}"


class SupplyItemManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(kind=Product.Kind.SUPPLY)


class SupplyItem(Product):
    """Vista del catálogo con solo los insumos (para el panel de administración)."""

    objects = SupplyItemManager()

    class Meta:
        proxy = True
        verbose_name = "insumo (catálogo)"
        verbose_name_plural = "insumos (catálogo)"

    def save(self, *args, **kwargs):
        self.kind = Product.Kind.SUPPLY
        super().save(*args, **kwargs)


PRODUCT_ONLY = {"kind": Product.Kind.PRODUCT}
SUPPLY_ONLY = {"kind": Product.Kind.SUPPLY}


class ServiceType(models.Model):
    """Tipo de servicio que se ofrece en «Nuevo pedido». Los módulos aportan los suyos (con ruta estándar de etapas);
    los demás se crean a mano, desde Configuración o escribiéndolos en el pedido. Se activan y desactivan aquí."""

    key = models.CharField("clave", max_length=40, unique=True)
    name = models.CharField("nombre", max_length=80)
    plugin = models.CharField("módulo", max_length=40, blank=True, help_text="Vacío = creado a mano.")
    active = models.BooleanField("activo", default=True, help_text="Inactivo = no aparece en la lista de «Nuevo pedido».")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "tipo de servicio"
        verbose_name_plural = "tipos de servicio"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def is_manual(self) -> bool:
        return not self.plugin

    @property
    def has_route(self) -> bool:
        """Tiene ruta estándar de etapas (la aporta un módulo instalado)."""
        return registry.get_process_type(self.key) is not None


class WorkOrder(models.Model):
    class Status(models.TextChoices):
        TO_PLAN = "por_planear", "Por planear"
        DRAFT = "borrador", "Borrador"
        PLANNED = "planificada", "Planificada"
        SUPPLIES = "insumos", "En insumos"
        RELEASED = "liberada", "Liberada"
        IN_PROGRESS = "en_proceso", "En proceso"
        DONE = "terminada", "Terminada"
        CANCELLED = "cancelada", "Cancelada"

    OPEN_STATUSES = (Status.TO_PLAN, Status.DRAFT, Status.PLANNED, Status.SUPPLIES, Status.RELEASED, Status.IN_PROGRESS)

    class GoodsStatus(models.TextChoices):
        PENDING = "pendiente", "Por mercar"
        PICKED = "mercado", "Mercado"

    number = models.CharField("número", max_length=20, unique=True, null=True, editable=False)
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="orders", verbose_name="planta")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="orders", verbose_name="producto",
                                limit_choices_to=PRODUCT_ONLY)
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders", verbose_name="cliente"
    )
    process_type = models.CharField("tipo de servicio", max_length=40, blank=True,
                                    help_text="Clave del tipo de servicio (ver Configuración → Tipos de servicio). Puede quedar vacío.")
    quantity = models.DecimalField("cantidad", max_digits=12, decimal_places=3)
    quantity_done = models.DecimalField("cantidad terminada", max_digits=12, decimal_places=3, default=Decimal("0"))
    status = models.CharField("estado", max_length=20, choices=Status.choices, default=Status.DRAFT)
    priority = models.PositiveSmallIntegerField("prioridad", default=3, help_text="1 = más urgente, 5 = menos urgente")
    due_date = models.DateTimeField("fecha compromiso", null=True, blank=True)
    planned_start = models.DateTimeField("inicio planificado", null=True, blank=True)
    planned_end = models.DateTimeField("fin planificado", null=True, blank=True)
    reference = models.CharField("referencia", max_length=60, blank=True, help_text="Pedido, cliente, ola…")
    purchase_order = models.CharField("OC (orden de compra)", max_length=60, blank=True)
    external_op = models.CharField("número de OP", max_length=60, blank=True,
                                   help_text="Número de OP del cliente o del ERP. El número interno se asigna solo.")
    shipment = models.CharField("embarque", max_length=60, blank=True)
    # Estado de recursos para producir (lo actualiza planeación / bodega)
    goods_pct = models.PositiveSmallIntegerField(
        "mercancía recibida (%)", default=0, validators=[MaxValueValidator(100)],
        help_text="Qué porcentaje de la mercancía a procesar ya está en planta.")
    supplies_pct = models.PositiveSmallIntegerField(
        "insumos disponibles (%)", default=0, validators=[MaxValueValidator(100)],
        help_text="Qué porcentaje de los insumos (etiquetas, ganchos, bolsas, cajas…) está disponible.")
    description = models.TextField("descripción", blank=True)
    notes = models.TextField("observaciones", blank=True)
    # Insumos: planeación lo envía, el equipo de insumos confirma, entrega y libera a planta
    sent_to_supplies_at = models.DateTimeField("enviado a insumos el", null=True, blank=True)
    supplies_released_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                             related_name="+", verbose_name="liberado a planta por")
    supplies_released_at = models.DateTimeField("liberado a planta el", null=True, blank=True)
    supplies_note = models.CharField("nota de insumos", max_length=255, blank=True,
                                     help_text="Motivo de devolución a planeación o de liberación con insumos incompletos.")
    # Logística: «mercar» = alistar la mercancía del pedido y dejarla ubicada para producción
    goods_status = models.CharField("mercancía", max_length=12, choices=GoodsStatus.choices, default=GoodsStatus.PENDING)
    goods_picked_by = models.CharField("mercado por", max_length=120, blank=True,
                                       help_text="Persona que mercó (no tiene que ser usuario del sistema).")
    goods_location = models.CharField("ubicación de la mercancía", max_length=120, blank=True)
    goods_note = models.TextField("observaciones de logística", blank=True)
    goods_marked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="+", verbose_name="marcado como mercado por")
    goods_marked_at = models.DateTimeField("mercado el", null=True, blank=True)
    # Datos de ventas (recepción del pedido)
    received_on = models.DateField("fecha de recepción del pedido", null=True, blank=True)
    delivery_date = models.DateField("fecha de entrega en el cliente", null=True, blank=True)
    transit_days = models.PositiveSmallIntegerField("días de tránsito", default=0,
                                                    help_text="Días de carretera desde la planta hasta el cliente.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name="registrado por")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "orden de producción"
        verbose_name_plural = "órdenes de producción"
        ordering = ["-created_at"]
        permissions = [
            ("plan_workorder", "Puede planear órdenes (etapas, tiempos y programación)"),
            ("release_to_plant", "Puede confirmar insumos y liberar pedidos a planta (equipo de insumos)"),
            ("mark_goods", "Puede marcar pedidos como mercados (logística)"),
        ]

    @property
    def dispatch_date(self):
        """Fecha en que el vehículo debe salir = entrega en el cliente − días de tránsito."""
        if not self.delivery_date:
            return None
        from datetime import timedelta
        return self.delivery_date - timedelta(days=self.transit_days)

    def __str__(self) -> str:
        return f"{self.number} · {self.product.sku} × {self.quantity}"

    def get_absolute_url(self) -> str:
        return reverse("mes_core:order_detail", args=[self.pk])

    def clean(self) -> None:
        if (self.process_type and registry.get_process_type(self.process_type) is None
                and not ServiceType.objects.filter(key=self.process_type).exists()):
            raise ValidationError({"process_type": "Tipo de servicio no registrado (créalo en Configuración → Tipos de servicio)."})
        if self.quantity is not None and self.quantity <= 0:
            raise ValidationError({"quantity": "Debe ser mayor que cero."})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.number:
            # El consecutivo sale del id para evitar choques entre usuarios concurrentes.
            self.number = f"OP-{self.pk:06d}"
            WorkOrder.objects.filter(pk=self.pk).update(number=self.number)

    @property
    def process_label(self) -> str:
        from .service_types import label_for

        return label_for(self.process_type) or "Sin tipo de servicio"

    @property
    def progress(self) -> int:
        if not self.quantity:
            return 0
        return min(100, int(self.quantity_done * 100 / self.quantity))

    @property
    def is_late(self) -> bool:
        return bool(self.due_date and self.planned_end and self.planned_end > self.due_date)


class PlanStage(models.Model):
    """Etapa definida por el equipo de planeación. Genera una o varias operaciones (lotes / saldo)."""

    class StdUnit(models.TextChoices):
        MIN_PER_UNIT = "min_unidad", "min / unidad"
        UNITS_PER_MIN = "unid_min", "unidades / min"
        UNITS_PER_HOUR = "unid_hora", "unidades / hora"

    class Mode(models.TextChoices):
        SHIFT = "turno", "Completar en 1 turno"
        DAYS = "dias", "Repartir en N días"
        CREW = "operarios", "Con N operarios"
        DEADLINE = "compromiso", "Terminar antes del compromiso"

    class QtyMode(models.TextChoices):
        ALL = "todo", "Todo el pedido"
        PARTIAL = "parcial", "Parcial"
        LOTS = "lotes", "Por lotes"

    class Link(models.TextChoices):
        QUEUE = "cola", "En cola: cuando termine la anterior"
        FLOW = "flujo", "A la par: en flujo con la anterior"
        PARALLEL = "paralelo", "Al mismo tiempo: independiente"

    class ReportMode(models.TextChoices):
        """Cómo llega la producción desde la planta (app de control de piso)."""
        COUNTER = "contador", "Contador: un toque por unidad"
        INTERVAL = "intervalo", "Cada operario reporta lo suyo cada cierto tiempo"
        SUPERNUMERARY = "supernumerario", "El supernumerario reporta por la línea"

    class SplitMode(models.TextChoices):
        TIME = "tiempo", "Según el tiempo trabajado"
        EQUAL = "iguales", "En partes iguales"

    order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="plan_stages", verbose_name="orden")
    sequence = models.PositiveSmallIntegerField("orden de la etapa")
    name = models.CharField("etapa", max_length=120)
    work_center = models.ForeignKey(WorkCenter, on_delete=models.PROTECT, related_name="+", verbose_name="línea")
    quantity = models.DecimalField("unidades", max_digits=12, decimal_places=3)
    std_value = models.DecimalField("tiempo estándar", max_digits=10, decimal_places=4)
    std_unit = models.CharField("unidad del estándar", max_length=12, choices=StdUnit.choices, default=StdUnit.MIN_PER_UNIT)
    setup_minutes = models.DecimalField("alistamiento (min)", max_digits=8, decimal_places=2, default=Decimal("0"))
    mode = models.CharField("cómo planear", max_length=12, choices=Mode.choices, default=Mode.SHIFT)
    mode_param = models.PositiveIntegerField("días / operarios", null=True, blank=True)
    qty_mode = models.CharField("cantidad", max_length=10, choices=QtyMode.choices, default=QtyMode.ALL)
    qty_param = models.DecimalField("unidades parcial / tamaño de lote", max_digits=12, decimal_places=3, null=True, blank=True)
    supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="supervisor", help_text="Vacío = automático por la regla de supervisor por línea y turno.")
    link = models.CharField(
        "relación con la etapa anterior", max_length=10, choices=Link.choices, default=Link.QUEUE,
        help_text="En cola = empieza al terminar la anterior. A la par = trabaja al tiempo con la anterior (le va llegando "
                  "producto) y no termina antes que ella. Al mismo tiempo = arranca con la anterior, sin depender de ella.")
    link_lag = models.DecimalField(
        "arranca cuando la anterior lleve (unidades)", max_digits=12, decimal_places=3, null=True, blank=True,
        help_text="Solo «A la par»: unidades que debe tener la etapa anterior para que esta empiece. Vacío = 1.")
    efficiency = models.PositiveSmallIntegerField(
        "rendimiento (%)", null=True, blank=True, validators=[MinValueValidator(10), MaxValueValidator(150)],
        help_text="Qué tan rápido trabaja la gente frente al estándar. Vacío = el de la línea o el de la planta.")
    report_mode = models.CharField(
        "forma de reportar en planta", max_length=16, choices=ReportMode.choices, blank=True,
        help_text="Vacío = la del tipo de proceso o la de la línea (Control de piso → Forma de reporte).")
    split_mode = models.CharField(
        "reparto de lo reportado", max_length=10, choices=SplitMode.choices, default=SplitMode.TIME,
        help_text="Cuando se reporta el total de la línea, cómo se reparte entre el personal de la etapa.")
    # Resultado del último cálculo (para listados e informes)
    man_hours = models.DecimalField("horas-hombre", max_digits=10, decimal_places=2, null=True, blank=True)
    crew = models.PositiveIntegerField("operarios", null=True, blank=True)
    duration_hours = models.DecimalField("duración (h)", max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = "etapa de planeación"
        verbose_name_plural = "etapas de planeación"
        ordering = ["order", "sequence"]

    def __str__(self) -> str:
        return f"{self.order.number} · {self.sequence}. {self.name}"


class Operation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pendiente", "Pendiente"
        IN_PROGRESS = "en_proceso", "En proceso"
        DONE = "terminada", "Terminada"

    order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="operations", verbose_name="orden")
    stage = models.ForeignKey(PlanStage, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="operations", verbose_name="etapa")
    quantity = models.DecimalField("unidades", max_digits=12, decimal_places=3, null=True, blank=True,
                                   help_text="Vacío = la cantidad de la orden.")
    crew = models.PositiveSmallIntegerField("operarios", default=1)
    sequence = models.PositiveSmallIntegerField("secuencia")
    name = models.CharField("operación", max_length=120)
    work_center_kind = models.CharField("tipo de centro", max_length=40, blank=True)
    work_center = models.ForeignKey(
        WorkCenter, on_delete=models.PROTECT, null=True, blank=True,
        related_name="operations", verbose_name="centro de trabajo",
    )
    setup_minutes = models.DecimalField("alistamiento (min)", max_digits=8, decimal_places=2, default=Decimal("0"))
    minutes_per_unit = models.DecimalField("min por unidad", max_digits=12, decimal_places=6, default=Decimal("0"))
    planned_start = models.DateTimeField("inicio planificado", null=True, blank=True)
    planned_end = models.DateTimeField("fin planificado", null=True, blank=True)
    actual_start = models.DateTimeField("inicio real", null=True, blank=True)
    actual_end = models.DateTimeField("fin real", null=True, blank=True)
    status = models.CharField("estado", max_length=20, choices=Status.choices, default=Status.PENDING)
    qty_good = models.DecimalField("cantidad buena", max_digits=12, decimal_places=3, default=Decimal("0"))
    qty_scrap = models.DecimalField("merma / rechazo", max_digits=12, decimal_places=3, default=Decimal("0"))
    qty_extra = models.DecimalField(
        "unidades de más", max_digits=12, decimal_places=3, default=Decimal("0"),
        help_text="Buenas producidas por encima de lo programado (se reportan aparte, con motivo).")
    qty_leftover = models.DecimalField(
        "códigos sobrantes", max_digits=12, decimal_places=3, default=Decimal("0"),
        help_text="Cuentan para completar lo programado (y la referencia), pero no como producción del operario.")
    changeover_minutes = models.DecimalField(
        "cambio de producto (min)", max_digits=8, decimal_places=2, default=Decimal("0"),
        help_text="Tiempo de cambio que el plan agregó porque la línea venía de otra familia de producto.")
    split_of = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="parts",
                                 verbose_name="parte de", help_text="Si la operación se dividió entre varias líneas.")
    # Responsable en piso: lo asigna planeación; el supervisor arma la cuadrilla (CrewAssignment).
    supervisor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="supervised_operations", verbose_name="supervisor responsable")
    supervisor_assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                               blank=True, related_name="+", verbose_name="supervisor asignado por")
    supervisor_assigned_at = models.DateTimeField("supervisor asignado el", null=True, blank=True)
    supervisor_auto = models.BooleanField(
        "supervisor por regla", default=False,
        help_text="Lo asignó la regla de supervisor por línea y turno; si la operación se mueve, se recalcula.")

    class Meta:
        verbose_name = "operación"
        verbose_name_plural = "operaciones"
        ordering = ["order", "sequence"]
        constraints = [models.UniqueConstraint(fields=["order", "sequence"], name="uniq_operation_sequence")]

    def __str__(self) -> str:
        return f"{self.order.number} / {self.sequence:02d} {self.name}"

    @property
    def qty(self) -> Decimal:
        return self.quantity if self.quantity is not None else self.order.quantity

    @property
    def qty_pending(self) -> Decimal:
        """Unidades que faltan para completar lo programado (los códigos sobrantes también cuentan)."""
        return max(Decimal("0"), self.qty - self.qty_good - self.qty_leftover)

    @property
    def planned_minutes(self) -> float:
        """Minutos hábiles de calendario: alistamiento + cambio + trabajo repartido entre la cuadrilla."""
        return float(self.setup_minutes + self.changeover_minutes + self.minutes_per_unit * self.qty / max(1, self.crew))

    @property
    def man_hours(self) -> float:
        return float(self.minutes_per_unit * self.qty + self.setup_minutes * max(1, self.crew)) / 60


class ProductionReport(models.Model):
    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="reports", verbose_name="operación")
    qty_good = models.DecimalField("cantidad buena", max_digits=12, decimal_places=3, default=Decimal("0"))
    qty_scrap = models.DecimalField("merma / rechazo", max_digits=12, decimal_places=3, default=Decimal("0"))
    qty_extra = models.DecimalField("unidades de más", max_digits=12, decimal_places=3, default=Decimal("0"),
                                    help_text="Buenas por encima de lo programado; no cuentan como cantidad pedida.")
    extra_reason = models.CharField("motivo de las unidades de más", max_length=255, blank=True)
    finished = models.BooleanField("operación terminada", default=False)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="reportado por"
    )
    reported_at = models.DateTimeField("fecha", auto_now_add=True)
    comment = models.CharField("comentario", max_length=255, blank=True)
    # Control de piso: las marcaciones de las tablets llegan como reportes del operario (o de la línea si employee es nulo)
    employee = models.ForeignKey("Employee", on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="production_reports", verbose_name="operario")
    source = models.CharField("origen", max_length=20, blank=True, default="",
                              help_text="Vacío = reporte hecho en el MES; «piso» = marcaciones de la tablet de planta.")
    ean = models.CharField("EAN (referencia)", max_length=14, blank=True, default="", db_index=True)
    qty_leftover = models.DecimalField(
        "códigos sobrantes", max_digits=12, decimal_places=3, default=Decimal("0"),
        help_text="Cuentan para completar la referencia y lo programado, pero no como producción del operario.")
    edited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+", verbose_name="editado por")
    edited_at = models.DateTimeField("editado el", null=True, blank=True)
    edit_reason = models.CharField("motivo de la edición", max_length=255, blank=True)

    class Meta:
        verbose_name = "reporte de producción"
        verbose_name_plural = "reportes de producción"
        ordering = ["-reported_at"]

    def __str__(self) -> str:
        return f"{self.operation} · +{self.qty_good}"


class VoidedReport(models.Model):
    """Reporte anulado. Sale de las sumas (se guarda aparte, con todo lo necesario para reactivarlo tal cual) y solo un
    reporte anulado se puede eliminar del todo."""

    order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="voided_reports", verbose_name="pedido")
    operation = models.ForeignKey(Operation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                  verbose_name="operación")
    original_id = models.PositiveIntegerField("número del reporte")
    operation_name = models.CharField("operación", max_length=120)
    ean = models.CharField("EAN", max_length=14, blank=True)
    qty_good = models.DecimalField("buenas", max_digits=12, decimal_places=3, default=Decimal("0"))
    qty_scrap = models.DecimalField("merma", max_digits=12, decimal_places=3, default=Decimal("0"))
    qty_leftover = models.DecimalField("sobrantes", max_digits=12, decimal_places=3, default=Decimal("0"))
    who = models.CharField("operario / quién reportó", max_length=160, blank=True)
    reported_at = models.DateTimeField("reportado el")
    data = models.JSONField("datos para reactivar", default=dict)
    reason = models.CharField("motivo de la anulación", max_length=255)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+", verbose_name="anulado por")
    voided_at = models.DateTimeField("anulado el", auto_now_add=True)

    class Meta:
        verbose_name = "reporte anulado"
        verbose_name_plural = "reportes anulados"
        ordering = ["-voided_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.order.number} · anulado #{self.original_id}"


class OrderClosure(models.Model):
    """Cierre de un pedido con faltante justificado: lo que no se pudo hacer queda registrado con su motivo y el pedido
    pasa a terminado con resultado «No cumple»."""

    order = models.OneToOneField(WorkOrder, on_delete=models.CASCADE, related_name="closure", verbose_name="pedido")
    reason = models.CharField("motivo", max_length=255)
    justified_by = models.CharField("justificado por", max_length=120)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="+", verbose_name="usuario")
    total = models.DecimalField("faltante total", max_digits=12, decimal_places=3, default=Decimal("0"))
    closed_at = models.DateTimeField("cerrado el", auto_now_add=True)

    class Meta:
        verbose_name = "cierre con faltante"
        verbose_name_plural = "cierres con faltante"

    def __str__(self) -> str:
        return f"{self.order.number} · faltan {self.total}"


class Shortfall(models.Model):
    """Lo que le faltó a cada etapa (y a cada referencia) al cerrar el pedido con faltante."""

    closure = models.ForeignKey(OrderClosure, on_delete=models.CASCADE, related_name="lines", verbose_name="cierre")
    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="shortfalls",
                                  verbose_name="operación")
    ean = models.CharField("EAN", max_length=14, blank=True)
    qty = models.DecimalField("faltante", max_digits=12, decimal_places=3)

    class Meta:
        verbose_name = "faltante justificado"
        verbose_name_plural = "faltantes justificados"

    def __str__(self) -> str:
        return f"{self.operation} · {self.ean or 'sin EAN'} · {self.qty}"


class Downtime(models.Model):
    """Paro planificado de una línea (mantenimiento, limpieza, sanitización…)."""

    work_center = models.ForeignKey(WorkCenter, on_delete=models.CASCADE, related_name="downtimes", verbose_name="línea")
    start = models.DateTimeField("inicio")
    end = models.DateTimeField("fin")
    reason = models.CharField("motivo", max_length=120)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "mantenimiento / paro"
        verbose_name_plural = "mantenimientos / paros"
        ordering = ["start"]

    def __str__(self) -> str:
        return f"{self.work_center.code}: {self.reason}"

    def clean(self) -> None:
        if self.start and self.end and self.end <= self.start:
            raise ValidationError({"end": "El fin debe ser posterior al inicio."})


class ScheduleChange(models.Model):
    """Bitácora de cambios hechos en el programador (pestaña "Cambios")."""

    class Action(models.TextChoices):
        PLACE = "programar", "Programada"
        MOVE = "mover", "Movida"
        RESIZE = "redimensionar", "Duración ajustada"
        EDIT = "editar", "Editada"
        PLAN = "planeacion", "Planeación de etapas"
        CONFIG = "turnos", "Configuración de turnos"
        ORDER = "pedido", "Pedido recibido"
        UNSCHEDULE = "devolver", "Devuelta al backlog"
        AUTO = "auto", "Plan automático"
        DOWNTIME = "mantenimiento", "Mantenimiento"
        IMPORT = "importar", "Importación"
        ATTENDANCE = "ausencias", "Ausencias"
        CALENDAR = "calendario", "Calendario"
        RULES = "reglas", "Reglas de planeación"
        VERSION = "version", "Versión del plan"
        SPLIT = "dividir", "División de operación"
        RESOURCES = "recursos", "Recursos"
        STAFF = "personal", "Personal y supervisores"
        SUPPLIES = "insumos", "Insumos y liberación a planta"
        GOODS = "mercancia", "Logística: mercancía"

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="schedule_changes")
    action = models.CharField(max_length=20, choices=Action.choices)
    description = models.CharField(max_length=255)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "cambio de programación"
        verbose_name_plural = "cambios de programación"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.description


class ShiftAttendance(models.Model):
    """Personal de un turno en un día: programados y ausencias (para la capacidad disponible)."""

    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name="attendance", verbose_name="turno")
    day = models.DateField("día")
    scheduled = models.PositiveIntegerField(
        "programados", null=True, blank=True,
        help_text="Vacío = los operarios del turno (o el total de la planta).")
    absent = models.PositiveIntegerField("ausencias", default=0)
    notes = models.CharField("observaciones", max_length=255, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "asistencia de turno"
        verbose_name_plural = "asistencia por turno"
        ordering = ["-day", "shift"]
        constraints = [models.UniqueConstraint(fields=["shift", "day"], name="uniq_attendance_shift_day")]

    def __str__(self) -> str:
        return f"{self.shift} · {self.day:%d/%m/%Y}: {self.absent} ausencias"


class CalendarException(models.Model):
    """Excepción al calendario normal de una planta en fechas puntuales."""

    class Kind(models.TextChoices):
        HOLIDAY = "festivo", "Festivo"
        OVERTIME = "horas_extra", "Horas extra"
        EXTRA_SHIFT = "turno_adicional", "Turno adicional"
        CLOSURE = "cierre", "Cierre / no laboral"

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="calendar_exceptions", verbose_name="planta")
    kind = models.CharField("tipo", max_length=20, choices=Kind.choices)
    date = models.DateField("fecha")
    date_to = models.DateField("hasta", null=True, blank=True, help_text="Opcional, para varios días seguidos.")
    name = models.CharField("nombre / motivo", max_length=120, blank=True)
    shift = models.ForeignKey(
        Shift, on_delete=models.CASCADE, null=True, blank=True, related_name="calendar_exceptions", verbose_name="turno",
        help_text="Horas extra: turno que se extiende. Cierre: turno que no trabaja (vacío = todo el día).")
    start_time = models.TimeField("desde", null=True, blank=True)
    end_time = models.TimeField("hasta (hora)", null=True, blank=True)
    operators = models.PositiveIntegerField(
        "operarios", default=0, help_text="Personas que trabajan en esa franja. 0 = las del turno o el total de la planta.")
    work_centers = models.ManyToManyField(WorkCenter, blank=True, related_name="calendar_exceptions", verbose_name="líneas",
                                          help_text="Vacío = todas las líneas.")
    auto = models.BooleanField("cargado automáticamente", default=False)
    order = models.ForeignKey("WorkOrder", on_delete=models.CASCADE, null=True, blank=True, related_name="plan_overtime",
                              verbose_name="pedido", help_text="Horas extra planeadas para este pedido desde la planeación por etapas.")
    stage_name = models.CharField("etapa", max_length=120, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "excepción de calendario"
        verbose_name_plural = "calendario: festivos y excepciones"
        ordering = ["date", "start_time"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.date:%d/%m/%Y}" + (f" · {self.name}" if self.name else "")

    @property
    def last_date(self):
        return self.date_to or self.date

    def days(self):
        from datetime import timedelta
        d = self.date
        while d <= self.last_date:
            yield d
            d += timedelta(days=1)

    def clean(self) -> None:
        if self.date_to and self.date and self.date_to < self.date:
            raise ValidationError("La fecha final no puede ser anterior a la inicial.")
        if self.date_to and self.date and (self.date_to - self.date).days > 366:
            raise ValidationError("El rango no puede superar un año.")
        if self.kind == self.Kind.OVERTIME:
            if not self.shift_id:
                raise ValidationError("Indica qué turno se extiende con horas extra.")
            if not (self.start_time and self.end_time):
                raise ValidationError("Indica desde y hasta qué hora son las horas extra.")
        if self.kind == self.Kind.EXTRA_SHIFT and not (self.start_time and self.end_time):
            raise ValidationError("Indica la hora de inicio y de fin del turno adicional.")
        if self.start_time and self.end_time and self.start_time == self.end_time:
            raise ValidationError("La hora de inicio y la de fin no pueden ser iguales.")
        if self.shift_id and self.site_id and self.shift.site_id != self.site_id:
            raise ValidationError("El turno pertenece a otra planta.")

    @property
    def time_label(self) -> str:
        return f"{self.start_time:%H:%M}–{self.end_time:%H:%M}" if self.start_time and self.end_time else ""

    @property
    def summary(self) -> str:
        t = self.time_label
        ops = f" · {self.operators} op." if self.operators else ""
        if self.kind == self.Kind.HOLIDAY:
            return self.name or "Festivo"
        if self.kind == self.Kind.OVERTIME:
            return f"{self.shift.name if self.shift else 'Turno'}: horas extra {t}{ops}"
        if self.kind == self.Kind.EXTRA_SHIFT:
            return f"{self.name or 'Turno adicional'} {t}{ops}"
        scope = self.shift.name if self.shift else "todo el día"
        return f"Cierre {scope}" + (f" · {self.name}" if self.name else "")


class PlanningRules(models.Model):
    """Reglas del plan automático de una planta (prioridades, plan congelado, cambios, división, aprobación)."""

    site = models.OneToOneField(Site, on_delete=models.CASCADE, related_name="planning_rules", verbose_name="planta")
    w_due = models.PositiveSmallIntegerField("peso: fecha compromiso", default=4, validators=[MaxValueValidator(10)])
    w_tier = models.PositiveSmallIntegerField("peso: nivel del cliente", default=3, validators=[MaxValueValidator(10)])
    w_age = models.PositiveSmallIntegerField("peso: antigüedad del pedido", default=1, validators=[MaxValueValidator(10)])
    w_priority = models.PositiveSmallIntegerField("peso: prioridad manual", default=5, validators=[MaxValueValidator(10)])
    frozen_hours = models.PositiveSmallIntegerField(
        "plan congelado (horas)", default=0, validators=[MaxValueValidator(336)],
        help_text="Lo que empieza dentro de estas horas no lo mueve el plan automático; moverlo a mano pide confirmación. 0 = sin congelar.")
    default_efficiency = models.PositiveSmallIntegerField(
        "rendimiento por defecto (%)", default=100, validators=[MinValueValidator(10), MaxValueValidator(150)],
        help_text="Rendimiento con el que se planea si la etapa o la línea no tienen uno propio. 100 = el estándar exacto.")
    overtime_efficiency = models.PositiveSmallIntegerField(
        "rendimiento en horas extra (%)", default=90, validators=[MinValueValidator(10), MaxValueValidator(100)],
        help_text="Cuánto rinde una hora extra frente a una hora normal (cansancio). Las sugerencias suman ese margen.")
    group_changeovers = models.BooleanField("agrupar productos de la misma familia", default=True)
    default_changeover_minutes = models.PositiveSmallIntegerField(
        "cambio entre familias (min)", default=0, help_text="Tiempo de cambio cuando no hay uno específico en la tabla.")
    allow_split = models.BooleanField("dividir operaciones largas entre líneas", default=False)
    split_threshold_hours = models.PositiveSmallIntegerField("dividir si dura más de (h)", default=16)
    require_approval = models.BooleanField(
        "liberar a planta solo con plan aprobado", default=False,
        help_text="El planeador propone una versión del plan; el jefe de planta la aprueba y ahí se libera.")
    auto_release = models.BooleanField(
        "liberación automática a planta", default=True,
        help_text="Cuando insumos entrega el 100 % de las líneas, sus insumos quedan liberados solos. El pedido pasa a "
                  "planta en cuanto insumos y logística (mercado) hayan liberado.")
    risk_tolerance_pct = models.PositiveSmallIntegerField(
        "tolerancia de atraso (%)", default=10, validators=[MaxValueValidator(90)],
        help_text="Un pedido queda «en riesgo» si lo reportado va más de este % por debajo de lo programado a la fecha.")
    attendance_grace_minutes = models.PositiveSmallIntegerField(
        "plazo para confirmar asistencia (min)", default=60, validators=[MaxValueValidator(600)],
        help_text="Minutos después del inicio del turno para que el supervisor confirme; luego se avisa al jefe de planta.")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "reglas de planeación"
        verbose_name_plural = "reglas de planeación"

    def __str__(self) -> str:
        return f"Reglas de {self.site}"


class ChangeoverTime(models.Model):
    """Minutos para cambiar una línea de una familia de producto a otra."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="changeover_times", verbose_name="planta")
    work_center_kind = models.CharField("tipo de línea", max_length=40, blank=True, help_text="Vacío = todas.")
    from_family = models.CharField("de familia", max_length=60)
    to_family = models.CharField("a familia", max_length=60)
    minutes = models.PositiveIntegerField("minutos")

    class Meta:
        verbose_name = "tiempo de cambio"
        verbose_name_plural = "tiempos de cambio"
        ordering = ["work_center_kind", "from_family", "to_family"]
        constraints = [models.UniqueConstraint(fields=["site", "work_center_kind", "from_family", "to_family"],
                                               name="uniq_changeover")]

    def __str__(self) -> str:
        return f"{self.from_family} → {self.to_family}: {self.minutes} min"


class PlanVersion(models.Model):
    """Foto del plan de una planta: para comparar versiones, medir cumplimiento y aprobar antes de liberar."""

    class Status(models.TextChoices):
        DRAFT = "borrador", "Guardada"
        PROPOSED = "propuesta", "Propuesta para aprobación"
        APPROVED = "aprobada", "Aprobada"
        REJECTED = "rechazada", "Rechazada"
        SUPERSEDED = "reemplazada", "Reemplazada"

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="plan_versions", verbose_name="planta")
    number = models.PositiveIntegerField("número")
    name = models.CharField("nombre", max_length=120, blank=True)
    note = models.TextField("comentario", blank=True)
    status = models.CharField("estado", max_length=20, choices=Status.choices, default=Status.DRAFT)
    data = models.JSONField("operaciones", default=list)
    summary = models.JSONField("resumen", default=dict)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField("comentario de aprobación", blank=True)

    class Meta:
        verbose_name = "versión del plan"
        verbose_name_plural = "versiones del plan"
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["site", "number"], name="uniq_planversion_number")]
        permissions = [("approve_plan", "Puede aprobar el plan y liberarlo a planta")]

    def __str__(self) -> str:
        return f"v{self.number} · {self.site.code}" + (f" · {self.name}" if self.name else "")


# ============================================================ Personal: operarios, polivalencia, asistencia
class Skill(models.Model):
    """Competencia de la matriz de polivalencia. Texto libre: el sistema la normaliza (minúsculas, sin tildes)."""

    name = models.CharField("competencia", max_length=80, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "competencia"
        verbose_name_plural = "competencias"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        from .normalize import normalize_key

        self.name = normalize_key(self.name)
        super().save(*args, **kwargs)

    def clean(self) -> None:
        from .normalize import normalize_key

        if not normalize_key(self.name):
            raise ValidationError({"name": "Escribe el nombre de la competencia."})


class Employee(models.Model):
    """Operario con nombre propio. Se carga a mano o masivamente desde Excel. El personal es GENERAL: no pertenece a una
    planta; cualquier supervisor lo ve en la lista y lo marca presente cuando trabaja con él."""

    site = models.ForeignKey(Site, on_delete=models.SET_NULL, null=True, blank=True, related_name="employees",
                             verbose_name="bodega habitual",
                             help_text="Dónde está si no tiene un traslado ni una cuadrilla en ese momento. El personal es "
                                       "una bolsa general: trabaja en cualquier bodega.")
    document = models.CharField("documento", max_length=20, unique=True,
                                help_text="Cédula u otro documento. Se guarda sin puntos ni espacios.")
    full_name = models.CharField("nombre completo", max_length=120)
    position = models.CharField("cargo", max_length=60, blank=True)
    shift = models.ForeignKey(Shift, on_delete=models.SET_NULL, null=True, blank=True, related_name="employees",
                              verbose_name="turno habitual")
    supervisor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="crew_members", verbose_name="supervisor",
                                   help_text="Supervisor que confirma su asistencia.")
    # Cuadrilla: al elegirlo para una operación queda con el supervisor de esa operación; al terminarla (o sacarlo de
    # la cuadrilla) vuelve a quedar libre, o con su supervisor habitual si tenía uno.
    crew_borrowed = models.BooleanField("con supervisor por cuadrilla", default=False)
    crew_operation = models.ForeignKey("Operation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                       verbose_name="cuadrilla actual",
                                       help_text="Operación por la que está con ese supervisor; al terminar queda libre.")
    home_supervisor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="+", verbose_name="supervisor habitual",
                                        help_text="Con quién vuelve a quedar al terminar la cuadrilla (vacío = libre).")
    phone = models.CharField("teléfono", max_length=30, blank=True)
    notes = models.TextField("conocimientos / observaciones", blank=True)
    active = models.BooleanField("activo", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "operario"
        verbose_name_plural = "operarios"
        ordering = ["full_name"]
        permissions = [
            ("supervise_crew", "Puede confirmar asistencia y asignar personal a sus operaciones (supervisor)"),
            ("assign_supervisor", "Puede asignar supervisores responsables a las operaciones"),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.document})"

    def save(self, *args, **kwargs):
        from .normalize import normalize_document, normalize_text

        self.document = normalize_document(self.document)
        self.full_name = normalize_text(self.full_name)
        self.position = normalize_text(self.position)
        self.phone = " ".join(str(self.phone or "").split())
        self.notes = normalize_text(self.notes, keep_newlines=True)
        super().save(*args, **kwargs)

    def clean(self) -> None:
        from .normalize import normalize_document, normalize_text

        if not normalize_document(self.document):
            raise ValidationError({"document": "Indica el documento."})
        if not normalize_text(self.full_name):
            raise ValidationError({"full_name": "Indica el nombre."})

    @property
    def display_name(self) -> str:
        from .normalize import display_name

        return display_name(self.full_name)


class EmployeeSkill(models.Model):
    """Celda de la matriz de polivalencia: nivel de un operario en una competencia."""

    class Level(models.IntegerChoices):
        NOVICE = 1, "Novato"
        INTERMEDIATE = 2, "Intermedio"
        EXPERT = 3, "Experto"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="skills", verbose_name="operario")
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="holders", verbose_name="competencia")
    level = models.PositiveSmallIntegerField("nivel", choices=Level.choices)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "nivel de competencia"
        verbose_name_plural = "matriz de polivalencia"
        ordering = ["employee", "skill"]
        constraints = [models.UniqueConstraint(fields=["employee", "skill"], name="uniq_employee_skill")]

    def __str__(self) -> str:
        return f"{self.employee.display_name} · {self.skill}: {self.get_level_display()}"


class Attendance(models.Model):
    """Asistencia confirmada por el supervisor: un registro por operario y día."""

    class Status(models.TextChoices):
        PRESENT = "presente", "Presente"
        LATE = "tarde", "Llegó tarde"
        ABSENT = "ausente", "Ausente"
        PERMIT = "permiso", "Permiso"
        SICK = "incapacidad", "Incapacidad"
        VACATION = "vacaciones", "Vacaciones"

    WORKING = (Status.PRESENT, Status.LATE)

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="attendance", verbose_name="operario")
    day = models.DateField("día")
    shift = models.ForeignKey(Shift, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                              verbose_name="turno")
    status = models.CharField("estado", max_length=20, choices=Status.choices)
    notes = models.CharField("observaciones", max_length=255, blank=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+", verbose_name="confirmado por")
    confirmed_at = models.DateTimeField("confirmado el", auto_now=True)

    class Meta:
        verbose_name = "asistencia de operario"
        verbose_name_plural = "asistencia de operarios"
        ordering = ["-day", "employee"]
        constraints = [models.UniqueConstraint(fields=["employee", "day"], name="uniq_attendance_employee_day")]

    def __str__(self) -> str:
        return f"{self.employee.display_name} · {self.day:%d/%m/%Y}: {self.get_status_display()}"

    @property
    def is_working(self) -> bool:
        return self.status in self.WORKING


class CrewAssignment(models.Model):
    """Operario asignado por el supervisor a una operación."""

    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="crew_assignments",
                                  verbose_name="operación")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="assignments", verbose_name="operario")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+", verbose_name="asignado por")
    assigned_at = models.DateTimeField("asignado el", auto_now_add=True)

    class Meta:
        verbose_name = "asignación de personal"
        verbose_name_plural = "asignaciones de personal"
        ordering = ["operation", "employee__full_name"]
        constraints = [models.UniqueConstraint(fields=["operation", "employee"], name="uniq_crew_operation_employee")]

    def __str__(self) -> str:
        return f"{self.operation} · {self.employee.display_name}"


# ============================================================ Bolsa global de personal: reparto y ubicación
class StaffAllocation(models.Model):
    """Reparto de la bolsa global de operarios: cuántas personas trabajan en una bodega (y turno).

    Sin fechas = el reparto base. Con fechas = un cambio para esos días (gana sobre el base). Un reparto con turno
    gana sobre uno de toda la bodega. Sin turno aplica a cada turno de la bodega. Donde no hay reparto se usan los
    operarios del turno o el total de la planta (configuración anterior)."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="allocations", verbose_name="bodega")
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, null=True, blank=True, related_name="allocations",
                              verbose_name="turno", help_text="Vacío = cada turno de la bodega.")
    date_from = models.DateField("desde", null=True, blank=True, help_text="Vacío = reparto base (siempre).")
    date_to = models.DateField("hasta", null=True, blank=True, help_text="Vacío = sin fin.")
    headcount = models.PositiveIntegerField("operarios")
    note = models.CharField("nota", max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "reparto de personal"
        verbose_name_plural = "reparto de personal"
        ordering = ["site", "date_from", "shift"]

    def __str__(self) -> str:
        when = "base" if not self.date_from else f"{self.date_from:%d/%m/%Y}" + (
            f"–{self.date_to:%d/%m/%Y}" if self.date_to and self.date_to != self.date_from else "")
        return f"{self.site} · {self.shift.name if self.shift_id else 'todos los turnos'} · {when}: {self.headcount}"

    def clean(self) -> None:
        if self.shift_id and self.site_id and self.shift.site_id != self.site_id:
            raise ValidationError({"shift": "El turno no es de esa bodega."})
        if self.date_to and not self.date_from:
            raise ValidationError({"date_from": "Indica desde cuándo."})
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValidationError({"date_to": "La fecha final es anterior a la inicial."})


class StaffMove(models.Model):
    """Dónde está una persona desde un momento: bodega (y línea). El traslado más reciente que cubre un momento manda;
    al terminar uno temporal, la persona vuelve a donde estaba. Sin fin = hasta nuevo aviso."""

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="moves", verbose_name="operario")
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="staff_moves", verbose_name="bodega")
    work_center = models.ForeignKey(WorkCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                    verbose_name="línea")
    start = models.DateTimeField("desde")
    end = models.DateTimeField("hasta", null=True, blank=True, help_text="Vacío = hasta nuevo aviso.")
    note = models.CharField("nota", max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name="movido por")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "ubicación de personal"
        verbose_name_plural = "ubicaciones de personal"
        ordering = ["-start"]
        indexes = [models.Index(fields=["employee", "start"])]

    def __str__(self) -> str:
        return f"{self.employee.display_name} → {self.site} desde {self.start:%d/%m %H:%M}"

    def clean(self) -> None:
        if self.work_center_id and self.site_id and self.work_center.site_id != self.site_id:
            raise ValidationError({"work_center": "La línea no es de esa bodega."})
        if self.end and self.start and self.end <= self.start:
            raise ValidationError({"end": "El fin debe ser posterior al inicio."})


# ============================================================ Insumos: inventario por planta y entregas a pedidos
class SupplyStock(models.Model):
    """Un insumo en el inventario de una planta (existencia y stock de seguridad)."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="supplies", verbose_name="planta")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stocks", verbose_name="insumo",
                                limit_choices_to=SUPPLY_ONLY)
    on_hand = models.DecimalField("existencia", max_digits=14, decimal_places=3, default=Decimal("0"))
    safety_stock = models.DecimalField("stock de seguridad", max_digits=14, decimal_places=3, default=Decimal("0"),
                                       help_text="Por debajo de esta cantidad el insumo se marca para reponer.")
    location = models.CharField("ubicación", max_length=60, blank=True)
    active = models.BooleanField("activo", default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "insumo en inventario"
        verbose_name_plural = "inventario de insumos"
        ordering = ["site", "product__name"]
        constraints = [models.UniqueConstraint(fields=["site", "product"], name="uniq_supply_site_product")]

    def __str__(self) -> str:
        return f"{self.product.sku} · {self.product.name} ({self.site.code})"


class SupplyRequirement(models.Model):
    """Insumo que necesita un pedido y lo que el equipo de insumos le ha entregado."""

    order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="supply_lines", verbose_name="pedido")
    stock = models.ForeignKey(SupplyStock, on_delete=models.PROTECT, related_name="requirements", verbose_name="insumo")
    operation = models.ForeignKey(Operation, on_delete=models.SET_NULL, null=True, blank=True, related_name="supply_lines",
                                  verbose_name="etapa", help_text="Vacío = para todo el pedido.")
    qty_required = models.DecimalField("requerido", max_digits=14, decimal_places=3)
    qty_released = models.DecimalField("entregado", max_digits=14, decimal_places=3, default=Decimal("0"))
    deliver_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name="entregado a (supervisor)")
    note = models.CharField("nota", max_length=255, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "insumo del pedido"
        verbose_name_plural = "insumos de los pedidos"
        ordering = ["order", "stock__product__name"]

    def __str__(self) -> str:
        return f"{self.order.number} · {self.stock.product.sku}: {self.qty_released}/{self.qty_required}"

    @property
    def qty_pending(self) -> Decimal:
        return max(Decimal("0"), self.qty_required - self.qty_released)


class SupplyMovement(models.Model):
    """Entrada, entrega a un pedido o ajuste de inventario (trazabilidad: no se editan)."""

    class Kind(models.TextChoices):
        IN = "entrada", "Entrada"
        OUT = "entrega", "Entrega a proceso"
        ADJUST = "ajuste", "Ajuste de inventario"

    stock = models.ForeignKey(SupplyStock, on_delete=models.CASCADE, related_name="movements", verbose_name="insumo")
    kind = models.CharField("tipo", max_length=12, choices=Kind.choices)
    qty = models.DecimalField("cantidad", max_digits=14, decimal_places=3, help_text="Positiva entra, negativa sale.")
    balance = models.DecimalField("existencia después", max_digits=14, decimal_places=3)
    order = models.ForeignKey(WorkOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="supply_movements",
                              verbose_name="pedido")
    requirement = models.ForeignKey(SupplyRequirement, on_delete=models.SET_NULL, null=True, blank=True, related_name="movements")
    deliver_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name="entregado a")
    note = models.CharField("nota", max_length=255, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                             verbose_name="registró")
    created_at = models.DateTimeField("fecha", auto_now_add=True)

    class Meta:
        verbose_name = "movimiento de insumo"
        verbose_name_plural = "movimientos de insumos"
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.qty:+} · {self.stock.product.sku}"


class BomLine(models.Model):
    """Lista de materiales: cuánto de un insumo lleva cada unidad del producto. Al enviar el pedido a insumos, las
    líneas de insumos se calculan solas (cantidad del pedido × cantidad por unidad + desperdicio)."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="bom_lines", verbose_name="producto",
                                limit_choices_to=PRODUCT_ONLY)
    component = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="used_in", verbose_name="insumo",
                                  limit_choices_to=SUPPLY_ONLY)
    qty_per_unit = models.DecimalField("cantidad por unidad", max_digits=14, decimal_places=6)
    scrap_pct = models.DecimalField("desperdicio (%)", max_digits=5, decimal_places=2, default=Decimal("0"),
                                    validators=[MaxValueValidator(100)])
    stage = models.CharField("etapa", max_length=60, blank=True,
                             help_text="Etapa o tipo de línea que lo consume (p. ej. etiquetado). Vacío = todo el pedido.")
    note = models.CharField("nota", max_length=200, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "línea de lista de materiales"
        verbose_name_plural = "listas de materiales"
        ordering = ["product__sku", "stage", "component__name"]
        constraints = [models.UniqueConstraint(fields=["product", "component", "stage"], name="uniq_bom_line")]

    def __str__(self) -> str:
        return f"{self.product.sku}: {self.qty_per_unit} {self.component.sku}/u"

    def save(self, *args, **kwargs):
        from .normalize import normalize_key

        self.stage = normalize_key(self.stage)
        super().save(*args, **kwargs)


class LineSupervisor(models.Model):
    """Regla: esta línea en este turno es responsabilidad de este supervisor. Las operaciones programadas en esa
    línea y turno quedan con ese supervisor sin que planeación lo asigne a mano."""

    work_center = models.ForeignKey(WorkCenter, on_delete=models.CASCADE, related_name="supervisor_rules",
                                    verbose_name="línea")
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name="supervisor_rules", verbose_name="turno")
    supervisor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="line_rules",
                                   verbose_name="supervisor")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "supervisor por línea y turno"
        verbose_name_plural = "supervisores por línea y turno"
        constraints = [models.UniqueConstraint(fields=["work_center", "shift"], name="uniq_line_shift_supervisor")]

    def __str__(self) -> str:
        return f"{self.work_center.code} · {self.shift.name} → {self.supervisor}"


class Notification(models.Model):
    """Aviso en la bandeja de un usuario (y por correo si la planta tiene correo configurado)."""

    class Kind(models.TextChoices):
        NEW_ORDER = "pedido_nuevo", "Pedido nuevo"
        TO_SUPPLIES = "a_insumos", "Pedido enviado a insumos"
        RELEASED = "a_planta", "Pedido liberado a planta"
        LOW_STOCK = "stock_bajo", "Insumo bajo stock de seguridad"
        ATTENDANCE = "asistencia", "Asistencia sin confirmar"
        AT_RISK = "en_riesgo", "Pedido en riesgo"
        SKILL = "polivalencia", "Sugerencia de polivalencia"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    site = models.ForeignKey(Site, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    kind = models.CharField("tipo", max_length=20, choices=Kind.choices)
    title = models.CharField("aviso", max_length=200)
    body = models.TextField("detalle", blank=True)
    url = models.CharField(max_length=300, blank=True)
    key = models.CharField(max_length=120, blank=True, help_text="Evita repetir el mismo aviso.")
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "aviso"
        verbose_name_plural = "avisos"
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["user", "read_at"])]
        constraints = [models.UniqueConstraint(fields=["user", "key"], condition=~models.Q(key=""),
                                               name="uniq_notification_key")]

    def __str__(self) -> str:
        return f"{self.user} · {self.title}"


class SkillReview(models.Model):
    """Lo que decidió el supervisor sobre una sugerencia de nivel (para no volver a proponerla)."""

    class Decision(models.TextChoices):
        APPROVED = "aprobada", "Aprobada"
        DISMISSED = "descartada", "Descartada"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="skill_reviews")
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="reviews")
    level = models.PositiveSmallIntegerField("nivel sugerido", choices=EmployeeSkill.Level.choices)
    hours = models.DecimalField("horas", max_digits=8, decimal_places=1)
    decision = models.CharField(max_length=12, choices=Decision.choices)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "revisión de polivalencia"
        verbose_name_plural = "revisiones de polivalencia"
        constraints = [models.UniqueConstraint(fields=["employee", "skill", "level"], name="uniq_skill_review")]
