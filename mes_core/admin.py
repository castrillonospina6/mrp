from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone

from . import services, staffing
from .models import (
    Attendance, BomLine, CalendarException, ChangeoverTime, CrewAssignment, Customer, Downtime, Employee, EmployeeSkill,
    LineSupervisor, Notification, Operation, PlanningRules, PlanStage, PlanVersion, ProductionReport, Product,
    ScheduleChange, ServiceType, Shift, ShiftAttendance, Site, StaffAllocation, StaffMove, Skill, SkillReview, SupplyItem, SupplyMovement,
    SupplyRequirement, SupplyStock, WorkCenter, WorkOrder,
)
from .plugins import registry

MAX_UPLOAD_MB = 10


class ShiftInline(admin.TabularInline):
    model = Shift
    extra = 0
    fields = ("name", "start_time", "end_time", "breakfast_start", "breakfast_end", "lunch_start", "lunch_end",
              "operators", "weekdays", "is_overlap", "color", "active")
    readonly_fields = ()
    verbose_name_plural = "Turnos (los horarios escalonados de desayuno y almuerzo se editan en Configuración → Turnos y personal)"


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "kind", "active")
    list_filter = ("kind", "active")
    inlines = [ShiftInline]


@admin.register(WorkCenter)
class WorkCenterAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "site", "kind", "capacity", "efficiency", "shift_list", "active")
    list_editable = ("efficiency",)
    list_filter = ("site", "kind", "active")
    search_fields = ("code", "name", "kind")
    filter_horizontal = ("shifts",)

    @admin.display(description="turnos")
    def shift_list(self, obj):
        return ", ".join(s.name for s in obj.shifts.all()) or "todos los de la planta"

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "shifts":
            kwargs["queryset"] = Shift.objects.select_related("site")
        return super().formfield_for_manytomany(db_field, request, **kwargs)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "tier", "active")
    list_filter = ("tier", "active")
    search_fields = ("name",)


def _uses(product) -> tuple[list[str], list[str]]:
    """(usos como producto, usos como insumo) de un artículo."""
    as_product, as_supply = [], []
    if product.orders.exists():
        as_product.append("pedidos")
    if product.bom_lines.exists():
        as_product.append("su lista de materiales")
    if product.stocks.exists():
        as_supply.append("inventario de insumos")
    if product.used_in.exists():
        as_supply.append("listas de materiales")
    try:
        from plugins.copacking.models import PackagingMaterial, PackagingSpec

        if PackagingSpec.objects.filter(product=product).exists():
            as_product.append("ficha de acondicionado")
        if PackagingMaterial.objects.filter(material=product).exists():
            as_supply.append("fichas de acondicionado")
    except (ImportError, LookupError):  # módulo de acondicionado no instalado
        pass
    return as_product, as_supply


def _convert(modeladmin, request, queryset, kind):
    done, blocked = 0, []
    for p in queryset:
        as_product, as_supply = _uses(p)
        uses = as_product if kind == Product.Kind.SUPPLY else as_supply
        if uses:
            blocked.append(f"{p.sku} (está en {', '.join(uses)})")
            continue
        Product.objects.filter(pk=p.pk).update(kind=kind)
        done += 1
    label = "insumo" if kind == Product.Kind.SUPPLY else "producto"
    if done:
        modeladmin.message_user(request, f"{done} artículo(s) pasaron a {label}.", messages.SUCCESS)
    if blocked:
        modeladmin.message_user(request, "No se cambiaron porque ya se usan como el otro tipo: " + "; ".join(blocked),
                                messages.WARNING)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """Solo productos (lo que se procesa en las órdenes). Los insumos están en «Insumos (catálogo)»."""

    list_display = ("sku", "name", "unit", "family", "active")
    list_filter = ("unit", "active", "family")
    search_fields = ("sku", "name")
    exclude = ("kind",)
    actions = ["to_supply"]

    def get_queryset(self, request):
        return super().get_queryset(request).filter(kind=Product.Kind.PRODUCT)

    def save_model(self, request, obj, form, change):
        obj.kind = Product.Kind.PRODUCT
        super().save_model(request, obj, form, change)

    @admin.action(description="Es un insumo: pasarlo a Insumos (catálogo)", permissions=["change"])
    def to_supply(self, request, queryset):
        _convert(self, request, queryset, Product.Kind.SUPPLY)


