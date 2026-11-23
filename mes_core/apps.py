from django.apps import AppConfig
from django.db.models.signals import post_migrate

# Roles base. Se crean/actualizan solos en cada `migrate`; asigna usuarios desde Administración → Grupos.
ROLE_PERMISSIONS = {
    "Ventas": [
        "mes_core.add_workorder", "mes_core.view_workorder",
        "mes_core.add_customer", "mes_core.view_customer", "mes_core.view_product", "mes_core.view_site",
    ],
    "Planeación": [
        "mes_core.plan_workorder", "mes_core.view_workorder", "mes_core.change_workorder",
        "mes_core.view_operation", "mes_core.change_operation",
        "mes_core.view_shift", "mes_core.change_shift", "mes_core.add_shift",
        "mes_core.add_downtime", "mes_core.change_downtime", "mes_core.delete_downtime", "mes_core.view_downtime",
        "mes_core.view_workcenter", "mes_core.view_product", "mes_core.view_customer", "mes_core.view_site",
        "mes_core.view_calendarexception", "mes_core.add_calendarexception", "mes_core.change_calendarexception",
        "mes_core.delete_calendarexception", "mes_core.view_planningrules", "mes_core.change_planningrules",
        "mes_core.add_planningrules", "mes_core.view_changeovertime", "mes_core.add_changeovertime",
        "mes_core.change_changeovertime", "mes_core.delete_changeovertime", "mes_core.view_planversion",
        "mes_core.add_planversion", "mes_core.change_product",
        "mes_core.view_servicetype", "mes_core.add_servicetype", "mes_core.change_servicetype",
        "mes_core.delete_servicetype", "mes_core.add_productionreport", "mes_core.view_productionreport",
        # Insumos: solo consulta (los libera el equipo de insumos)
        "mes_core.view_supplystock", "mes_core.view_supplyrequirement", "mes_core.view_supplymovement",
        # Personal: carga de operarios y asignación de supervisores
        "mes_core.assign_supervisor", "mes_core.view_employee", "mes_core.add_employee", "mes_core.change_employee",
        "mes_core.view_skill", "mes_core.add_skill", "mes_core.view_employeeskill", "mes_core.change_employeeskill",
        "mes_core.view_attendance", "mes_core.view_crewassignment",
        # Automatizaciones: listas de materiales y supervisor por línea y turno
        "mes_core.view_bomline", "mes_core.add_bomline", "mes_core.change_bomline", "mes_core.delete_bomline",
        "mes_core.view_linesupervisor", "mes_core.add_linesupervisor", "mes_core.change_linesupervisor",
        "mes_core.delete_linesupervisor", "mes_core.view_skillreview",
        # Bolsa global de personal: reparto por bodega y quién está dónde
        "mes_core.view_staffallocation", "mes_core.add_staffallocation", "mes_core.change_staffallocation",
        "mes_core.delete_staffallocation", "mes_core.view_staffmove", "mes_core.add_staffmove",
        "mes_core.change_staffmove", "mes_core.delete_staffmove",
    ],
    "Jefe de planta": [
        "mes_core.approve_plan", "mes_core.plan_workorder", "mes_core.view_planversion", "mes_core.change_planversion",
        "mes_core.view_workorder", "mes_core.change_workorder", "mes_core.view_operation", "mes_core.view_shift",
        "mes_core.view_calendarexception", "mes_core.view_planningrules", "mes_core.view_workcenter",
        "mes_core.view_product", "mes_core.view_customer", "mes_core.view_site",
        "mes_core.assign_supervisor", "mes_core.supervise_crew", "mes_core.view_employee", "mes_core.view_skill",
        "mes_core.view_employeeskill", "mes_core.change_employeeskill", "mes_core.view_attendance",
        "mes_core.view_crewassignment", "mes_core.view_servicetype", "mes_core.add_servicetype",
        "mes_core.change_servicetype", "mes_core.delete_servicetype",
        "mes_core.release_to_plant", "mes_core.view_supplystock", "mes_core.view_supplyrequirement",
        "mes_core.view_supplymovement", "mes_core.add_productionreport", "mes_core.view_productionreport",
        "mes_core.view_bomline", "mes_core.view_linesupervisor", "mes_core.add_linesupervisor",
        "mes_core.change_linesupervisor", "mes_core.delete_linesupervisor", "mes_core.view_skillreview",
        "mes_core.add_skillreview",
        "mes_core.view_staffallocation", "mes_core.add_staffallocation", "mes_core.change_staffallocation",
        "mes_core.delete_staffallocation", "mes_core.view_staffmove", "mes_core.add_staffmove",
        "mes_core.change_staffmove", "mes_core.delete_staffmove",
    ],
    # Inventario de insumos, entregas a cada proceso y liberación a planta.
    "Insumos": [
        "mes_core.release_to_plant", "mes_core.view_workorder", "mes_core.view_operation", "mes_core.view_site",
        "mes_core.view_shift", "mes_core.view_workcenter", "mes_core.view_customer",
        "mes_core.view_product", "mes_core.view_supplyitem", "mes_core.add_supplyitem", "mes_core.change_supplyitem",
        "mes_core.view_supplystock", "mes_core.add_supplystock", "mes_core.change_supplystock",
        "mes_core.view_supplymovement", "mes_core.add_supplymovement",
        "mes_core.view_supplyrequirement", "mes_core.add_supplyrequirement", "mes_core.change_supplyrequirement",
        "mes_core.delete_supplyrequirement",
        "mes_core.view_bomline", "mes_core.add_bomline", "mes_core.change_bomline", "mes_core.delete_bomline",
    ],
    # Merca la mercancía de cada pedido: ve la información en tarjetas y solo la marca como «mercado»
    # (quién mercó, ubicación y observaciones). No modifica pedidos.
    "Logística": [
        "mes_core.mark_goods", "mes_core.view_workorder", "mes_core.view_operation", "mes_core.view_site",
        "mes_core.view_product", "mes_core.view_customer", "mes_core.view_workcenter", "mes_core.view_shift",
    ],
    # Ve a su equipo, llena su matriz de polivalencia, confirma su asistencia y arma las cuadrillas de sus
    # operaciones. NO carga ni edita operarios: eso se hace solo desde el panel de administración.
    "Supervisor": [
        "mes_core.supervise_crew", "mes_core.view_employee",
        "mes_core.view_skill", "mes_core.add_skill",
        "mes_core.view_employeeskill", "mes_core.change_employeeskill", "mes_core.view_attendance",
        "mes_core.add_attendance", "mes_core.change_attendance", "mes_core.view_crewassignment",
        "mes_core.add_crewassignment", "mes_core.delete_crewassignment", "mes_core.view_operation",
        "mes_core.view_workorder", "mes_core.view_shift", "mes_core.view_workcenter", "mes_core.view_site",
        "mes_core.add_productionreport", "mes_core.view_productionreport", "mes_core.view_supplyrequirement",
        "mes_core.view_skillreview", "mes_core.add_skillreview",
        # Ve el reparto y mueve gente entre bodegas (quién está dónde)
        "mes_core.view_staffallocation", "mes_core.view_staffmove", "mes_core.add_staffmove",
    ],
}

