"""Control de piso: lo que pasa en las tablets de planta (marcaciones, paros, pausas, mensajes y traslados).

Los datos maestros son los del MES: el operario es `Employee`, la línea (módulo) es `WorkCenter`, la tarea es la
`Operation` a la que el supervisor lo asignó en su cuadrilla y el lote es el pedido (`WorkOrder`). Cada marcación de
unidades buenas o rechazos se vuelve un reporte de producción del MES en el mismo instante.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.contrib.auth.hashers import PBKDF2PasswordHasher, check_password
from django.db import models
from django.utils import timezone

from django.core.validators import MaxValueValidator, MinValueValidator

from mes_core.models import Employee, Operation, PlanStage, ProductionReport, Site, WorkCenter, WorkOrder

REPORT_MODES = PlanStage.ReportMode
INTERVAL_VALIDATORS = [MinValueValidator(5), MaxValueValidator(480)]

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin 0/O ni 1/I para que se puedan dictar


def new_code(n: int = 8) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(n))


def new_pause_code() -> str:
    return new_code(12)


class PinHasher(PBKDF2PasswordHasher):
    """PIN de la tablet: PBKDF2 con sal, con menos vueltas que la contraseña de un usuario del MES.

    Con las 1,2 millones de vueltas de Django, comprobar un PIN tardaba 0,5 a 2 s en cada entrada. Un PIN de 4 a 8
    números no gana protección real con tantas vueltas (lo que lo protege es el límite de intentos por minuto), así que
    se usan 60 000: entrar con el PIN es instantáneo y el PIN sigue guardado sin poder leerse."""

    algorithm = "pbkdf2_pin"
    iterations = 60_000


PIN_HASHER = PinHasher()


class FloorAccess(models.Model):
    """Acceso de un operario a la tablet: documento + PIN. Lo crea el administrador (como el resto del personal)."""

    class Role(models.TextChoices):
        OPERATOR = "operario", "Operario"
        MOVER = "patinador", "Patinador (traslados)"
        SUPERNUMERARY = "supernumerario", "Supernumerario (reporta por las líneas)"

    employee = models.OneToOneField(Employee, on_delete=models.CASCADE, related_name="floor_access",
                                    verbose_name="operario")
    role = models.CharField("rol en la tablet", max_length=20, choices=Role.choices, default=Role.OPERATOR)
    pin_hash = models.CharField(max_length=128, editable=False)
    active = models.BooleanField("activo", default=True)
    last_seen = models.DateTimeField("última conexión", null=True, blank=True, editable=False,
                                     help_text="Última vez que su app habló con el servidor (con la app abierta, cada ~20 s).")
    sound_ok = models.BooleanField("sonido de alarmas activo", null=True, blank=True, editable=False,
                                   help_text="Si el equipo donde está conectado deja sonar las alarmas solo.")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")

    ONLINE_SECONDS = 90  # con la app abierta en pantalla se reporta cada ~20 s

    class Meta:
        verbose_name = "acceso a la tablet"
        verbose_name_plural = "accesos a la tablet (PIN)"
        ordering = ["employee__full_name"]

    def __str__(self) -> str:
        return f"{self.employee.display_name} · {self.get_role_display()}"

    def set_pin(self, pin: str) -> None:
        self.pin_hash = PIN_HASHER.encode(str(pin), PIN_HASHER.salt())

    def check_pin(self, pin: str) -> bool:
        h = self.pin_hash
        if not h:
            return False
        if h.startswith(PIN_HASHER.algorithm + "$"):
            return PIN_HASHER.verify(str(pin), h)
        ok = check_password(str(pin), h)  # PIN guardado antes (formato lento): al acertar se pasa al formato rápido
        if ok and self.pk:
            from mes_core.dbqueue import write_turn

            self.set_pin(pin)
            with write_turn():
                type(self).objects.filter(pk=self.pk).update(pin_hash=self.pin_hash)
        return ok


class LineCode(models.Model):
    """Códigos de una línea: con el de vinculación una tablet queda fija como «tablet de la línea»; el de pausas va
    impreso como QR en la línea y el operario lo escanea para volver del desayuno o almuerzo."""

    work_center = models.OneToOneField(WorkCenter, on_delete=models.CASCADE, related_name="floor_code",
                                       verbose_name="línea")
    pair_code = models.CharField("código de la tablet", max_length=12, unique=True, default=new_code)
    pause_code = models.CharField("código del QR de pausas", max_length=16, default=new_pause_code)
    report_mode = models.CharField("forma de reportar", max_length=16, choices=REPORT_MODES.choices, blank=True,
                                   help_text="Vacío = contador por toque (módulos y mesas).")
    interval_minutes = models.PositiveSmallIntegerField("reportar cada (min)", default=60,
                                                        validators=INTERVAL_VALIDATORS)
    tablet_seen = models.DateTimeField("tablet conectada por última vez", null=True, blank=True, editable=False)
    tablet_sound = models.BooleanField("sonido de la tablet activo", null=True, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "código de línea"
        verbose_name_plural = "códigos de línea (tablets y QR)"
        ordering = ["work_center__site", "work_center__code"]

    def __str__(self) -> str:
        return f"{self.work_center.code} · tablet {self.pair_code}"

    @classmethod
    def for_line(cls, wc: WorkCenter) -> "LineCode":
        obj, _ = cls.objects.get_or_create(work_center=wc)
        return obj


class ProcessReporting(models.Model):
    """Forma de reportar de un tipo de proceso (por el nombre de la etapa: Limpieza, Aspersión…). Pesa más que la de
    la línea; la que planeación elija en la etapa de un pedido pesa más que esta."""

    key = models.CharField(max_length=120, unique=True, editable=False)
    name = models.CharField("proceso (nombre de la etapa)", max_length=120)
    report_mode = models.CharField("forma de reportar", max_length=16, choices=REPORT_MODES.choices)
    interval_minutes = models.PositiveSmallIntegerField("reportar cada (min)", null=True, blank=True,
                                                        validators=INTERVAL_VALIDATORS,
                                                        help_text="Vacío = el de la línea.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "forma de reportar de un proceso"
        verbose_name_plural = "formas de reportar por proceso"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name}: {self.get_report_mode_display()}"

    @staticmethod
    def key_of(name: str) -> str:
        return " ".join(str(name or "").split()).lower()[:120]

    def save(self, *args, **kwargs):
        self.name = " ".join(self.name.split())
        self.key = self.key_of(self.name)
        super().save(*args, **kwargs)


class OvertimeCall(models.Model):
    """Convocatoria de horas extra que arma planeación en el MES: días habilitados (entre semana, sábados, domingos,
    festivos) con su franja. Al enviarla, a cada persona le aparece en la tablet una pantalla que no la deja seguir
    hasta que responda SÍ (con los días y hasta qué hora puede) o NO (con el motivo)."""

    class Status(models.TextChoices):
        DRAFT = "borrador", "Borrador"
        SENT = "enviada", "Enviada"
        CLOSED = "cerrada", "Cerrada"

    site = models.ForeignKey(Site, on_delete=models.CASCADE, null=True, blank=True, related_name="overtime_calls",
                             verbose_name="planta", help_text="Vacía = convocatoria para todas las bodegas.")
    title = models.CharField("nombre", max_length=120)
    note = models.TextField("mensaje para el personal", blank=True)
    sites = models.ManyToManyField(Site, blank=True, related_name="+", verbose_name="bodegas",
                                   help_text="Vacío = todo el personal de todas las bodegas con acceso a la tablet.")
    shifts = models.ManyToManyField("mes_core.Shift", blank=True, related_name="+", verbose_name="turnos",
                                    help_text="Vacío = todos los turnos.")
    status = models.CharField("estado", max_length=10, choices=Status.choices, default=Status.DRAFT)
    respond_by = models.DateTimeField("responder hasta", null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "convocatoria de horas extra"
        verbose_name_plural = "convocatorias de horas extra"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


class OvertimeDay(models.Model):
    """Día habilitado para horas extra y su franja (de qué hora a qué hora)."""

    call = models.ForeignKey(OvertimeCall, on_delete=models.CASCADE, related_name="days", verbose_name="convocatoria")
    date = models.DateField("fecha")
    start = models.TimeField("desde")
    end = models.TimeField("hasta")
    needed = models.PositiveIntegerField("personas que se necesitan", default=0, help_text="0 = sin tope.")
    holiday = models.BooleanField("festivo", default=False)

    class Meta:
        verbose_name = "día de horas extra"
        verbose_name_plural = "días de horas extra"
        ordering = ["date", "start"]
        constraints = [models.UniqueConstraint(fields=["call", "date"], name="uniq_overtime_day")]

    def __str__(self) -> str:
        return f"{self.date:%d/%m} {self.start:%H:%M}–{self.end:%H:%M}"

    @property
    def kind(self) -> str:
        if self.holiday:
            return "Festivo"
        return {5: "Sábado", 6: "Domingo"}.get(self.date.weekday(), "Entre semana")


class OvertimeRequest(models.Model):
    """La convocatoria para una persona y su respuesta."""

    class Answer(models.TextChoices):
        PENDING = "pendiente", "Sin responder"
        YES = "si", "Sí"
        NO = "no", "No"

    call = models.ForeignKey(OvertimeCall, on_delete=models.CASCADE, related_name="requests",
                             verbose_name="convocatoria")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="overtime_requests",
                                 verbose_name="operario")
    answer = models.CharField("respuesta", max_length=10, choices=Answer.choices, default=Answer.PENDING)
    reason = models.CharField("motivo (si dijo que no)", max_length=255, blank=True)
    answered_at = models.DateTimeField("respondió el", null=True, blank=True)

    class Meta:
        verbose_name = "respuesta a horas extra"
        verbose_name_plural = "respuestas a horas extra"
        constraints = [models.UniqueConstraint(fields=["call", "employee"], name="uniq_overtime_request")]

    def __str__(self) -> str:
        return f"{self.employee.display_name} · {self.call}: {self.get_answer_display()}"


class OvertimeAnswer(models.Model):
    """Un día que la persona eligió y hasta qué hora puede quedarse."""

    request = models.ForeignKey(OvertimeRequest, on_delete=models.CASCADE, related_name="answers")
    day = models.ForeignKey(OvertimeDay, on_delete=models.CASCADE, related_name="answers")
    until = models.TimeField("hasta qué hora puede")

    class Meta:
        verbose_name = "día elegido para horas extra"
        verbose_name_plural = "días elegidos para horas extra"
        constraints = [models.UniqueConstraint(fields=["request", "day"], name="uniq_overtime_answer")]


class CheckIn(models.Model):
    """Llegada de la persona a la planta: lo primero que hace en la tablet. Ve a dónde va (bodega, línea, operación,
    supervisor y supernumerario) y al aceptar confirma su asistencia del día (queda «Presente» en el MES)."""

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="floor_checkins",
                                 verbose_name="operario")
    day = models.DateField("día")
    at = models.DateTimeField("confirmó a las", default=timezone.now)
    work_center = models.ForeignKey(WorkCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                    verbose_name="línea")
    operation = models.ForeignKey(Operation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                  verbose_name="operación")
    info = models.JSONField("lo que se le mostró", default=dict, blank=True)

    class Meta:
        verbose_name = "llegada confirmada en la tablet"
        verbose_name_plural = "llegadas confirmadas en la tablet"
        ordering = ["-at"]
        constraints = [models.UniqueConstraint(fields=["employee", "day"], name="uniq_checkin_employee_day")]

    def __str__(self) -> str:
        return f"{self.employee.display_name} · {timezone.localtime(self.at):%d/%m %H:%M}"


class SupernumeraryLine(models.Model):
    """Línea por la que reporta un supernumerario. Además de estas ve las de su supervisor (las operaciones que tiene
    a cargo el supervisor de su ficha)."""

    class Source(models.TextChoices):
        MES = "mes", "Asignada en el MES"
        TABLET = "tablet", "Elegida en la tablet"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="sup_lines",
                                 verbose_name="supernumerario")
    work_center = models.ForeignKey(WorkCenter, on_delete=models.CASCADE, related_name="+", verbose_name="línea")
    source = models.CharField("origen", max_length=8, choices=Source.choices, default=Source.MES)
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "línea de un supernumerario"
        verbose_name_plural = "líneas de los supernumerarios"
        ordering = ["employee__full_name", "work_center__code"]
        constraints = [models.UniqueConstraint(fields=["employee", "work_center", "source"], name="uniq_piso_sup_line")]

    def __str__(self) -> str:
        return f"{self.employee.display_name} · {self.work_center.code} ({self.get_source_display()})"


class IntervalReport(models.Model):
    """Reporte de lo producido en un tramo de tiempo (líneas continuas). Lo hace el operario por sí mismo, o el
    supernumerario / la tablet de la línea por toda la línea (el total se reparte entre el personal o se escribe por
    persona). Entra al MES como un reporte de producción; si ese reporte se borra en el MES, este también."""

    class Kind(models.TextChoices):
        OWN = "propio", "El operario reporta lo suyo"
        LINE = "linea", "Total de la línea, repartido"
        PEOPLE = "persona", "Por persona"

    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="interval_reports",
                                  verbose_name="operación")
    work_center = models.ForeignKey(WorkCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                    verbose_name="línea")
    employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                 verbose_name="reportado por", help_text="Vacío = la tablet de la línea.")
    kind = models.CharField("tipo", max_length=8, choices=Kind.choices)
    split_mode = models.CharField("reparto", max_length=10, blank=True)
    period_start = models.DateTimeField("desde")
    period_end = models.DateTimeField("hasta", db_index=True)
    qty_good = models.DecimalField("buenas", max_digits=12, decimal_places=3, default=0)
    qty_scrap = models.DecimalField("rechazos", max_digits=12, decimal_places=3, default=0)
    people = models.PositiveSmallIntegerField("personas", default=1)
    report = models.ForeignKey(ProductionReport, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "reporte por intervalo"
        verbose_name_plural = "reportes por intervalo"
        ordering = ["-period_end"]

    def __str__(self) -> str:
        return f"{self.operation} · {self.qty_good:g} ({self.get_kind_display()})"


class Mark(models.Model):
    """Una marcación en la tablet: unidad buena, rechazo o reproceso."""

    class Kind(models.TextChoices):
        GOOD = "p", "Buena"
        SCRAP = "x", "Rechazo"
        REWORK = "w", "Reproceso"

    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="floor_marks",
                                  verbose_name="operación")
    work_center = models.ForeignKey(WorkCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                    verbose_name="línea")
    employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="floor_marks",
                                 verbose_name="operario", help_text="Vacío = marcó la tablet de la línea.")
    kind = models.CharField("tipo", max_length=1, choices=Kind.choices, default=Kind.GOOD)
    qty = models.DecimalField("cantidad", max_digits=12, decimal_places=3, default=1)
    minutes_per_unit = models.DecimalField("min estándar por unidad", max_digits=12, decimal_places=6, default=0)
    at = models.DateTimeField("hora", default=timezone.now, db_index=True)
    since = models.DateTimeField("desde", null=True, blank=True,
                                 help_text="Si viene de un reporte por intervalo: inicio del tramo reportado.")
    report = models.ForeignKey(ProductionReport, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    interval = models.ForeignKey(IntervalReport, on_delete=models.CASCADE, null=True, blank=True, related_name="marks")

    class Meta:
        verbose_name = "marcación"
        verbose_name_plural = "marcaciones"
        ordering = ["-at"]
        indexes = [models.Index(fields=["employee", "at"]), models.Index(fields=["work_center", "at"])]

    def __str__(self) -> str:
        who = self.employee.display_name if self.employee_id else "Línea"
        return f"{who} · {self.get_kind_display()} {self.qty:g} · {self.operation}"

    @property
    def std_minutes(self) -> float:
        return float(self.qty * self.minutes_per_unit) if self.kind == self.Kind.GOOD else 0.0


class Stop(models.Model):
    """Paro registrado en piso (o pausa de desayuno / almuerzo). Abierto mientras no tenga fin."""

    BREAKS = ("Desayuno", "Almuerzo")

    work_center = models.ForeignKey(WorkCenter, on_delete=models.CASCADE, null=True, blank=True,
                                    related_name="floor_stops", verbose_name="línea")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, null=True, blank=True, related_name="floor_stops",
                                 verbose_name="operario", help_text="Vacío = paro de toda la línea.")
    operation = models.ForeignKey(Operation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                  verbose_name="operación")
    cause = models.CharField("motivo", max_length=60)
    is_break = models.BooleanField("pausa (desayuno / almuerzo)", default=False)
    planned_end = models.DateTimeField("fin previsto de la pausa", null=True, blank=True)
    start = models.DateTimeField("inicio", default=timezone.now, db_index=True)
    end = models.DateTimeField("fin", null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+", verbose_name="cerrado por (supervisor)")

    class Meta:
        verbose_name = "paro de piso"
        verbose_name_plural = "paros de piso"
        ordering = ["-start"]

    def __str__(self) -> str:
        who = self.employee.display_name if self.employee_id else (self.work_center.code if self.work_center_id else "—")
        return f"{who} · {self.cause}"

    def minutes(self, since=None, until=None) -> float:
        now = timezone.now()
        a = max(self.start, since) if since else self.start
        b = min(self.end or now, until or now)
        return max(0.0, (b - a).total_seconds() / 60)


class Message(models.Model):
    """Mensaje de producción del supervisor a la planta: aparece encima de la pantalla del operario."""

    text = models.CharField("mensaje", max_length=500)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+",
                                verbose_name="enviado por")
    sent_by_employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                         verbose_name="enviado por (supernumerario)")
    audience = models.CharField("para", max_length=120, blank=True)
    work_center = models.ForeignKey(WorkCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                    verbose_name="línea", help_text="Si va a una línea, también lo ve su tablet.")
    alarm = models.BooleanField("con alarma (zumbido)", default=True,
                                help_text="Suena una sirena con zumbido y vibra hasta que lo acepten.")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "mensaje a la planta"
        verbose_name_plural = "mensajes a la planta"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.text[:60]


class MessageRecipient(models.Model):
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="recipients")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="floor_messages")
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["message", "employee"], name="uniq_piso_msg_employee")]


# ------------------------------------------------------------------ traslados (patinadores)
class Location(models.Model):
    """Ubicación de la planta para los traslados (bodega, corte, frente de una línea…)."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="floor_locations", verbose_name="planta")
    name = models.CharField("nombre", max_length=80)
    distance = models.PositiveSmallIntegerField("distancia", default=1, choices=[(1, "Cerca"), (2, "Media"), (3, "Lejana")],
                                                help_text="Pondera los puntos del patinador.")
    work_center = models.ForeignKey(WorkCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                    verbose_name="es el frente de la línea")
    active = models.BooleanField("activa", default=True)

    class Meta:
        verbose_name = "ubicación"
        verbose_name_plural = "ubicaciones (traslados)"
        ordering = ["site", "name"]
        constraints = [models.UniqueConstraint(fields=["site", "name"], name="uniq_piso_location")]

    def __str__(self) -> str:
        return self.name


