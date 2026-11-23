"""``{% site_chips %}``: botones para elegir la bodega (con «Todas» donde se puede) en vez de una lista desplegable.

Lee ``site``, ``sites`` y ``site_allow_all`` del contexto; cada botón conserva los demás filtros de la dirección.
"""

from django import template

from mes_core.site_scope import ALL

register = template.Library()


@register.inclusion_tag("mes_core/_site_chips.html", takes_context=True)
def site_chips(context, allow_all=None):
    request = context.get("request")
    sites = list(context.get("sites") or [])
    site = context.get("site")
    if allow_all is None:
        allow_all = bool(context.get("site_allow_all"))
    params = request.GET.copy() if request is not None else None

    def url(value):
        if params is None:
            return f"?site={value}"
        p = params.copy()
        p.pop("page", None)
        p["site"] = value
        return "?" + p.urlencode()

    items = []
    if allow_all:
        items.append({"label": "Todas", "title": "Todas las bodegas: toda la capacidad y todo el personal",
                      "url": url(ALL), "on": site is None})
    for s in sites:
        items.append({"label": s.name, "title": s.code, "url": url(s.pk), "on": site is not None and s.pk == site.pk})
    return {"items": items, "show": len(sites) > 1}
