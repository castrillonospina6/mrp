"""API pública para escribir plugins del MES.

Un plugin es una app de Django cuyo AppConfig hereda de ``MESPlugin``.
Puede aportar:
  * Tipos de proceso (ruta de operaciones estándar) -> ``get_process_types()``
  * Entradas de menú                                 -> ``get_menu_items()``
  * Acciones en el detalle de una orden              -> ``get_order_actions(order)``
  * Inlines en el admin de la orden                  -> ``get_order_admin_inlines()``
  * Vistas propias: un módulo ``urls.py`` se monta en ``/p/<label>/``
  * Reacciones a eventos: conectarse a las señales de ``mes_core.signals``
  * Validar y acompañar los reportes de producción -> ``check_report()``, ``report_info()``,
    ``report_snapshot()`` / ``report_restore()`` (anular / reactivar), ``report_edited()``, ``shortfall_lines()``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module

from django.apps import AppConfig, apps
from django.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class OperationTemplate:
    """Paso estándar de un proceso.

    ``work_center_kind`` se compara con ``WorkCenter.kind`` para asignar el
    centro de trabajo automáticamente al crear la orden.
    """

    name: str
    work_center_kind: str
    minutes_per_unit: float = 0.0
    setup_minutes: float = 0.0


@dataclass(frozen=True)
class ProcessType:
    key: str
    label: str
    operations: tuple[OperationTemplate, ...]
    description: str = ""
    plugin: str = field(default="", compare=False)


# Por defecto un módulo se muestra en el menú a planeación, jefe de planta e insumos (no a logística, ventas ni supervisor).
DEFAULT_MENU_PERMS = ("mes_core.plan_workorder", "mes_core.approve_plan", "mes_core.release_to_plant")


@dataclass(frozen=True)
class MenuItem:
    label: str
    url_name: str
    perms: tuple[str, ...] = DEFAULT_MENU_PERMS  # basta con tener uno; vacío = todos

    def visible_to(self, user) -> bool:
        return not self.perms or any(user.has_perm(p) for p in self.perms)


@dataclass(frozen=True)
class OrderAction:
    label: str
    url: str


class MESPlugin(AppConfig):
    """Clase base para el AppConfig de cada plugin."""

    # Django elige solo el AppConfig de un apps.py si es el único con default=True.
    # La base importada no debe competir; las subclases se marcan solas como default.
    default = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "default" not in cls.__dict__:
            cls.default = True

    plugin_version: str = "0.1.0"
    plugin_description: str = ""
    requires: tuple[str, ...] = ()  # labels de otros plugins/apps necesarios

    def get_process_types(self) -> list[ProcessType]:
        return []

    def get_menu_items(self) -> list[MenuItem]:
        return []

    def get_order_actions(self, order) -> list[OrderAction]:
        return []

    def get_order_admin_inlines(self) -> list[type]:
        return []

    def get_supply_requirements(self, order) -> list[tuple]:
        """Insumos sugeridos para un pedido: [(producto, cantidad), …]. El equipo de insumos los ajusta."""
        return []

    # --- reportes de producción ------------------------------------------
    def check_report(self, operation, *, ean: str, qty_good, qty_leftover, exclude_report=None) -> dict:
        """Antes de guardar un reporte (dentro de la transacción, con la operación bloqueada). Lanza ValidationError
        para rechazarlo; puede devolver {"ean": forma canónica}."""
        return {}

    def report_info(self, operations) -> dict:
        """Datos extra para reportar cada operación (p. ej. sus referencias): {id_operación: {...}}."""
        return {}

    def report_snapshot(self, report) -> dict:
        """Lo que el módulo guarda de un reporte antes de anularlo (para poder reactivarlo tal cual)."""
        return {}

    def report_restore(self, report, data: dict) -> None:
        """Rehace lo guardado por ``report_snapshot`` al reactivar el reporte (``report`` es el nuevo)."""

    def report_edited(self, report, change: dict) -> None:
        """Un reporte se corrigió. ``change``: old_operation, delta (timedelta), good/scrap (antes, después),
        old_employee_id."""

    def shortfall_lines(self, operation, pending) -> list[tuple[str, object]]:
        """Al cerrar con faltante: cómo se reparte lo que falta de la operación, [(ean, cantidad), …]. Vacío = una
        sola línea sin referencia."""
        return []

    def crew_panel(self, operation, request) -> str:
        """HTML extra para «Armar cuadrilla» (va dentro del formulario que guarda la cuadrilla)."""
        return ""

    def crew_save(self, operation, data, user) -> str:
        """Se guardó la cuadrilla: el módulo lee sus campos de ``data`` (POST). Devuelve un aviso o ""; puede lanzar
        ValidationError."""
        return ""


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, MESPlugin] = {}
        self._process_types: dict[str, ProcessType] = {}

    def load(self) -> None:
        self._plugins.clear()
        self._process_types.clear()
        for config in apps.get_app_configs():
            if isinstance(config, MESPlugin):
                self.register(config)

    def register(self, plugin: MESPlugin) -> None:
        labels = {c.label for c in apps.get_app_configs()}
        missing = [req for req in plugin.requires if req not in labels]
        if missing:
            raise ImproperlyConfigured(
                f"El plugin '{plugin.label}' requiere: {', '.join(missing)}"
            )
        for pt in plugin.get_process_types():
            if pt.key in self._process_types:
                other = self._process_types[pt.key].plugin
                raise ImproperlyConfigured(
                    f"Tipo de proceso '{pt.key}' duplicado entre '{other}' y '{plugin.label}'"
                )
            self._process_types[pt.key] = ProcessType(
                key=pt.key,
                label=pt.label,
                operations=tuple(pt.operations),
                description=pt.description,
                plugin=plugin.label,
            )
        self._plugins[plugin.label] = plugin

    # --- consultas -------------------------------------------------------
    def plugins(self) -> list[MESPlugin]:
        return list(self._plugins.values())

    def process_types(self) -> list[ProcessType]:
        return list(self._process_types.values())

    def process_type_choices(self) -> list[tuple[str, str]]:
        return [(pt.key, pt.label) for pt in self._process_types.values()]

    def get_process_type(self, key: str) -> ProcessType | None:
        return self._process_types.get(key)

    def menu_items(self) -> list[MenuItem]:
        return [item for p in self._plugins.values() for item in p.get_menu_items()]

    def order_actions(self, order) -> list[OrderAction]:
        return [a for p in self._plugins.values() for a in p.get_order_actions(order)]

    def order_admin_inlines(self) -> list[type]:
        return [i for p in self._plugins.values() for i in p.get_order_admin_inlines()]

    def supply_requirements(self, order) -> list[tuple]:
        return [r for p in self._plugins.values() for r in p.get_supply_requirements(order)]

    def check_report(self, operation, **kw) -> dict:
        out: dict = {}
        for p in self._plugins.values():
            out.update(p.check_report(operation, **kw) or {})
        return out

    def report_info(self, operations) -> dict:
        out: dict = {}
        for p in self._plugins.values():
            for k, v in (p.report_info(operations) or {}).items():
                out.setdefault(k, {}).update(v)
        return out

    def report_snapshot(self, report) -> dict:
        return {p.label: p.report_snapshot(report) or {} for p in self._plugins.values()}

    def report_restore(self, report, data: dict) -> None:
        for p in self._plugins.values():
            p.report_restore(report, (data or {}).get(p.label) or {})

    def report_edited(self, report, change: dict) -> None:
        for p in self._plugins.values():
            p.report_edited(report, change)

    def shortfall_lines(self, operation, pending) -> list:
        for p in self._plugins.values():
            lines = p.shortfall_lines(operation, pending)
            if lines:
                return lines
        return []

    def crew_panels(self, operation, request) -> list[str]:
        return [h for h in (p.crew_panel(operation, request) for p in self._plugins.values()) if h]

    def crew_save(self, operation, data, user) -> list[str]:
        return [m for m in (p.crew_save(operation, data, user) for p in self._plugins.values()) if m]


registry = PluginRegistry()


def plugin_urlpatterns():
    """Monta ``<plugin>.urls`` en ``/p/<label>/`` con namespace ``<label>``."""
    from django.urls import include, path

    patterns = []
    for plugin in registry.plugins():
        module_name = f"{plugin.name}.urls"
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                continue
            raise
        patterns.append(
            path(f"p/{plugin.label}/", include((module.urlpatterns, plugin.label)))
        )
    return patterns