@admin.register(SupplyItem)
class SupplyItemAdmin(admin.ModelAdmin):
    """Solo insumos (lo que se consume: etiquetas, bolsas, cajas…). Nunca aparecen como producto de un pedido."""

    list_display = ("sku", "name", "unit", "active")
    list_filter = ("unit", "active")
    search_fields = ("sku", "name")
    fields = ("sku", "name", "unit", "active")
    actions = ["to_product"]

    @admin.action(description="Es un producto: pasarlo a Productos", permissions=["change"])
    def to_product(self, request, queryset):
        _convert(self, request, queryset, Product.Kind.PRODUCT)


class OperationInline(admin.TabularInline):
    model = Operation
    extra = 0
    fields = ("sequence", "name", "work_center_kind", "work_center", "quantity", "crew", "setup_minutes",
              "minutes_per_unit", "planned_start", "planned_end", "status", "qty_good", "qty_scrap")
    readonly_fields = ("planned_start", "planned_end", "qty_good", "qty_scrap")


class PlanStageInline(admin.TabularInline):
    model = PlanStage
    extra = 0
    fields = ("sequence", "name", "work_center", "quantity", "std_value", "std_unit", "setup_minutes", "mode",
              "mode_param", "qty_mode", "qty_param", "man_hours", "crew", "duration_hours")
    readonly_fields = ("man_hours", "crew", "duration_hours")
    verbose_name_plural = "Etapas de planeación (edítalas desde Planeación)"


class WorkOrderAdminForm(forms.ModelForm):
    process_type = forms.ChoiceField(label="Tipo de servicio", choices=(), required=False)

    class Meta:
        model = WorkOrder
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        from .service_types import sync

        super().__init__(*args, **kwargs)
        sync()
        current = self.instance.process_type if self.instance else ""
        choices = [("", "— Sin tipo de servicio —")] + [
            (st.key, st.name + ("" if st.active else " (inactivo)")) for st in ServiceType.objects.order_by("name")]
        if current and current not in {k for k, _ in choices}:
            choices.append((current, current))
        self.fields["process_type"].choices = choices


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    form = WorkOrderAdminForm
    list_display = ("number", "site", "product", "customer", "process_type", "quantity", "quantity_done", "status",
                    "priority", "due_date", "planned_end")
    list_filter = ("site", "status", "process_type", "customer__tier")
    search_fields = ("number", "product__sku", "product__name", "reference", "purchase_order", "external_op",
                     "shipment", "customer__name")
    autocomplete_fields = ("customer",)
    readonly_fields = ("number", "quantity_done", "planned_start", "planned_end", "created_by")
    actions = ["release"]

    def get_inlines(self, request, obj):
        # Núcleo + inlines aportados por los plugins.
        return [PlanStageInline, OperationInline, *registry.order_admin_inlines()]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change and registry.get_process_type(obj.process_type):  # sin ruta estándar: las etapas las define planeación
            services.generate_operations(obj)

    @admin.action(description="Liberar órdenes seleccionadas")
    def release(self, request, queryset):
        for order in queryset:
            try:
                services.release_order(order)
            except Exception as exc:  # noqa: BLE001 - se muestra al usuario
                self.message_user(request, f"{order.number}: {exc}", messages.WARNING)


@admin.register(Downtime)
class DowntimeAdmin(admin.ModelAdmin):
    list_display = ("work_center", "reason", "start", "end", "created_by")
    list_filter = ("work_center__site", "work_center")
    date_hierarchy = "start"


@admin.register(ScheduleChange)
class ScheduleChangeAdmin(admin.ModelAdmin):
    list_display = ("created_at", "site", "action", "description", "user")
    list_filter = ("site", "action")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ProductionReport)
class ProductionReportAdmin(admin.ModelAdmin):
    list_display = ("operation", "qty_good", "qty_extra", "qty_scrap", "finished", "reported_by", "employee", "source",
                    "reported_at", "extra_reason")
    list_filter = ("finished", "source")
    date_hierarchy = "reported_at"

    def has_change_permission(self, request, obj=None):
        return False  # los reportes son inmutables (trazabilidad)


