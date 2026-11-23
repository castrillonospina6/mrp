from django import forms

from .models import Customer, Employee, Operation, Site, WorkOrder
from .plugins import registry


class DateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%dT%H:%M", **kwargs)


class ProductionReportForm(forms.Form):
    operation = forms.ModelChoiceField(label="Operación", queryset=Operation.objects.none())
    qty_good = forms.DecimalField(label="Cantidad buena", min_value=0, decimal_places=3, initial=0)
    qty_scrap = forms.DecimalField(label="Merma / rechazo", min_value=0, decimal_places=3, initial=0)
    ean = forms.CharField(label="Referencia (EAN)", required=False, max_length=20,
                          widget=forms.TextInput(attrs={"list": "order-eans", "inputmode": "numeric",
                                                        "autocomplete": "off", "placeholder": "Escanea o elige"}))
    qty_leftover = forms.DecimalField(label="Códigos sobrantes", min_value=0, decimal_places=3, initial=0, required=False,
                                      help_text="Completan la referencia, pero no cuentan como producción.")
    allow_extra = forms.BooleanField(
        label="Reportar unidades de más", required=False,
        help_text="Solo si se produjo por encima de lo programado. Lo que falta completa la operación y el excedente "
                  "queda registrado aparte, con su motivo.")
    extra_reason = forms.CharField(label="Motivo de las unidades de más", required=False, max_length=255,
                                   widget=forms.TextInput(attrs={"placeholder": "Ej.: sobrante de material, pedido del cliente"}))
    finished = forms.BooleanField(label="Operación terminada", required=False)
    comment = forms.CharField(label="Comentario", required=False, max_length=255)

    def __init__(self, *args, order, **kwargs):
        super().__init__(*args, **kwargs)
        ops = order.operations.exclude(status=Operation.Status.DONE)
        self.fields["operation"].queryset = ops
        fmt = lambda d: f"{d.normalize():f}" if d == d.to_integral() else f"{d:g}"  # noqa: E731
        self.fields["operation"].label_from_instance = (
            lambda op: f"{op.sequence}. {op.name} · faltan {fmt(op.qty_pending)} de {fmt(op.qty)}")
        # Para el aviso en vivo: {id: [programadas, buenas ya reportadas]}
        self.pending = {op.pk: [float(op.qty), float(op.qty_good)] for op in ops}


class PlanForm(forms.Form):
    site = forms.ModelChoiceField(label="Planta", queryset=Site.objects.filter(active=True))
    start = forms.DateTimeField(label="Planificar desde", required=False, widget=DateTimeInput())


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class TimeInput(forms.TimeInput):
    input_type = "time"

    def __init__(self, **kwargs):
        super().__init__(format="%H:%M", **kwargs)


