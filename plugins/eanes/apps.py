from django.db.models.signals import post_migrate

from mes_core.plugins import MenuItem, MESPlugin, OrderAction

ROLE = "Validación EAN"
# Carga los EAN de cada pedido. Solo ve los pedidos (no los planea ni los modifica).
ROLE_PERMS = [
    "eanes.manage_eans", "eanes.view_eansheet", "eanes.view_eanline", "eanes.view_eanlog",
    "mes_core.view_workorder", "mes_core.view_product", "mes_core.view_customer", "mes_core.view_site",
]
VIEW_PERMS = ("eanes.manage_eans", "mes_core.plan_workorder", "mes_core.approve_plan")


def ensure_role(sender, **kwargs):
    from django.contrib.auth.models import Group, Permission

    group, _ = Group.objects.get_or_create(name=ROLE)
    for dotted in ROLE_PERMS:
        app_label, codename = dotted.split(".")
        perm = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
        if perm:
            group.permissions.add(perm)


class EanesConfig(MESPlugin):
    name = "plugins.eanes"
    label = "eanes"
    verbose_name = "Validación de EAN"
    plugin_version = "0.1.0"
    plugin_description = ("Referencias (EAN) de cada pedido y cuántas unidades de cada una. Las carga una persona "
                          "aparte (rol «Validación EAN»): pegando desde Excel, subiendo el archivo del cliente, "
                          "escaneando o copiando de otro pedido.")

    def ready(self):
        post_migrate.connect(ensure_role, sender=self, dispatch_uid="eanes_role")
        from mes_core import signals

        from .notify import on_order_created

        signals.order_created.connect(on_order_created, dispatch_uid="eanes_order_created")

    def get_menu_items(self):
        return [MenuItem("Validación EAN", "eanes:queue", perms=VIEW_PERMS)]

    # Reportes de producción: referencia obligatoria y tope por referencia (como la app anterior)
    def check_report(self, operation, **kw):
        from .eans import check_report

        return check_report(operation, **kw)

    def report_info(self, operations):
        from .eans import report_info

        return report_info(operations)

    def shortfall_lines(self, operation, pending):
        from .eans import shortfall_lines

        return shortfall_lines(operation, pending)

    def get_order_actions(self, order):
        from django.urls import reverse

        from .eans import status_of
        from .models import EanSheet

        label = {EanSheet.Status.LOADED: "EAN del pedido", EanSheet.Status.NOT_APPLY: "EAN: no aplica"}.get(
            status_of(order), "EAN: pendiente")
        return [OrderAction(label, reverse("eanes:order", args=[order.pk]))]