# Permisos que un rol tuvo en versiones anteriores y ya no debe tener: se le quitan en cada `migrate`.
ROLE_REVOKED = {
    "Supervisor": ["mes_core.add_employee", "mes_core.change_employee"],
    # Insumos maneja el catálogo de insumos, no el de productos (no se mezclan)
    "Insumos": ["mes_core.add_product", "mes_core.change_product"],
}


def _perm(dotted):
    from django.contrib.auth.models import Permission

    app_label, codename = dotted.split(".")
    return Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()


def ensure_roles(sender, **kwargs):
    from django.contrib.auth.models import Group

    for group_name, perms in ROLE_PERMISSIONS.items():
        group, _ = Group.objects.get_or_create(name=group_name)
        for dotted in perms:
            perm = _perm(dotted)
            if perm:
                group.permissions.add(perm)
        for dotted in ROLE_REVOKED.get(group_name, []):
            perm = _perm(dotted)
            if perm:
                group.permissions.remove(perm)


class MesCoreConfig(AppConfig):
    name = "mes_core"
    verbose_name = "Producción y planeación"

    def ready(self) -> None:
        # Todos los AppConfig ya existen cuando se llama ready(): registramos los plugins.
        from .plugins import registry

        registry.load()
        post_migrate.connect(ensure_roles, sender=self)
        from .workcal import connect_signals

        connect_signals()
        from .service_types import connect_signals as connect_service_type_signals

        connect_service_type_signals()
        from .staffing import connect_signals as connect_staff_signals

        connect_staff_signals()
        from .notify import connect_signals as connect_notify_signals

        connect_notify_signals()
