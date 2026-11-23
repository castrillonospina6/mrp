"""Revisiones automáticas: asistencia sin confirmar, pedidos en riesgo y sugerencias de polivalencia.

La aplicación ya las corre sola (máx. cada 5 min) mientras alguien la usa. Para que corran aunque nadie entre,
prográmalo en el Programador de tareas de Windows cada 15 minutos:

    python manage.py mes_avisos
"""

from django.core.management.base import BaseCommand

from mes_core.notify import run_checks


class Command(BaseCommand):
    help = "Genera los avisos por tiempo (asistencia, atrasos, polivalencia)."

    def handle(self, *args, **options):
        out = run_checks(force=True)
        self.stdout.write(", ".join(f"{k}: {v}" for k, v in out.items()) or "sin revisiones")
