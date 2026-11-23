from django.urls import reverse

from mes_core.plugins import MenuItem, MESPlugin, OperationTemplate, OrderAction, ProcessType


class FoodConfig(MESPlugin):
    name = "plugins.food"
    label = "food"
    verbose_name = "Planta de alimentos"
    plugin_version = "0.1.0"
    plugin_description = (
        "Procesos cárnicos (deshuese de pollo): rendimiento estándar vs. real por corte "
        "y trazabilidad de lotes de entrada y salida."
    )

    def get_process_types(self):
        return [
            ProcessType(
                key="deshuese",
                label="Deshuese de pollo",
                description="Cantidad = kg de materia prima de entrada.",
                operations=(
                    OperationTemplate("Recepción y pesaje", "recepcion", setup_minutes=10, minutes_per_unit=0.01),
                    OperationTemplate("Deshuese", "deshuese", minutes_per_unit=0.6, setup_minutes=15),
                    OperationTemplate("Porcionado", "porcionado", minutes_per_unit=0.3),
                    OperationTemplate("Empaque y rotulado", "empaque", minutes_per_unit=0.2, setup_minutes=10),
                ),
            )
        ]

    def get_menu_items(self):
        return [MenuItem("Lotes", "food:lot_list",
                         perms=("mes_core.plan_workorder", "mes_core.approve_plan", "mes_core.supervise_crew"))]

    def get_order_actions(self, order):
        if order.process_type != "deshuese":
            return []
        return [OrderAction("Rendimiento y lotes", reverse("food:order_yield", args=[order.pk]))]
