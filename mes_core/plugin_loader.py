"""Descubrimiento de plugins en tiempo de configuración (antes de que Django arranque).

No importa nada de Django: solo decide qué paquetes entran a INSTALLED_APPS.

Fuentes de plugins:
  1. Carpeta local ``plugins/``: cada subpaquete con ``apps.py`` es un plugin.
  2. Paquetes instalados con pip que declaren el entry point ``mes.plugins``:
         [project.entry-points."mes.plugins"]
         mi_plugin = "mi_paquete"

Filtro (variables de entorno, listas separadas por coma):
  MES_PLUGINS           -> si se define, solo se cargan esos plugins.
  MES_DISABLED_PLUGINS  -> plugins a excluir.
"""

from __future__ import annotations

import os
from importlib.metadata import entry_points
from pathlib import Path

ENTRY_POINT_GROUP = "mes.plugins"


def _csv_env(name: str) -> set[str] | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


def _local_plugins(plugins_dir: Path, package: str) -> dict[str, str]:
    found: dict[str, str] = {}
    if not plugins_dir.is_dir():
        return found
    for child in sorted(plugins_dir.iterdir()):
        if child.is_dir() and (child / "__init__.py").exists() and (child / "apps.py").exists():
            found[child.name] = f"{package}.{child.name}"
    return found


def _installed_plugins() -> dict[str, str]:
    return {ep.name: ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)}


def discover_plugins(plugins_dir: Path, package: str = "plugins") -> list[str]:
    """Devuelve la lista de módulos de plugin para añadir a INSTALLED_APPS."""
    candidates = {**_local_plugins(plugins_dir, package), **_installed_plugins()}
    enabled = _csv_env("MES_PLUGINS")
    disabled = _csv_env("MES_DISABLED_PLUGINS") or set()
    return [
        module
        for name, module in candidates.items()
        if (enabled is None or name in enabled) and name not in disabled
    ]
