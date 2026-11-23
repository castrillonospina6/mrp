"""Eventos del núcleo a los que los plugins pueden suscribirse.

Ejemplo en el ``ready()`` de un plugin::

    from mes_core.signals import order_released
    order_released.connect(mi_funcion)
"""

from django.dispatch import Signal

order_created = Signal()        # kwargs: order
order_released = Signal()       # kwargs: order
order_completed = Signal()      # kwargs: order
production_reported = Signal()  # kwargs: report
plan_generated = Signal()       # kwargs: site, result
