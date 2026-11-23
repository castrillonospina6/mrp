from mes_core.plugins import MenuItem, MESPlugin

SUPERVISION = ("mes_core.supervise_crew", "mes_core.approve_plan", "mes_core.assign_supervisor")


class PisoConfig(MESPlugin):
    name = "plugins.piso"
    label = "piso"
    verbose_name = "Control de piso (tablets)"
    plugin_version = "1.0.0"
    plugin_description = (
        "App de planta para operarios en tablet, conectada en tiempo real con el MES: cada operario ve la tarea que le "
        "asignó su supervisor, marca unidades (que entran al MES como reporte de producción), registra paros y pausas "
        "con QR y recibe mensajes. Incluye tablero en vivo, histórico y traslados con patinadores."
    )

    def get_menu_items(self):
        return [
            MenuItem("Abrir app de planta", "piso:tablet", perms=SUPERVISION),
            MenuItem("Piso en vivo", "piso:live", perms=SUPERVISION),
            MenuItem("Histórico de piso", "piso:history", perms=SUPERVISION),
            MenuItem("Traslados", "piso:transfers", perms=SUPERVISION),
            MenuItem("Forma de reporte", "piso:reporting", perms=SUPERVISION),
            MenuItem("Tablets y QR", "piso:codes", perms=("mes_core.approve_plan",)),
            MenuItem("Horas extra", "piso:overtime", perms=("mes_core.plan_workorder", "mes_core.approve_plan",
                                                            "mes_core.supervise_crew", "mes_core.change_shift")),
        ]

    def ready(self):
        from .live import connect_signals

        connect_signals()

    # Las marcaciones siguen a su reporte al anularlo, reactivarlo o corregirlo en el MES
    def report_snapshot(self, report):
        from .report_hooks import snapshot

        return snapshot(report)

    def report_restore(self, report, data):
        from .report_hooks import restore

        restore(report, data)

    def report_edited(self, report, change):
        from .report_hooks import edited

        edited(report, change)

    # «Armar cuadrilla» en el MES: ¿requiere patinador? → quién (su app pasa a la pantalla de patinador)
    def crew_panel(self, operation, request):
        from django.template.loader import render_to_string

        from mes_core.models import Employee

        from .models import FloorAccess

        current = next((a.employee for a in operation.mover_assignments.select_related("employee")), None)
        people = list(Employee.objects.filter(active=True).order_by("full_name"))
        access = dict(FloorAccess.objects.filter(active=True).values_list("employee_id", "role"))
        crew = set(operation.crew_assignments.values_list("employee_id", flat=True))
        movers = [e for e in people if access.get(e.pk) == FloorAccess.Role.MOVER]
        others = [e for e in people if access.get(e.pk) != FloorAccess.Role.MOVER and e.pk not in crew]
        return render_to_string("piso/_crew_mover.html", {
            "op": operation, "current": current, "movers": movers, "others": others, "with_app": set(access),
        }, request=request)

    def crew_save(self, operation, data, user):
        from django.core.exceptions import ValidationError

        from mes_core.models import Employee

        from .transfers import set_operation_mover

        if data.get("mover_panel") != "1":
            return ""
        if data.get("mover_need") != "1":
            return set_operation_mover(operation, None, user) if operation.mover_assignments.exists() else ""
        pk = str(data.get("mover") or "")
        emp = Employee.objects.filter(pk=pk).first() if pk.isdigit() else None
        if emp is None:
            raise ValidationError("Elige quién será el patinador (o marca «No» en «¿Requiere patinador?»).")
        return set_operation_mover(operation, emp, user)
