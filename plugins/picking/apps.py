from mes_core.plugins import MESPlugin, OperationTemplate, ProcessType


class PickingConfig(MESPlugin):
    name = "plugins.picking"
    label = "picking"
    verbose_name = "Picking"
    plugin_version = "0.1.0"
    plugin_description = "Alistamiento de pedidos: líneas de picking por ubicación y verificación antes de despacho."

    def get_process_types(self):
        return [
            ProcessType(
                key="picking",
                label="Picking de pedidos",
                description="Cantidad = unidades a alistar. Los tiempos son estándar de referencia: ajústalos a tu operación.",
                operations=(
                    OperationTemplate("Liberación de ola", "picking", setup_minutes=10),
                    OperationTemplate("Picking", "picking", minutes_per_unit=0.5),
                    OperationTemplate("Verificación", "verificacion", minutes_per_unit=0.1, setup_minutes=5),
                    OperationTemplate("Despacho", "despacho", setup_minutes=15, minutes_per_unit=0.02),
                ),
            )
        ]

    def get_order_admin_inlines(self):
        from .admin import PickLineInline

        return [PickLineInline]
