"""Tipos de servicio: catálogo editable (activar / desactivar / renombrar / crear a mano).

* Los módulos (plugins) aportan tipos con ruta estándar de etapas; aquí se reflejan como filas de ``ServiceType``
  para poder desactivarlos o cambiarles el nombre sin tocar código.
* En «Nuevo pedido» el tipo se elige de la lista o se escribe: si no existe, se crea como tipo manual.
* Un pedido puede quedar sin tipo de servicio.
"""

from __future__ import annotations

import re

from django.db import transaction

from .models import ServiceType
from .normalize import normalize_key, strip_accents
from .plugins import registry

_labels: dict[str, str] | None = None


def clear_cache(**kwargs) -> None:
    global _labels
    _labels = None


def sync() -> None:
    """Crea las filas de los tipos que aportan los módulos instalados (una sola vez; luego se editan aquí)."""
    existing = set(ServiceType.objects.values_list("key", flat=True))
    new = [ServiceType(key=pt.key, name=pt.label, plugin=pt.plugin)
           for pt in registry.process_types() if pt.key not in existing]
    if new:
        ServiceType.objects.bulk_create(new, ignore_conflicts=True)
        clear_cache()


def label_for(key: str) -> str:
    """Nombre para mostrar de una clave (nombre editado → nombre del módulo → la clave). Vacío si no hay tipo."""
    global _labels
    if not key:
        return ""
    if _labels is None:
        _labels = dict(ServiceType.objects.values_list("key", "name"))
    if key in _labels:
        return _labels[key]
    pt = registry.get_process_type(key)
    return pt.label if pt else key


def active_types() -> list[ServiceType]:
    sync()
    return list(ServiceType.objects.filter(active=True).order_by("name"))


def route_choices() -> list[tuple[str, str]]:
    """Tipos activos con ruta estándar (los que sirven para simular o prometer fecha)."""
    return [(st.key, st.name) for st in active_types() if st.has_route]


def find(text: str) -> ServiceType | None:
    """Busca por clave o por nombre, sin importar mayúsculas, tildes ni espacios."""
    raw = (text or "").strip()
    norm = normalize_key(raw)
    if not norm:
        return None
    sync()
    exact = ServiceType.objects.filter(key=raw).first()
    if exact:
        return exact
    for st in ServiceType.objects.all():
        if normalize_key(st.name) == norm or normalize_key(st.key) == norm:
            return st
    return None


def clean_name(text: str) -> str:
    name = " ".join((text or "").split())[:80]
    if name.isupper():  # «MAQUILA DE JABON» → «Maquila de jabon»
        name = name.capitalize()
    return name[:1].upper() + name[1:]


def _new_key(name: str) -> str:
    base = re.sub(r"[^a-z0-9ñ]+", "-", strip_accents(name.lower())).strip("-")[:36] or "servicio"
    key, n = base, 1
    while ServiceType.objects.filter(key=key).exists():
        n += 1
        key = f"{base}-{n}"
    return key


@transaction.atomic
def resolve(text: str) -> tuple[ServiceType | None, bool]:
    """Texto del pedido → (tipo, creado). Vacío → (None, False). Si no existe se crea como tipo manual activo."""
    name = clean_name(text)
    if not name:
        return None, False
    found = find(name)
    if found:
        return found, False
    return ServiceType.objects.create(key=_new_key(name), name=name), True


def connect_signals() -> None:
    from django.core.signals import request_started
    from django.db.models.signals import post_delete, post_save

    post_save.connect(clear_cache, sender=ServiceType, dispatch_uid="servicetype_cache_save")
    post_delete.connect(clear_cache, sender=ServiceType, dispatch_uid="servicetype_cache_delete")
    request_started.connect(clear_cache, dispatch_uid="servicetype_cache_request")
