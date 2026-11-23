from django.urls import reverse

from mes_core.plugins import MenuItem, MESPlugin, OperationTemplate, OrderAction, ProcessType


class CopackingConfig(MESPlugin):
    name = "plugins.copacking"
    label = "copacking"
    verbose_name = "Acondicionado"
    plugin_version = "0.1.0"
    plugin_description = (
        "Acondicionado de producto (etiquetado, ganchos, embolsado, encajado). "
        "Ficha de acondicionado por producto y cálculo de insumos por orden."
    )

    def get_process_types(self):
        return [
            ProcessType(
                key="acondicionado",
                label="Acondicionado de producto",
                description="Cantidad = unidades a acondicionar.",
                operations=(
                    OperationTemplate("Alistamiento de insumos", "acondicionado", setup_minutes=15),
                    OperationTemplate("Etiquetado", "etiquetado", minutes_per_unit=0.15, setup_minutes=10),
                    OperationTemplate("Colocación de ganchos", "acondicionado", minutes_per_unit=0.10),
                    OperationTemplate("Embolsado", "acondicionado", minutes_per_unit=0.12),
                    OperationTemplate("Encajado", "encajado", minutes_per_unit=0.08, setup_minutes=5),
                ),
            )
        ]

    def get_menu_items(self):
        return [MenuItem("Fichas de acondicionado", "copacking:spec_list")]

    def get_supply_requirements(self, order):
        """Sugiere al equipo de insumos lo que dice la ficha de acondicionado del producto."""
        from .models import material_requirements

        return [(r.material, r.quantity) for r in material_requirements(order)]

    def get_order_actions(self, order):
        if order.process_type != "acondicionado":
            return []
        return [OrderAction("Insumos requeridos", reverse("copacking:order_materials", args=[order.pk]))]
