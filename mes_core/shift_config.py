"""Configuración de jornada de una planta: genera los turnos a partir de unos pocos datos.

Datos de entrada (los que llena el usuario):
  * hora de inicio del primer turno y duración (o, equivalente, hora de fin)
  * número de turnos (consecutivos, misma duración)
  * desayuno y almuerzo del primer turno (en los demás turnos se ubican a la misma distancia del inicio)
  * operarios disponibles por turno
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import MAX_BREAK_GROUPS, ScheduleChange, Shift, Site, equivalent_break

DAY = 24 * 60
COLORS = {"Mañana": "#2f80ed", "Tarde": "#16a39a", "Noche": "#5b5bd6"}
OVERLAP_COLOR = "#d97706"


def add_minutes(t: time, minutes: int) -> time:
    total = (t.hour * 60 + t.minute + minutes) % DAY
    return time(total // 60, total % 60)


def _base_name(t: time) -> str:
    if 5 <= t.hour < 12:
        return "Mañana"
    if 12 <= t.hour < 19:
        return "Tarde"
    return "Noche"


@dataclass
class ShiftSpec:
    name: str
    start: time
    end: time
    breakfast_groups: list[tuple[time, time]]
    lunch_groups: list[tuple[time, time]]
    operators: int

    @property
    def breakfast(self) -> tuple[time, time] | None:
        """Descanso equivalente (con grupos escalonados: el tiempo de un grupo)."""
        return equivalent_break(self.breakfast_groups)

    @property
    def lunch(self) -> tuple[time, time] | None:
        return equivalent_break(self.lunch_groups)

    @property
    def effective_minutes(self) -> int:
        brk = sum(Shift.span_minutes(a, b) for a, b in (self.breakfast, self.lunch) if a)
        return Shift.span_minutes(self.start, self.end) - brk


def as_groups(value) -> list[tuple[time, time]]:
    """Acepta None, un solo (desde, hasta) o una lista de ellos."""
    if not value:
        return []
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], time):
        return [value]
    return [tuple(g) for g in value if g and g[0] and g[1]]


def check_groups(start: time, duration_minutes: int, groups: dict[str, list], where: str) -> dict[str, list]:
    """Valida que cada grupo de descanso quede dentro del turno y que desayunos y almuerzos no se crucen.
    Devuelve, por tipo, la lista de (minutos desde el inicio, duración)."""
    offsets = {}
    for label, items in groups.items():
        if len(items) > MAX_BREAK_GROUPS:
            raise ValidationError(f"Máximo {MAX_BREAK_GROUPS} horarios de {label} por turno.")
        offs = []
        for a, b in items:
            start_off = Shift.span_minutes(start, a) % DAY
            length = Shift.span_minutes(a, b)
            if not (0 < start_off and start_off + length <= duration_minutes):
                raise ValidationError(f"El {label} {a:%H:%M}–{b:%H:%M} debe quedar dentro de {where}.")
            offs.append((start_off, length))
        offsets[label] = sorted(offs)
    for a0, al in offsets.get("desayuno", []):
        for b0, bl in offsets.get("almuerzo", []):
            if a0 < b0 + bl and b0 < a0 + al:
                raise ValidationError(f"Un horario de desayuno y uno de almuerzo se cruzan en {where}.")
    return offsets


def build_shift_specs(*, first_start: time, duration_minutes: int, count: int,
                      breakfast=None, lunch=None, operators: list[int]) -> list[ShiftSpec]:
    """``breakfast`` / ``lunch``: un horario (desde, hasta) o varios (grupos escalonados), del primer turno."""
    if not 30 <= duration_minutes <= DAY:
        raise ValidationError("La duración del turno debe estar entre 30 minutos y 24 horas.")
    if count < 1:
        raise ValidationError("Debe haber al menos un turno.")
    if count * duration_minutes > DAY:
        raise ValidationError(f"{count} turnos de {duration_minutes / 60:g} h suman más de 24 horas.")

    offsets = check_groups(first_start, duration_minutes,
                           {"desayuno": as_groups(breakfast), "almuerzo": as_groups(lunch)}, "el primer turno")

    specs, seen = [], {}
    for k in range(count):
        start = add_minutes(first_start, k * duration_minutes)
        base = _base_name(start)
        seen[base] = seen.get(base, 0) + 1
        name = base if seen[base] == 1 else f"{base} {seen[base]}"

        def shifted(key):
            out = []
            for off, length in offsets[key]:
                s = add_minutes(start, off)
                out.append((s, add_minutes(s, length)))
            return out

        specs.append(ShiftSpec(
            name=name, start=start, end=add_minutes(start, duration_minutes),
            breakfast_groups=shifted("desayuno"), lunch_groups=shifted("almuerzo"),
            operators=operators[k] if k < len(operators) else 0,
        ))
    return specs


def day_ordered(shifts: list[Shift]) -> list[Shift]:
    """Turnos empezando por el primero diurno (Mañana, Tarde, Noche), como los piensa la gente."""
    return sorted(shifts, key=lambda s: (s.crosses_midnight, s.start_time))


@transaction.atomic
def apply_shift_config(site: Site, specs: list[ShiftSpec], user=None) -> list[Shift]:
    """Actualiza los turnos base de la planta conservando sus ids (las líneas mantienen sus asignaciones).

    Cada turno nuevo reutiliza primero el registro con el mismo nombre (Mañana → Mañana) y luego,
    en orden, los que sobren; así una línea asignada a «Mañana» sigue en «Mañana».
    Los turnos de traslape no se tocan."""
    overlap_names = set(site.shifts.filter(active=True, is_overlap=True).values_list("name", flat=True))
    clash = [s.name for s in specs if s.name in overlap_names]
    if clash:
        raise ValidationError(f"Ya hay un turno de traslape llamado «{clash[0]}»; cámbiale el nombre primero.")
    base = site.shifts.filter(is_overlap=False)
    existing = day_ordered(list(base.filter(active=True))) + list(base.filter(active=False))
    by_name = {sh.name: sh for sh in existing}
    targets = [by_name.pop(spec.name, None) for spec in specs]
    leftovers = [sh for sh in existing if sh.name in by_name]
    targets = [t if t is not None else (leftovers.pop(0) if leftovers else None) for t in targets]
    for sh in existing:  # nombres temporales para no chocar con la restricción de nombre único
        Shift.objects.filter(pk=sh.pk).update(name=f"__tmp_{sh.pk}")

    result = []
    for spec, target in zip(specs, targets):
        sh = target or Shift(site=site)
        sh.name = spec.name
        sh.start_time, sh.end_time = spec.start, spec.end
        sh.set_break_groups("breakfast", spec.breakfast_groups)
        sh.set_break_groups("lunch", spec.lunch_groups)
        sh.operators = spec.operators
        sh.is_overlap = False
        sh.color = COLORS.get(spec.name.split()[0], sh.color or "#3b82f6")
        sh.active = True
        sh.clean()
        sh.save()
        result.append(sh)
    used = {sh.pk for sh in result}
    for sh in existing:
        if sh.pk not in used:
            Shift.objects.filter(pk=sh.pk).update(active=False, name=f"Inactivo #{sh.pk}")

    first = specs[0]
    ScheduleChange.objects.create(
        site=site, action=ScheduleChange.Action.CONFIG,
        description=f"Turnos: {len(specs)} × {Shift.span_minutes(first.start, first.end) / 60:g} h desde "
                    f"{first.start:%H:%M}" + (f", operarios por turno: {'/'.join(str(s.operators or site.operators_total) for s in specs)}"
                                    if site.operators_total or any(s.operators for s in specs) else ""),
        user=user if user and user.is_authenticated else None,
    )
    return result


def current_config(site: Site) -> dict:
    """Valores actuales para precargar el formulario (solo turnos base)."""
    shifts = day_ordered(list(site.shifts.filter(active=True, is_overlap=False)))
    if not shifts:
        return {"first_start": time(6), "duration_minutes": 480, "count": 1, "breakfast": [], "lunch": [],
                "operators": [0], "shifts": []}
    first = shifts[0]
    return {
        "first_start": first.start_time,
        "duration_minutes": first.duration_minutes,
        "count": len(shifts),
        "breakfast": first.breakfast_groups,
        "lunch": first.lunch_groups,
        "operators": [s.operators for s in shifts],
        "shifts": shifts,
    }


# ---------------------------------------------------------------- turnos de traslape
@dataclass
class OverlapSpec:
    pk: int | None
    name: str
    start: time | None
    end: time | None
    operators: int
    breakfast_groups: list
    lunch_groups: list
    delete: bool = False


@transaction.atomic
def save_overlap_shifts(site: Site, specs: list[OverlapSpec], user=None) -> list[str]:
    """Crea, actualiza o retira (desactiva) los turnos de traslape. Devuelve los cambios hechos."""
    base_days = (site.shifts.filter(active=True, is_overlap=False).order_by("start_time").values_list("weekdays", flat=True)
                 .first())
    own = {sh.pk: sh for sh in site.shifts.filter(is_overlap=True)}
    taken = {sh.name.lower(): sh.pk for sh in site.shifts.filter(active=True)}
    changes = []
    for spec in specs:
        sh = own.get(spec.pk) if spec.pk else None
        if spec.pk and sh is None:
            raise ValidationError("Uno de los turnos de traslape ya no existe; recarga la página.")
        if spec.delete:
            if sh and sh.active:
                old = sh.name
                Shift.objects.filter(pk=sh.pk).update(active=False, name=f"Inactivo #{sh.pk}")
                taken.pop(old.lower(), None)
                changes.append(f"retirado {old}")
            continue
        name = " ".join((spec.name or "").split())[:40]
        if not name:
            raise ValidationError("Ponle nombre a cada turno de traslape (p. ej. «Traslape»).")
        if not (spec.start and spec.end) or spec.start == spec.end:
            raise ValidationError(f"Indica la hora de inicio y de fin de «{name}».")
        if taken.get(name.lower(), sh.pk if sh else None) != (sh.pk if sh else None):
            raise ValidationError(f"Ya existe un turno llamado «{name}» en {site.code}.")
        duration = Shift.span_minutes(spec.start, spec.end)
        check_groups(spec.start, duration, {"desayuno": spec.breakfast_groups, "almuerzo": spec.lunch_groups},
                     f"«{name}» ({spec.start:%H:%M}–{spec.end:%H:%M})")
        created = sh is None
        if created:
            sh = Shift(site=site, is_overlap=True, color=OVERLAP_COLOR, weekdays=base_days or "0123456")
        if sh.name:
            taken.pop(sh.name.lower(), None)
        sh.name, sh.start_time, sh.end_time, sh.operators = name, spec.start, spec.end, max(0, spec.operators or 0)
        sh.set_break_groups("breakfast", spec.breakfast_groups)
        sh.set_break_groups("lunch", spec.lunch_groups)
        sh.active = True
        sh.clean()
        sh.save()
        taken[name.lower()] = sh.pk
        brk = "sin desayuno" if not spec.breakfast_groups else f"desayuno {Shift.groups_label(spec.breakfast_groups)}"
        changes.append(f"{'nuevo ' if created else ''}{name} {spec.start:%H:%M}–{spec.end:%H:%M} ({brk})")
    if changes:
        ScheduleChange.objects.create(
            site=site, action=ScheduleChange.Action.CONFIG, description=("Traslapes: " + "; ".join(changes))[:255],
            user=user if user and user.is_authenticated else None)
    return changes
