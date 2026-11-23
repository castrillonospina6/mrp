import secrets

from django import forms
from django.contrib import admin, messages

from mes_core.models import Employee

from .models import (CheckIn, FloorAccess, IntervalReport, LineCode, Location, Mark, Message, OvertimeCall,
                     OvertimeDay, OvertimeRequest, ProcessReporting, Stop, SupernumeraryLine, Transfer)
from .views import pins_xlsx


class OvertimeDayInline(admin.TabularInline):
    model = OvertimeDay
    extra = 0


@admin.register(OvertimeCall)
class OvertimeCallAdmin(admin.ModelAdmin):
    list_display = ("title", "site", "status", "created_at", "sent_at", "respond_by")
    list_filter = ("status", "site")
    inlines = [OvertimeDayInline]
    filter_horizontal = ("sites", "shifts")


@admin.register(OvertimeRequest)
class OvertimeRequestAdmin(admin.ModelAdmin):
    list_display = ("employee", "call", "answer", "reason", "answered_at")
    list_filter = ("answer", "call")
    search_fields = ("employee__full_name", "employee__document")
    raw_id_fields = ("employee",)


@admin.register(CheckIn)
class CheckInAdmin(admin.ModelAdmin):
    list_display = ("employee", "day", "at", "work_center", "operation")
    list_filter = ("day", "work_center")
    search_fields = ("employee__full_name", "employee__document")
    raw_id_fields = ("employee", "operation")
    readonly_fields = ("info",)
    date_hierarchy = "day"


@admin.register(ProcessReporting)
class ProcessReportingAdmin(admin.ModelAdmin):
    list_display = ("name", "report_mode", "interval_minutes", "updated_at")
    search_fields = ("name",)


@admin.register(SupernumeraryLine)
class SupernumeraryLineAdmin(admin.ModelAdmin):
    list_display = ("employee", "work_center", "source", "assigned_by", "created_at")
    list_filter = ("source", "work_center")
    raw_id_fields = ("employee",)


@admin.register(IntervalReport)
class IntervalReportAdmin(admin.ModelAdmin):
    list_display = ("period_end", "operation", "work_center", "employee", "kind", "qty_good", "qty_scrap", "people")
    list_filter = ("kind", "work_center")
    date_hierarchy = "period_end"
    raw_id_fields = ("operation", "employee", "report")


def _grant(request, queryset, role):
    rows = []
    for emp in queryset.filter(active=True).order_by("full_name"):
        pin = f"{secrets.randbelow(10000):04d}"
        acc, _ = FloorAccess.objects.get_or_create(employee=emp, defaults={"role": role})
        acc.role, acc.active, acc.updated_by = role, True, request.user
        acc.set_pin(pin)
        acc.save()
        rows.append((emp.document, emp.display_name, acc.get_role_display(), pin))
    if not rows:
        messages.warning(request, "Ninguno de los seleccionados está activo.")
        return None
    return pins_xlsx(rows)


@admin.action(description="Tablet de piso: dar acceso y generar PIN (descarga Excel)", permissions=["change"])
def grant_operator(modeladmin, request, queryset):
    return _grant(request, queryset, FloorAccess.Role.OPERATOR)


@admin.action(description="Tablet de piso: dar acceso como PATINADOR y generar PIN (descarga Excel)",
              permissions=["change"])
def grant_mover(modeladmin, request, queryset):
    return _grant(request, queryset, FloorAccess.Role.MOVER)


@admin.action(description="Tablet de piso: dar acceso como SUPERNUMERARIO y generar PIN (descarga Excel)",
              permissions=["change"])
def grant_supernumerary(modeladmin, request, queryset):
    return _grant(request, queryset, FloorAccess.Role.SUPERNUMERARY)


@admin.action(description="Tablet de piso: quitar acceso", permissions=["change"])
def revoke(modeladmin, request, queryset):
    n = FloorAccess.objects.filter(employee__in=queryset).update(active=False)
    messages.info(request, f"Se quitó el acceso a la tablet a {n} operario(s).")


class FloorAccessForm(forms.ModelForm):
    """Apartado «Acceso a la tablet» dentro de la ficha del operario: se escribe un PIN o se genera uno al azar."""

    new_pin = forms.CharField(label="PIN nuevo", required=False, max_length=8,
                              widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off", "size": 10}),
                              help_text="De 4 a 8 números. Déjalo vacío y marca «Generar PIN al azar» para que el "
                                        "sistema lo cree. Al guardar se muestra arriba una sola vez.")
    generate = forms.BooleanField(label="Generar PIN al azar", required=False)

    class Meta:
        model = FloorAccess
        fields = ("role", "active")

    def clean_new_pin(self):
        pin = (self.cleaned_data.get("new_pin") or "").strip()
        if pin and not (pin.isdigit() and 4 <= len(pin) <= 8):
            raise forms.ValidationError("El PIN debe tener de 4 a 8 números.")
        return pin

    def save(self, commit=True):
        obj = super().save(commit=False)
        pin = self.cleaned_data.get("new_pin") or ""
        if not pin and (self.cleaned_data.get("generate") or not obj.pin_hash):
            pin = f"{secrets.randbelow(10000):04d}"
        self.generated_pin = pin
        if pin:
            obj.set_pin(pin)
        if commit:
            obj.save()
        return obj