@admin.register(StaffAllocation)
class StaffAllocationAdmin(admin.ModelAdmin):
    list_display = ("site", "shift", "date_from", "date_to", "headcount", "note", "created_by", "updated_at")
    list_filter = ("site",)
    list_editable = ("headcount",)
    autocomplete_fields = ()
    readonly_fields = ("created_by", "updated_at")

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(StaffMove)
class StaffMoveAdmin(admin.ModelAdmin):
    list_display = ("employee", "site", "work_center", "start", "end", "note", "created_by")
    list_filter = ("site",)
    search_fields = ("employee__full_name", "employee__document")
    date_hierarchy = "start"
    raw_id_fields = ("employee",)
    readonly_fields = ("created_by", "created_at")

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(ShiftAttendance)
class ShiftAttendanceAdmin(admin.ModelAdmin):
    list_display = ("day", "shift", "scheduled", "absent", "notes", "updated_by", "updated_at")
    list_filter = ("shift__site", "shift")
    date_hierarchy = "day"
    readonly_fields = ("updated_by", "updated_at")


# Marca del panel de administración (reemplaza "Administración de Django")
admin.site.site_header = "MES Planta"
admin.site.site_title = "MES Planta · Administración"
admin.site.index_title = "Administración del sistema"
admin.site.site_url = "/"



@admin.register(CalendarException)
class CalendarExceptionAdmin(admin.ModelAdmin):
    list_display = ("date", "date_to", "site", "kind", "name", "shift", "start_time", "end_time", "operators", "auto")
    list_filter = ("site", "kind", "auto")
    date_hierarchy = "date"
    filter_horizontal = ("work_centers",)
    search_fields = ("name",)


@admin.register(PlanningRules)
class PlanningRulesAdmin(admin.ModelAdmin):
    list_display = ("site", "w_due", "w_tier", "w_age", "w_priority", "frozen_hours", "group_changeovers", "allow_split",
                    "require_approval", "auto_release")


@admin.register(ChangeoverTime)
class ChangeoverTimeAdmin(admin.ModelAdmin):
    list_display = ("site", "work_center_kind", "from_family", "to_family", "minutes")
    list_filter = ("site", "work_center_kind")


@admin.register(PlanVersion)
class PlanVersionAdmin(admin.ModelAdmin):
    list_display = ("number", "site", "name", "status", "created_by", "created_at", "decided_by", "decided_at")
    list_filter = ("site", "status")
    readonly_fields = ("data", "summary", "created_by", "created_at", "decided_by", "decided_at")


@admin.register(SupplyStock)
class SupplyStockAdmin(admin.ModelAdmin):
    list_display = ("product", "site", "on_hand", "safety_stock", "location", "active")
    list_filter = ("site", "active")
    search_fields = ("product__sku", "product__name", "location")
    readonly_fields = ("on_hand",)  # la existencia cambia solo con movimientos (entradas, entregas, ajustes)


@admin.register(SupplyMovement)
class SupplyMovementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "stock", "qty", "balance", "order", "deliver_to", "user", "note")
    list_filter = ("kind", "stock__site")
    search_fields = ("stock__product__sku", "stock__product__name", "order__number", "note")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SupplyRequirement)
class SupplyRequirementAdmin(admin.ModelAdmin):
    list_display = ("order", "stock", "operation", "qty_required", "qty_released", "deliver_to")
    search_fields = ("order__number", "stock__product__sku", "stock__product__name")
    readonly_fields = ("qty_released",)


@admin.register(ServiceType)
class ServiceTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "plugin", "active")
    list_filter = ("active", "plugin")
    list_editable = ("active",)
    search_fields = ("name", "key")


# ---------------------------------------------------------------- Personal
class EmployeeSkillInline(admin.TabularInline):
    model = EmployeeSkill
    extra = 0
    fields = ("skill", "level", "updated_by", "updated_at")
    readonly_fields = ("updated_by", "updated_at")
    autocomplete_fields = ("skill",)


def _shift_label(shift) -> str:
    return f"{shift.site.code} · {shift}"


class BulkSupervisorForm(forms.Form):
    supervisor = forms.ModelChoiceField(queryset=None, required=False, label="Supervisor",
                                        empty_label="— Quitar supervisor —")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["supervisor"]
        field.queryset = staffing.supervisors()
        field.label_from_instance = staffing.user_label


