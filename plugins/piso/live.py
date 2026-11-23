"""Tiempo real sin conexiones abiertas: cada pantalla pregunta cada pocos segundos por los «pulsos» que le interesan
(números de versión en la caché) y solo vuelve a pedir sus datos cuando alguno cambió.

Pulsos:
    g        cambios del MES que afectan a todos (cuadrillas, operaciones, pedidos liberados, asistencia)
    e<id>    lo de un operario (sus marcaciones, paros, mensajes)
    l<id>    lo de una línea (marcaciones y paros de su gente, mensajes a la línea)
    t        traslados
    b        tablero en vivo de los supervisores (cualquier marcación o paro)

La caché por defecto vive en el proceso del servidor (runserver). Con varios procesos configura una caché compartida.
"""

from __future__ import annotations

from contextvars import ContextVar

from django.core.cache import cache
from django.db import transaction

PREFIX = "piso:v:"
# Mientras se guarda una marcación, los cambios que hace en la operación y el pedido NO avisan a todas las tablets
# (solo a la del operario, su línea y el tablero); si la marcación cierra la operación se avisa a todos aparte.
quiet = ContextVar("piso_quiet", default=False)


def _incr(keys) -> None:
    for k in keys:
        try:
            cache.incr(PREFIX + k)
        except ValueError:  # aún no existe
            cache.set(PREFIX + k, 2, None)


def bump(*keys: str) -> None:
    """Avisa que cambió algo (al confirmar la transacción, para que la pantalla no lea datos viejos)."""
    keys = tuple(dict.fromkeys(k for k in keys if k))
    if keys:
        transaction.on_commit(lambda: _incr(keys))


def bump_employee(emp_id, wc_id=None, *extra) -> None:
    bump(f"e{emp_id}" if emp_id else "", f"l{wc_id}" if wc_id else "", "b", *extra)


def versions(keys) -> str:
    keys = [k for k in keys if k and len(k) <= 12][:24]  # el supernumerario escucha cada una de sus líneas
    got = cache.get_many([PREFIX + k for k in keys])
    return "|".join(f"{k}:{got.get(PREFIX + k, 1)}" for k in keys)


def connect_signals() -> None:
    from django.db.models.signals import post_delete, post_save

    from mes_core.models import Attendance, CrewAssignment, Operation, WorkOrder

    def _global(sender, **kwargs):
        if not quiet.get():
            bump("g", "b")

    for model in (CrewAssignment, Operation, WorkOrder, Attendance):
        post_save.connect(_global, sender=model, dispatch_uid=f"piso-live-save-{model.__name__}")
        post_delete.connect(_global, sender=model, dispatch_uid=f"piso-live-del-{model.__name__}")

    # Si en el MES se elimina un reporte de producción, se borran también las marcaciones de piso que lo formaron
    # (así el histórico y la eficiencia de la tablet cuadran con lo que quedó en el pedido).
    from django.db.models.signals import pre_delete

    from mes_core.models import ProductionReport

    def _drop_marks(sender, instance, **kwargs):
        from .models import Mark

        if Mark.objects.filter(report_id=instance.pk).delete()[0]:
            bump("g", "b")

    pre_delete.connect(_drop_marks, sender=ProductionReport, dispatch_uid="piso-drop-marks-of-report")
