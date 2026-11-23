"""Calendario laboral de cada planta.

Combina, para cualquier rango de tiempo:
  * los turnos y sus días laborables (``Shift.weekdays``) y descansos,
  * festivos (la planta no trabaja ese día),
  * cierres (un día, un turno o unas líneas no trabajan),
  * horas extra (un turno se extiende en una fecha) y turnos adicionales (franja que normalmente no existe).

Todo el sistema (programador, Gantt, plan automático, planeación por etapas, capacidad, tablero de planta)
obtiene las ventanas laborables de aquí, así que una excepción se refleja en todas las pantallas.

Reglas:
  * Un festivo o un cierre de todo el día quita los turnos de esa fecha (el turno "pertenece" a la fecha en que
    termina si cruza medianoche, igual que en el resto del sistema).
  * Las horas extra y los turnos adicionales se suman aunque la fecha sea festiva o no laborable
    (así se programa "trabajar el festivo" o "abrir el sábado").
  * Un cierre explícito gana sobre todo lo demás en su franja.

Operarios de cada turno: el reparto de la bolsa global de personal (``StaffAllocation``) si hay uno para esa bodega,
turno y fecha; si no, los operarios configurados en el turno (0 = el total de la planta).
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone

Interval = tuple[datetime, datetime]
_CACHE: dict[int, tuple[float, "SiteCalendar"]] = {}
_TTL = 30.0


def _tz():
    return timezone.get_current_timezone()


def _at(day: date, t: time, tz) -> datetime:
    return datetime.combine(day, t, tzinfo=tz)


def _span(day: date, a: time, b: time, tz) -> Interval:
    """Franja de reloj en una fecha; si b <= a termina al día siguiente."""
    s = _at(day, a, tz)
    e = _at(day + timedelta(days=1) if b <= a else day, b, tz)
    return s, e


def subtract(pieces: list[Interval], holes: list[Interval]) -> list[Interval]:
    for hs, he in holes:
        out = []
        for a, b in pieces:
            if he <= a or hs >= b:
                out.append((a, b))
                continue
            if a < hs:
                out.append((a, hs))
            if he < b:
                out.append((he, b))
        pieces = out
    return pieces


def merge(wins: list[Interval]) -> list[Interval]:
    wins = sorted(w for w in wins if w[1] > w[0])
    out: list[list[datetime]] = []
    for a, b in wins:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


# --- festivos de Colombia --------------------------------------------------------------

def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _next_monday(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7)


def colombia_holidays(year: int) -> list[tuple[date, str]]:
    """Festivos nacionales de Colombia (Ley 51 de 1983, «Ley Emiliani»: varios se trasladan al lunes)."""
    fixed = [((1, 1), "Año Nuevo"), ((5, 1), "Día del Trabajo"), ((7, 20), "Día de la Independencia"),
             ((8, 7), "Batalla de Boyacá"), ((12, 8), "Inmaculada Concepción"), ((12, 25), "Navidad")]
    moved = [((1, 6), "Reyes Magos"), ((3, 19), "San José"), ((6, 29), "San Pedro y San Pablo"),
             ((8, 15), "Asunción de la Virgen"), ((10, 12), "Día de la Raza"), ((11, 1), "Todos los Santos"),
             ((11, 11), "Independencia de Cartagena")]
    e = _easter(year)
    out = [(date(year, m, d), n) for (m, d), n in fixed]
    out += [(_next_monday(date(year, m, d)), n) for (m, d), n in moved]
    out += [(e - timedelta(days=3), "Jueves Santo"), (e - timedelta(days=2), "Viernes Santo"),
            (_next_monday(e + timedelta(days=39)), "Ascensión del Señor"),
            (_next_monday(e + timedelta(days=60)), "Corpus Christi"),
            (_next_monday(e + timedelta(days=68)), "Sagrado Corazón de Jesús")]
    return sorted(out)


# --- calendario de una planta -----------------------------------------------------------

@dataclass(frozen=True)
class Extra:
    """Franja adicional de trabajo (horas extra o turno adicional)."""
    start: datetime
    end: datetime
    operators: int
    label: str
    kind: str
    shift_id: int | None
    lines: frozenset
    exc_id: int
    day: date


@dataclass(frozen=True)
class Closure:
    start: datetime
    end: datetime
    day: date
    shift_id: int | None
    lines: frozenset
    label: str


@dataclass(frozen=True)
class WorkSlot:
    """Qué se trabaja en un instante: un turno normal o una franja adicional."""
    operators: int          # 0 = usa el total de la planta
    label: str
    shift_id: int | None
    extra: bool


class SiteCalendar:
    def __init__(self, site_id: int | None, exceptions=(), allocations=()):
        tz = _tz()
        self.site_id = site_id
        self.allocations = list(allocations)
        self.holidays: dict[date, str] = {}
        self.closures: list[Closure] = []
        self.extras: list[Extra] = []
        self.by_day: dict[date, list] = {}
        for exc in exceptions:
            lines = frozenset(wc.pk for wc in exc.work_centers.all())
            for d in exc.days():
                self.by_day.setdefault(d, []).append(exc)
                if exc.kind == "festivo":
                    self.holidays.setdefault(d, exc.name or "Festivo")
                elif exc.kind == "cierre":
                    if exc.shift_id:
                        from .scheduling import shift_window
                        s, e = shift_window(exc.shift, d, tz)
                    else:
                        s, e = _at(d, time(0), tz), _at(d + timedelta(days=1), time(0), tz)
                    self.closures.append(Closure(s, e, d, exc.shift_id, lines, exc.name or "Cierre"))
                elif exc.start_time and exc.end_time:
                    s, e = _span(d, exc.start_time, exc.end_time, tz)
                    label = (f"{exc.shift.name} · horas extra" if exc.kind == "horas_extra" and exc.shift
                             else exc.name or "Turno adicional")
                    self.extras.append(Extra(s, e, exc.operators, label, exc.kind, exc.shift_id, lines, exc.pk, d))
        self.extras.sort(key=lambda x: x.start)

    # -- reparto de la bolsa global de personal --
    @property
    def has_allocations(self) -> bool:
        return bool(self.allocations)

    def allocation(self, shift, day: date):
        """El reparto que aplica a ese turno ese día: con fechas gana sobre el base, con turno sobre toda la bodega."""
        best, key = None, None
        for a in self.allocations:
            if a.shift_id and a.shift_id != shift.pk:
                continue
            if a.date_from and (a.date_from > day or (a.date_to and a.date_to < day)):
                continue
            k = (a.date_from is not None, a.shift_id is not None, a.date_from or date.min, a.pk)
            if key is None or k > key:
                best, key = a, k
        return best

    def people(self, shift, day: date) -> int:
        """Operarios de ese turno ese día según el reparto; si no hay reparto, los del turno (0 = total de la planta)."""
        a = self.allocation(shift, day) if self.allocations else None
        return a.headcount if a is not None else (shift.operators or 0)

    # -- consultas por fecha --
    def holiday(self, day: date) -> str | None:
        return self.holidays.get(day)

    def _applies(self, lines: frozenset, wc_id: int | None) -> bool:
        return not lines or (wc_id is not None and wc_id in lines)

    def day_closure(self, day: date, shift_id: int | None, wc_id: int | None) -> Closure | None:
        """Cierre que deja sin trabajo ese turno en esa fecha (todo el día o ese turno)."""
        for c in self.closures:
            if c.day == day and (c.shift_id is None or c.shift_id == shift_id) and self._applies(c.lines, wc_id):
                return c
        return None

    def shift_off_reason(self, shift, day: date, wc_id: int | None = None) -> str:
        """'' si el turno trabaja esa fecha; si no, el motivo (no laboral, festivo, cierre)."""
        if not shift.works_on_weekday(day.weekday()):
            return "No laboral"
        h = self.holiday(day)
        if h:
            return f"Festivo · {h}"
        c = self.day_closure(day, shift.pk, wc_id)
        if c:
            return c.label if c.label != "Cierre" else "Cierre"
        return ""

    def overtime_for(self, shift, day: date, wc_id: int | None = None) -> list[Extra]:
        """Horas extra de ese turno en esa fecha (la franja empieza el día del turno o el siguiente)."""
        return [x for x in self.extras if x.kind == "horas_extra" and x.shift_id == shift.pk and x.day == day
                and self._applies(x.lines, wc_id)]

    def extra_shifts_on(self, day: date, wc_id: int | None = None, any_line: bool = False) -> list[Extra]:
        return [x for x in self.extras if x.kind == "turno_adicional" and x.day == day
                and (any_line or self._applies(x.lines, wc_id))]

    # -- ventanas de trabajo --
    def windows(self, shifts, start: datetime, end: datetime, wc_id: int | None = None,
                with_breaks: bool = False) -> list[Interval]:
        """Ventanas laborables (sin descansos) en [start, end) para esos turnos y esa línea (None = planta)."""
        from .scheduling import shift_window, shift_work_windows
        if start >= end:
            return []
        tz = _tz()
        day = start.astimezone(tz).date() - timedelta(days=1)
        last = end.astimezone(tz).date() + timedelta(days=1)
        shift_ids = {s.pk for s in shifts}
        wins: list[Interval] = []
        while day <= last:
            for s in shifts:
                if self.shift_off_reason(s, day, wc_id):
                    continue
                pieces = [shift_window(s, day, tz)] if with_breaks else shift_work_windows(s, day, tz)
                wins.extend(pieces)
            day += timedelta(days=1)
        for x in self.extras:
            if x.end <= start or x.start >= end or not self._applies(x.lines, wc_id):
                continue
            if x.shift_id and shift_ids and x.shift_id not in shift_ids:
                continue  # horas extra de un turno que esta línea no trabaja
            wins.append((x.start, x.end))
        holes = [(c.start, c.end) for c in self.closures
                 if c.end > start and c.start < end and self._applies(c.lines, wc_id)
                 and (c.shift_id is None or not shift_ids or c.shift_id in shift_ids)]
        wins = subtract(merge(wins), holes)
        return [(max(a, start), min(b, end)) for a, b in wins if b > start and a < end]

    def slot_at(self, shifts, t: datetime) -> WorkSlot | None:
        """Qué turno o franja adicional se trabaja en t (para la bolsa de operarios). None = no se trabaja."""
        from .scheduling import shift_window
        for c in self.closures:
            if c.start <= t < c.end and not c.lines and c.shift_id is None:
                return None
        for x in self.extras:
            if x.start <= t < x.end:
                base = next((s for s in shifts if s.pk == x.shift_id), None)
                ops = x.operators or (self.people(base, x.day) if base else 0)
                return WorkSlot(ops, x.label, x.shift_id, True)
        tz = _tz()
        base_day = t.astimezone(tz).date()
        found, people = [], []
        for d in (base_day, base_day + timedelta(days=1)):
            for s in shifts:
                a, b = shift_window(s, d, tz)
                if a <= t < b and not self.shift_off_reason(s, d) and s not in found:
                    found.append(s)
                    people.append(self.people(s, d))
        if not found:
            return None
        if len(found) == 1:
            s = found[0]
            return WorkSlot(people[0], s.name, s.pk, False)
        # Turnos que se cruzan (traslape): su gente se suma. Si alguno usa el total de la planta, manda el total.
        ops = 0 if any(not n for n in people) else sum(people)
        return WorkSlot(ops, " + ".join(s.name for s in found), found[0].pk, False)

    def boundaries(self, shifts, start: datetime, end: datetime) -> set[datetime]:
        """Momentos en que cambia quién trabaja (inicios de turnos y franjas) dentro de (start, end)."""
        from .scheduling import shift_window
        tz = _tz()
        pts: set[datetime] = set()
        day, last = start.astimezone(tz).date() - timedelta(days=1), end.astimezone(tz).date() + timedelta(days=1)
        while day <= last:
            for s in shifts:
                a, _ = shift_window(s, day, tz)
                if start < a < end:
                    pts.add(a)
            day += timedelta(days=1)
        for x in self.extras:
            for p in (x.start, x.end):
                if start < p < end:
                    pts.add(p)
        return pts

    def exceptions_on(self, day: date) -> list:
        return self.by_day.get(day, [])


_EMPTY = SiteCalendar(None)


def get_calendar(site_id: int | None) -> SiteCalendar:
    if not site_id:
        return _EMPTY
    hit = _CACHE.get(site_id)
    now = _time.monotonic()
    if hit and now - hit[0] < _TTL:
        return hit[1]
    from .models import CalendarException, StaffAllocation
    excs = list(CalendarException.objects.filter(site_id=site_id).select_related("shift").prefetch_related("work_centers"))
    allocs = list(StaffAllocation.objects.filter(site_id=site_id))
    cal = SiteCalendar(site_id, excs, allocs)
    _CACHE[site_id] = (now, cal)
    return cal


def invalidate(site_id: int | None = None) -> None:
    if site_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(site_id, None)


def connect_signals() -> None:
    from django.db.models.signals import m2m_changed, post_delete, post_save

    from .models import CalendarException, Shift, StaffAllocation

    def _inv(sender, instance=None, **kwargs):
        invalidate(getattr(instance, "site_id", None))

    for model in (CalendarException, Shift, StaffAllocation):
        post_save.connect(_inv, sender=model, dispatch_uid=f"workcal-save-{model.__name__}")
        post_delete.connect(_inv, sender=model, dispatch_uid=f"workcal-del-{model.__name__}")
    m2m_changed.connect(_inv, sender=CalendarException.work_centers.through, dispatch_uid="workcal-m2m")


def load_colombia_holidays(site, year: int, user=None) -> int:
    """Crea los festivos de Colombia del año que falten. Devuelve cuántos creó."""
    from .models import CalendarException
    existing = set(CalendarException.objects.filter(site=site, kind=CalendarException.Kind.HOLIDAY,
                                                    date__year=year).values_list("date", flat=True))
    created = 0
    for d, name in colombia_holidays(year):
        if d in existing:
            continue
        CalendarException.objects.create(site=site, kind=CalendarException.Kind.HOLIDAY, date=d, name=name, auto=True,
                                         created_by=user if user and user.is_authenticated else None)
        created += 1
    invalidate(site.pk)
    return created