class BulkShiftForm(forms.Form):
    shift = forms.ModelChoiceField(queryset=None, required=False, label="Turno", empty_label="— Quitar turno —")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["shift"]
        field.queryset = Shift.objects.filter(active=True).select_related("site").order_by("site__code", "start_time")
        field.label_from_instance = _shift_label  # el personal es general: turnos de todas las plantas


class BulkSkillForm(forms.Form):
    skill = forms.ModelChoiceField(queryset=Skill.objects.order_by("name"), required=False, label="Competencia",
                                   empty_label="— Elegir —")
    new_skill = forms.CharField(required=False, max_length=80, label="… o escribe una nueva",
                                help_text="Se normaliza sola (minúsculas, sin tildes).")
    level = forms.ChoiceField(label="Nivel", choices=[*((str(v), l) for v, l in EmployeeSkill.Level.choices),
                                                      ("0", "Quitar esta competencia")])

    def clean(self):
        from .normalize import normalize_key

        data = super().clean()
        name = normalize_key(data.get("new_skill") or "")
        if name:
            data["skill"] = Skill.objects.filter(name=name).first() or Skill(name=name)
        if not data.get("skill"):
            raise forms.ValidationError("Elige una competencia o escribe una nueva.")
        return data


def _ids(queryset) -> list[int]:
    return list(queryset.values_list("pk", flat=True))