class FloorAccessInline(admin.StackedInline):
    model = FloorAccess
    form = FloorAccessForm
    fk_name = "employee"
    can_delete = False
    max_num = 1
    extra = 1
    verbose_name = "acceso a la tablet de piso (PIN)"
    verbose_name_plural = "acceso a la tablet de piso (PIN)"
    fields = ("role", "active", "new_pin", "generate")


@admin.display(description="tablet")
def tablet_col(obj):
    acc = getattr(obj, "floor_access", None)
    if acc is None:
        return "—"
    if not acc.active:
        return "Sin acceso"
    return {FloorAccess.Role.MOVER: "Patinador", FloorAccess.Role.SUPERNUMERARY: "Supernumerario"}.get(acc.role, "Con PIN")


# El personal se administra en Operarios: ahí se dan los PIN, igual que el resto de datos del personal
# (en la ficha de cada operario, apartado «Acceso a la tablet», o a muchos a la vez con las acciones de la lista).
_emp_admin = admin.site._registry.get(Employee)
if _emp_admin is not None:
    _emp_admin.actions = [*_emp_admin.actions, grant_operator, grant_mover, grant_supernumerary, revoke]
    _emp_admin.inlines = [*_emp_admin.inlines, FloorAccessInline]
    _emp_admin.list_display = (*_emp_admin.list_display, tablet_col)
    _emp_admin.list_select_related = (*(_emp_admin.list_select_related or ()), "floor_access")
    _orig_save_formset = _emp_admin.save_formset

    def _save_formset(request, form, formset, change, _orig=_orig_save_formset):
        _orig(request, form, formset, change)
        for f in getattr(formset, "forms", []):
            pin = getattr(f, "generated_pin", "")
            if pin and isinstance(f, FloorAccessForm):
                if f.instance.updated_by_id is None:
                    FloorAccess.objects.filter(pk=f.instance.pk).update(updated_by=request.user)
                messages.warning(request, f"PIN de tablet de {form.instance.display_name} "
                                          f"(documento {form.instance.document}): {pin}. Anótalo y entrégaselo: "
                                          "no se vuelve a mostrar.")

    _emp_admin.save_formset = _save_formset


@admin.register(FloorAccess)
class FloorAccessAdmin(admin.ModelAdmin):
    list_display = ("employee", "role", "active", "updated_at", "updated_by")
    list_editable = ("role", "active")
    list_filter = ("role", "active")
    search_fields = ("employee__full_name", "employee__document")
    autocomplete_fields = ("employee",)
    readonly_fields = ("updated_at", "updated_by")
    actions = ["new_pin"]

    def has_add_permission(self, request):
        return False  # se crean desde Operarios con la acción «dar acceso y generar PIN»

    @admin.action(description="Generar PIN nuevo (descarga Excel)", permissions=["change"])
    def new_pin(self, request, queryset):
        return _grant_access_rows(request, queryset)


def _grant_access_rows(request, queryset):
    rows = []
    for acc in queryset.select_related("employee"):
        pin = f"{secrets.randbelow(10000):04d}"
        acc.set_pin(pin)
        acc.updated_by = request.user
        acc.save()
        rows.append((acc.employee.document, acc.employee.display_name, acc.get_role_display(), pin))
    return pins_xlsx(rows)


@admin.register(LineCode)
class LineCodeAdmin(admin.ModelAdmin):
    list_display = ("work_center", "pair_code", "pause_code", "report_mode", "interval_minutes", "updated_at")
    search_fields = ("work_center__code", "work_center__name")
    readonly_fields = ("updated_at",)


@admin.register(Mark)
class MarkAdmin(admin.ModelAdmin):
    list_display = ("at", "employee", "work_center", "operation", "kind", "qty")
    list_filter = ("kind", "work_center")
    date_hierarchy = "at"
    search_fields = ("employee__full_name", "operation__order__number")
    raw_id_fields = ("operation", "employee", "report")


@admin.register(Stop)
class StopAdmin(admin.ModelAdmin):
    list_display = ("start", "end", "employee", "work_center", "cause", "is_break", "closed_by")
    list_filter = ("is_break", "cause", "work_center")
    date_hierarchy = "start"
    raw_id_fields = ("operation", "employee")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "sent_by", "audience", "text")
    date_hierarchy = "created_at"


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "site", "distance", "work_center", "active")
    list_filter = ("site", "active")


@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = ("created_at", "origin", "destination", "mover", "pallets", "status", "urgent", "delivered_at")
    list_filter = ("status", "site", "urgent")
    date_hierarchy = "created_at"
    raw_id_fields = ("mover", "order")