class Transfer(models.Model):
    """Traslado de estibas. Pasa por pendiente → aceptada → cargada → entregada; el servidor pone la hora de cada paso."""

    class Status(models.TextChoices):
        PENDING = "p", "Pendiente"
        ACCEPTED = "a", "Aceptada"
        LOADED = "c", "Cargada"
        DELIVERED = "e", "Entregada"

    INCIDENTS = ("Estiba dañada", "Ruta bloqueada", "No encontré la estiba")

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="floor_transfers", verbose_name="planta")
    mover = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="floor_transfers",
                              verbose_name="patinador", help_text="Vacío = abierto: cualquier patinador lo puede tomar.")
    origin = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                               verbose_name="origen")
    destination = models.ForeignKey(Location, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                    verbose_name="destino")
    work_center = models.ForeignKey(WorkCenter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                    verbose_name="línea que lo pide")
    order = models.ForeignKey(WorkOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                              verbose_name="pedido / lote")
    pallets = models.PositiveSmallIntegerField("estibas", default=1)
    note = models.CharField("nota", max_length=200, blank=True)
    urgent = models.BooleanField("urgente", default=False)
    required_at = models.DateTimeField("para las", null=True, blank=True)
    from_line = models.BooleanField("pedido desde la línea", default=False)
    status = models.CharField("estado", max_length=1, choices=Status.choices, default=Status.PENDING)
    incident = models.CharField("incidencia", max_length=60, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    loaded_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    requested_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                     verbose_name="pedido por (tablet)",
                                     help_text="El supernumerario que lo pidió desde su tablet.")

    class Meta:
        verbose_name = "traslado"
        verbose_name_plural = "traslados"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.origin or '—'} → {self.destination or self.work_center or '—'} · {self.get_status_display()}"

    @property
    def route_key(self) -> str:
        return f"{self.origin_id or ''}>{self.destination_id or ''}"

    @property
    def weight(self) -> int:
        """Estibas × distancia (la mayor de origen y destino): base de los puntos del patinador."""
        d = max(self.origin.distance if self.origin_id else 1, self.destination.distance if self.destination_id else 1)
        return self.pallets * d


class MoverAssignment(models.Model):
    """Patinador de una operación: lo elige el supervisor al armar la cuadrilla. Mientras la operación esté abierta,
    esa persona ve la app como patinador (aunque su acceso sea de operario) y los pedidos de material de la línea le
    llegan a él. No es parte de la cuadrilla: no cuenta en la producción ni en el reparto."""

    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="mover_assignments",
                                  verbose_name="operación")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="mover_assignments",
                                 verbose_name="patinador")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+", verbose_name="asignado por")
    assigned_at = models.DateTimeField("asignado el", auto_now_add=True)

    class Meta:
        verbose_name = "patinador de la operación"
        verbose_name_plural = "patinadores de las operaciones"
        constraints = [models.UniqueConstraint(fields=["operation", "employee"], name="uniq_mover_operation_employee")]

    def __str__(self) -> str:
        return f"{self.operation} · patinador {self.employee.display_name}"