def _plural(n: int, word: str = "operario") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    """Única puerta para cargar personal (uno a uno o desde Excel). El personal es GENERAL (todas las plantas).
    Supervisor y turno se cambian directo en la lista; las acciones asignan supervisor, turno o competencia a muchos."""

    change_list_template = "admin/mes_core/employee/change_list.html"
    list_display = ("document", "full_name", "position", "shift", "supervisor", "skills_col", "active")
    list_display_links = ("document", "full_name")
    list_editable = ("shift", "supervisor", "active")
    list_filter = ("active", "shift", "supervisor", "skills__skill")
    list_select_related = ("shift", "shift__site", "supervisor")
    exclude = ("site", "crew_borrowed", "crew_operation", "home_supervisor")
    list_per_page = 100
    search_fields = ("document", "full_name", "position")
    inlines = [EmployeeSkillInline]
    actions = ["bulk_supervisor", "bulk_shift", "bulk_skill", "bulk_activate", "bulk_deactivate"]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("skills__skill")

    def save_model(self, request, obj, form, change):
        if "supervisor" in getattr(form, "changed_data", ()):  # elegido a mano: deja de ser préstamo de cuadrilla
            obj.crew_borrowed, obj.crew_operation, obj.home_supervisor = False, None, None
        super().save_model(request, obj, form, change)

    @admin.display(description="competencias")
    def skills_col(self, obj):
        short = {1: "N", 2: "I", 3: "E"}
        return ", ".join(f"{name} ({short.get(lvl, lvl)})" for name, lvl in staffing.employee_skill_summary(obj)) or "—"

    # ------------------------------------------------------------ desplegables (lista y ficha)
    def _in_changelist(self, request) -> bool:
        match = getattr(request, "resolver_match", None)
        return bool(match and match.url_name == "mes_core_employee_changelist")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        from django.contrib.auth import get_user_model
        from django.db.models import Q

        if db_field.name == "supervisor":
            # supervisores vigentes + los que ya tienen gente (para no borrarlos sin querer al guardar la lista)
            kwargs["queryset"] = (get_user_model().objects
                                  .filter(Q(pk__in=staffing.supervisors().values("pk")) | Q(crew_members__isnull=False))
                                  .distinct().order_by("first_name", "last_name", "username"))
        elif db_field.name == "shift":
            qs = Shift.objects.filter(Q(active=True) | Q(employees__isnull=False)).distinct()  # todas las plantas
            kwargs["queryset"] = qs.select_related("site").order_by("site__code", "start_time")
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if db_field.name in ("supervisor", "shift") and field is not None:
            field.label_from_instance = staffing.user_label if db_field.name == "supervisor" else _shift_label
            # Una sola consulta por desplegable aunque la lista tenga 100 filas.
            cache = request.__dict__.setdefault("_mes_emp_choices", {})
            if db_field.name not in cache:
                cache[db_field.name] = list(field.choices)
            field.choices = cache[db_field.name]
        return field

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        from django.contrib.admin.widgets import RelatedFieldWidgetWrapper

        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if field is not None and self._in_changelist(request) and isinstance(field.widget, RelatedFieldWidgetWrapper):
            field.widget = field.widget.widget  # en la lista: solo el desplegable, sin los íconos de agregar/editar
        return field

    # ------------------------------------------------------------ acciones masivas
    def _bulk_step(self, request, queryset, action, title, form_class, apply, **form_kw):
        if "apply" in request.POST:
            form = form_class(request.POST, **form_kw)
            if form.is_valid():
                with transaction.atomic():
                    msg = apply(queryset, form.cleaned_data)
                self.message_user(request, msg, messages.SUCCESS)
                return None  # vuelve a la lista
        else:
            form = form_class(**form_kw)
        across = request.POST.get("select_across") == "1"
        ids = _ids(queryset)
        context = {
            **self.admin_site.each_context(request),
            "title": title, "opts": self.model._meta, "form": form, "action": action, "count": len(ids),
            "sample": queryset.select_related("shift", "shift__site", "supervisor")[:25],
            "ids": ids[:1] if across else ids, "select_across": across,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME, "media": self.media + form.media,
        }
        return TemplateResponse(request, "admin/mes_core/employee/bulk_assign.html", context)

    def _log(self, queryset_ids, text, user):
        site = Site.objects.filter(active=True).order_by("code").first()  # personal general: bitácora en la planta principal
        if site is not None and queryset_ids:
            staffing._log(site, f"{_plural(len(queryset_ids))} (administración): {text}", user)

    @admin.action(description="Asignar supervisor a los seleccionados", permissions=["change"])
    def bulk_supervisor(self, request, queryset):
        def apply(qs, data):
            ids, sup = _ids(qs), data["supervisor"]
            n = Employee.objects.filter(pk__in=ids).update(supervisor=sup, updated_at=timezone.now(), crew_borrowed=False,
                                                           crew_operation=None, home_supervisor=None)
            text = f"supervisor {staffing.user_label(sup)}" if sup else "sin supervisor"
            self._log(ids, text, request.user)
            return f"{_plural(n)} con {text}."
        return self._bulk_step(request, queryset, "bulk_supervisor", "Asignar supervisor", BulkSupervisorForm, apply)

    @admin.action(description="Asignar turno a los seleccionados", permissions=["change"])
    def bulk_shift(self, request, queryset):
        def apply(qs, data):
            ids, shift = _ids(qs), data["shift"]
            n = Employee.objects.filter(pk__in=ids).update(shift=shift, updated_at=timezone.now())
            text = f"turno {_shift_label(shift)}" if shift else "sin turno"
            self._log(ids, text, request.user)
            return f"{_plural(n)} con {text}."
        return self._bulk_step(request, queryset, "bulk_shift", "Asignar turno", BulkShiftForm, apply)

    @admin.action(description="Asignar competencia y nivel a los seleccionados", permissions=["change"])
    def bulk_skill(self, request, queryset):
        def apply(qs, data):
            ids, skill, level = _ids(qs), data["skill"], int(data["level"])
            if level == 0:
                if skill.pk is None:
                    return "Esa competencia no existe: no había nada que quitar."
                n, _ = EmployeeSkill.objects.filter(employee_id__in=ids, skill=skill).delete()
                self._log(ids, f"sin la competencia {skill.name}", request.user)
                return f"Competencia «{skill.name}» quitada a {_plural(n)}."
            if skill.pk is None:
                skill.save()
            now = timezone.now()
            had = set(EmployeeSkill.objects.filter(employee_id__in=ids, skill=skill).values_list("employee_id", flat=True))
            EmployeeSkill.objects.filter(employee_id__in=had, skill=skill).update(level=level, updated_by=request.user,
                                                                                  updated_at=now)
            EmployeeSkill.objects.bulk_create([EmployeeSkill(employee_id=i, skill=skill, level=level, updated_by=request.user)
                                               for i in ids if i not in had])
            label = EmployeeSkill.Level(level).label.lower()
            self._log(ids, f"{skill.name}: {label}", request.user)
            return f"{_plural(len(ids))} con «{skill.name}» en nivel {label}."
        return self._bulk_step(request, queryset, "bulk_skill", "Asignar competencia", BulkSkillForm, apply)

    @admin.action(description="Activar los seleccionados", permissions=["change"])
    def bulk_activate(self, request, queryset):
        n = Employee.objects.filter(pk__in=_ids(queryset)).update(active=True, updated_at=timezone.now())
        self.message_user(request, f"{_plural(n)} activado{'' if n == 1 else 's'}.", messages.SUCCESS)

    @admin.action(description="Inactivar los seleccionados", permissions=["change"])
    def bulk_deactivate(self, request, queryset):
        n = Employee.objects.filter(pk__in=_ids(queryset)).update(active=False, updated_at=timezone.now())
        self.message_user(request, f"{_plural(n)} inactivado{'' if n == 1 else 's'}.", messages.SUCCESS)

    # ------------------------------------------------------------ carga desde Excel
    def get_urls(self):
        extra = [
            path("importar/", self.admin_site.admin_view(self.import_view), name="mes_core_employee_import"),
            path("plantilla/", self.admin_site.admin_view(self.template_view), name="mes_core_employee_template"),
        ]
        return extra + super().get_urls()

    def import_view(self, request):
        """Carga del personal general (sirve para todas las plantas)."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        result, upload_name, error = None, "", ""
        if request.method == "POST":
            upload = request.FILES.get("file")
            if upload is None:
                error = "Elige el archivo de Excel."
            elif upload.size > MAX_UPLOAD_MB * 1024 * 1024:
                error = f"El archivo pesa más de {MAX_UPLOAD_MB} MB."
            else:
                upload_name = upload.name
                result = staffing.import_employees(upload.read(), upload.name, user=request.user)
                if result.ok:
                    if not result.rows:
                        self.message_user(request, "El archivo no tiene filas con datos debajo de los encabezados.",
                                          messages.WARNING)
                    else:
                        msg = (f"Carga lista: {result.created} operario(s) nuevo(s), {result.updated} actualizado(s)"
                               f"{f', {result.levels} niveles de competencia' if result.levels else ''}.")
                        if result.new_skills:
                            msg += " Competencias nuevas: " + ", ".join(result.new_skills) + "."
                        self.message_user(request, msg, messages.SUCCESS)
                    return redirect("admin:mes_core_employee_changelist")
        context = {
            **self.admin_site.each_context(request), "title": "Cargar operarios desde Excel", "opts": self.model._meta,
            "result": result, "upload_name": upload_name, "error": error,
            "max_mb": MAX_UPLOAD_MB, "max_rows": staffing.MAX_IMPORT_ROWS,
        }
        return TemplateResponse(request, "admin/mes_core/employee/import.html", context)

    def template_view(self, request):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        resp = HttpResponse(staffing.template_xlsx(),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = 'attachment; filename="plantilla_operarios.xlsx"'
        return resp


@admin.register(BomLine)
class BomLineAdmin(admin.ModelAdmin):
    list_display = ("product", "component", "qty_per_unit", "scrap_pct", "stage", "updated_at")
    search_fields = ("product__sku", "product__name", "component__sku", "component__name")
    raw_id_fields = ("product", "component")
    readonly_fields = ("updated_by", "updated_at")


@admin.register(LineSupervisor)
class LineSupervisorAdmin(admin.ModelAdmin):
    list_display = ("work_center", "shift", "supervisor", "updated_at")
    list_filter = ("work_center__site", "shift")
    list_editable = ("supervisor",)
    readonly_fields = ("updated_by", "updated_at")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "kind", "title", "read_at", "emailed_at")
    list_filter = ("kind", "read_at")
    search_fields = ("title", "user__username")
    readonly_fields = [f.name for f in Notification._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(SkillReview)
class SkillReviewAdmin(admin.ModelAdmin):
    list_display = ("employee", "skill", "level", "hours", "decision", "decided_by", "decided_at")
    list_filter = ("decision", "level")


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ("day", "employee", "shift", "status", "notes", "confirmed_by", "confirmed_at")
    list_filter = ("status", "shift__site", "shift")
    date_hierarchy = "day"
    search_fields = ("employee__full_name", "employee__document")
    autocomplete_fields = ("employee",)


@admin.register(CrewAssignment)
class CrewAssignmentAdmin(admin.ModelAdmin):
    list_display = ("operation", "employee", "assigned_by", "assigned_at")
    search_fields = ("employee__full_name", "employee__document", "operation__order__number")
    autocomplete_fields = ("employee",)
    raw_id_fields = ("operation",)
