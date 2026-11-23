from mes_core.plugins import MenuItem, MESPlugin, OrderAction

VIEW_PERMS = ("mes_core.view_productionreport", "mes_core.plan_workorder", "mes_core.approve_plan")


class RegistrosConfig(MESPlugin):
    name = "plugins.registros"
    label = "registros"
    verbose_name = "Registros, seguimiento y exportación"
    plugin_version = "1.0.0"
    plugin_description = ("Lo que hacía la app anterior con su hoja de cálculo: buscar registros por persona, editarlos "
                          "con motivo, anularlos, reactivarlos y eliminar los anulados; seguimiento de pedidos (Cumple / "
                          "No cumple, avance por referencia, cierre con faltante) y exportar a Excel con las mismas "
                          "hojas y columnas.")
    requires = ("piso",)

    def get_menu_items(self):
        return [
            MenuItem("Registros de producción", "registros:records", perms=VIEW_PERMS),
            MenuItem("Seguimiento de pedidos", "registros:tracking", perms=VIEW_PERMS),
            MenuItem("Exportar a Excel", "registros:export", perms=VIEW_PERMS),
        ]

    def get_order_actions(self, order):
        from django.urls import reverse

        return [OrderAction("Seguimiento y registros", reverse("registros:tracking_detail", args=[order.pk]))]