class OrderIntakeForm(forms.Form):
    """Recepción del pedido (equipo de ventas). Las etapas las define planeación después."""

    site = forms.ModelChoiceField(label="Planta", queryset=Site.objects.filter(active=True))
    customer = forms.ModelChoiceField(label="Cliente", queryset=Customer.objects.filter(active=True), required=False)
    new_customer = forms.CharField(label="…o cliente nuevo", required=False, max_length=120)
    product = forms.CharField(
        label="Producto", max_length=200,
        help_text="Escribe el SKU o el nombre. Si no existe, se crea automáticamente.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "placeholder": "Ej. CAM-001 o Camiseta básica talla M"}),
    )
    process_type = forms.CharField(
        label="Tipo de servicio", required=False, max_length=80,
        help_text="Elige de la lista o escríbelo. Si es nuevo se agrega a la lista. Puede quedar en blanco.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "placeholder": "Elige o escribe… (opcional)",
                                      "role": "combobox", "aria-autocomplete": "list", "aria-expanded": "false"}))
    quantity = forms.DecimalField(label="Unidades a procesar", min_value=0.001, decimal_places=3)
    received_on = forms.DateField(label="Fecha de recepción del pedido", widget=DateInput())
    delivery_date = forms.DateField(label="Fecha de entrega en el cliente", widget=DateInput())
    transit_days = forms.IntegerField(label="Días de tránsito en carretera", min_value=0, max_value=60, initial=0)
    reference = forms.CharField(label="Referencia", required=False, max_length=60)
    purchase_order = forms.CharField(label="OC (orden de compra)", required=False, max_length=60)
    external_op = forms.CharField(label="Número de OP", required=False, max_length=60,
                                  help_text="El número interno del sistema se asigna solo.")
    shipment = forms.CharField(label="Embarque", required=False, max_length=60)
    priority = forms.TypedChoiceField(label="Prioridad", coerce=int, initial=3, choices=[
        (1, "1 · Urgente"), (2, "2 · Alta"), (3, "3 · Media"), (4, "4 · Baja"), (5, "5 · Muy baja")])
    description = forms.CharField(label="Descripción", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    notes = forms.CharField(label="Observaciones", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def clean_product(self):
        text = " ".join(self.cleaned_data["product"].split())
        if not text:
            raise forms.ValidationError("Escribe el producto.")
        return text

    def __init__(self, *args, **kwargs):
        from .service_types import active_types

        super().__init__(*args, **kwargs)
        self.service_options = active_types()

    def clean_process_type(self):
        from .service_types import clean_name

        return clean_name(self.cleaned_data.get("process_type"))

    def clean(self):
        data = super().clean()
        if data.get("customer") and data.get("new_customer"):
            self.add_error("new_customer", "Elige un cliente existente o escribe uno nuevo, no ambos.")
        return data


class OrderEditForm(forms.ModelForm):
    """Editar el pedido desde la aplicación (antes solo se podía en Administración)."""

    PRIORITIES = [(1, "1 · Urgente"), (2, "2 · Alta"), (3, "3 · Media"), (4, "4 · Baja"), (5, "5 · Muy baja")]
    priority = forms.TypedChoiceField(label="Prioridad", coerce=int, choices=PRIORITIES)
    process_type = forms.ChoiceField(label="Tipo de servicio", required=False)

    class Meta:
        model = WorkOrder
        fields = ["site", "customer", "product", "process_type", "quantity", "priority", "due_date", "received_on",
                  "delivery_date", "transit_days", "reference", "purchase_order", "external_op", "shipment",
                  "goods_pct", "supplies_pct", "status", "description", "notes"]
        widgets = {"due_date": DateTimeInput(), "received_on": DateInput(), "delivery_date": DateInput(),
                   "description": forms.Textarea(attrs={"rows": 3}), "notes": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"status": "Cámbialo solo para corregir: el flujo normal lo mueve solo (insumos, planta, reportes).",
                      "quantity": "Si cambias la cantidad de un pedido ya planeado, vuelve a planearlo para ajustar "
                                  "las operaciones."}

    def __init__(self, *args, **kwargs):
        from .service_types import active_types

        super().__init__(*args, **kwargs)
        current = self.instance.process_type or ""
        choices = [("", "Sin tipo de servicio")] + [(t.key, t.name) for t in active_types()]
        if current and current not in dict(choices):
            choices.append((current, current))
        self.fields["process_type"].choices = choices
        self.fields["customer"].queryset = Customer.objects.filter(active=True) | Customer.objects.filter(
            pk=self.instance.customer_id or 0)
        self.fields["site"].queryset = Site.objects.filter(active=True) | Site.objects.filter(pk=self.instance.site_id or 0)
        if self.instance.pk and self.instance.operations.exists():
            self.fields["site"].disabled = True
            self.fields["site"].help_text = "Tiene operaciones en líneas de esta planta: para cambiarla, vuelve a planear."


def parse_time_rows(data, prefix: str, label: str) -> list[tuple]:
    """Lee filas repetidas «<prefix>_start» / «<prefix>_end» (HH:MM) de un formulario. Filas vacías se ignoran."""
    from datetime import datetime

    out = []
    for raw_a, raw_b in zip(data.getlist(f"{prefix}_start"), data.getlist(f"{prefix}_end")):
        raw_a, raw_b = (raw_a or "").strip(), (raw_b or "").strip()
        if not raw_a and not raw_b:
            continue
        if not raw_a or not raw_b:
            raise forms.ValidationError(f"Indica inicio y fin de cada horario de {label}, o deja ambos vacíos.")
        try:
            a, b = (datetime.strptime(v[:5], "%H:%M").time() for v in (raw_a, raw_b))
        except ValueError:
            raise forms.ValidationError(f"Hora inválida en el {label}: usa HH:MM.") from None
        if a == b:
            raise forms.ValidationError(f"El {label} {raw_a}–{raw_b} no dura nada.")
        out.append((a, b))
    return out


class ShiftConfigForm(forms.Form):
    first_start = forms.TimeField(label="Hora de inicio del turno", widget=TimeInput())
    duration_hours = forms.DecimalField(label="Duración del turno (horas)", min_value=0.5, max_value=24, decimal_places=2)
    end_time = forms.TimeField(label="Hora de finalización", widget=TimeInput(), required=False)
    count = forms.IntegerField(label="Número de turnos", min_value=1, max_value=4)
    # Desayunos y almuerzos: varias filas breakfast_start/breakfast_end (grupos escalonados); ver clean().
    operators_total = forms.IntegerField(
        label="Total de operarios de la planta", min_value=0, max_value=100000, initial=0,
        help_text="Personas disponibles para producir en cada turno. 0 = sin límite.",
    )
    # Opcional por turno (vacío o 0 = usa el total)
    operators_1 = forms.IntegerField(required=False, min_value=0)
    operators_2 = forms.IntegerField(required=False, min_value=0)
    operators_3 = forms.IntegerField(required=False, min_value=0)
    operators_4 = forms.IntegerField(required=False, min_value=0)

    def __init__(self, *args, groups=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._initial_groups = groups or {"breakfast": [], "lunch": []}

    def clean(self):
        data = super().clean()
        for kind, label in (("breakfast", "desayuno"), ("lunch", "almuerzo")):
            try:
                data[f"{kind}_groups"] = parse_time_rows(self.data, kind, label)
            except forms.ValidationError as exc:
                self.add_error(None, exc)
        return data

    def group_rows(self, kind: str) -> list[tuple[str, str]]:
        """Filas a pintar: lo enviado (si hubo error) o lo guardado."""
        if self.is_bound:
            return list(zip(self.data.getlist(f"{kind}_start"), self.data.getlist(f"{kind}_end")))
        return [(f"{a:%H:%M}", f"{b:%H:%M}") for a, b in self._initial_groups.get(kind, [])]

    def operators_list(self) -> list[int]:
        d = self.cleaned_data
        return [d.get(f"operators_{k}") or 0 for k in range(1, d["count"] + 1)]


class EmployeeForm(forms.ModelForm):
    """Alta / edición de un operario. Los textos se normalizan al guardar (minúsculas, sin tildes)."""

    class Meta:
        model = Employee
        fields = ["document", "full_name", "position", "shift", "supervisor", "phone", "notes", "active"]  # personal general
        widgets = {"notes": forms.Textarea(attrs={"rows": 3,
                                                  "placeholder": "Ej.: manejo de montacargas, RF, BPM, curso de alturas…"})}

    def __init__(self, *args, team_of=None, **kwargs):
        from .models import Shift
        from .staffing import supervisors, user_label

        super().__init__(*args, **kwargs)
        self.fields["shift"].queryset = Shift.objects.filter(active=True).select_related("site").order_by("site__code", "start_time")
        self.fields["shift"].label_from_instance = lambda s: f"{s.site.code} · {s.name} ({s.start_time:%H:%M}-{s.end_time:%H:%M})"
        self.fields["supervisor"].queryset = supervisors()
        self.fields["supervisor"].label_from_instance = user_label
        self.fields["supervisor"].help_text = "Usuarios con el rol Supervisor."
        if team_of is not None:  # un supervisor solo carga operarios a su propio equipo
            sup = self.fields["supervisor"]
            from django.contrib.auth import get_user_model

            sup.queryset = get_user_model().objects.filter(pk=team_of.pk)
            sup.disabled = True
            sup.initial = team_of.pk
            self.initial["supervisor"] = team_of.pk
            sup.help_text = "Queda en tu equipo."

    def clean_document(self):
        from .normalize import normalize_document

        doc = normalize_document(self.cleaned_data.get("document"))
        if not doc:
            raise forms.ValidationError("Indica el documento.")
        if Employee.objects.filter(document=doc).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"Ya existe un operario con el documento {doc}.")
        return doc
