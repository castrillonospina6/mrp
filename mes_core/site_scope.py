"""Qué bodega (planta) mira cada pantalla: una sola o «Todas».

* ``?site=<pk>`` elige una bodega; ``?site=todas`` las elige todas (solo en las pantallas que lo admiten).
* La elección se recuerda en la sesión: al pasar a otra pantalla se sigue viendo lo mismo sin volver a elegir.
* Sin elección previa, las pantallas que admiten «Todas» arrancan en Todas; las que son de una sola bodega
  (turnos, calendario, reglas…) arrancan en la bodega con más líneas y personal, no en la primera de la lista.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Count, Q

from .models import Site

ALL = "todas"
SESSION_KEY = "mes_site"
SESSION_KEY_ONE = "mes_site_one"


@dataclass
class Scope:
    site: Site | None              # None = todas las bodegas
    sites: list = field(default_factory=list)
    allow_all: bool = False

    @property
    def all(self) -> bool:
        return self.site is None

    @property
    def param(self) -> str:
        return ALL if self.site is None else str(self.site.pk)

    @property
    def ids(self) -> list[int]:
        return [s.pk for s in self.sites] if self.site is None else [self.site.pk]

    def q(self, field_name: str = "site") -> Q:
        """Filtro para un queryset: ``qs.filter(scope.q("order__site"))``."""
        return Q() if self.site is None else Q(**{field_name: self.site})

    @property
    def label(self) -> str:
        return "Todas las bodegas" if self.site is None else self.site.name


def busiest(sites):
    """La bodega con más líneas activas (y, a igualdad, más personal): la que más se usa."""
    return (Site.objects.filter(pk__in=[s.pk for s in sites])
            .annotate(nw=Count("work_centers", filter=Q(work_centers__active=True), distinct=True),
                      ne=Count("employees", filter=Q(employees__active=True), distinct=True))
            .order_by("-nw", "-ne", "code").first())


def pick(request, *, allow_all: bool = True) -> Scope:
    sites = list(Site.objects.filter(active=True).order_by("code"))
    by_pk = {str(s.pk): s for s in sites}
    raw = str(request.POST.get("site") or request.GET.get("site") or "").strip().lower()
    session = getattr(request, "session", None)
    # Dos memorias: la de las pantallas generales (puede ser «Todas») y la de las de una sola bodega; así elegir una
    # bodega en Turnos no le quita «Todas» a Capacidad, y elegir una en Capacidad sí la propone en Turnos.
    key = SESSION_KEY if allow_all else SESSION_KEY_ONE
    remembered = ""
    if session is not None:
        remembered = str(session.get(key, ""))
        if not allow_all and remembered not in by_pk:
            remembered = str(session.get(SESSION_KEY, ""))
    chosen = None
    if raw == ALL or raw in by_pk:
        chosen = raw
        if session is not None and session.get(key) != raw and (allow_all or raw != ALL):
            session[key] = raw
    elif remembered == ALL or remembered in by_pk:
        chosen = remembered
    if not sites:
        return Scope(None, sites, allow_all)
    if chosen in by_pk:
        return Scope(by_pk[chosen], sites, allow_all)
    if allow_all:
        return Scope(None, sites, allow_all)
    return Scope(busiest(sites) or sites[0], sites, allow_all)


def site_and_sites(request):
    """Para las pantallas de UNA bodega (turnos, calendario, reglas…): ``(site, sites)`` como antes, pero con la
    bodega recordada (o la más usada) en vez de la primera de la lista."""
    s = pick(request, allow_all=False)
    return s.site, s.sites
