"""{% asset 'ruta' %}: URL estática con ?v=<fecha de modificación>.

Evita que el navegador o el túnel sigan usando un .js/.css viejo después de actualizar, y deja que el navegador lo
guarde sin volver a preguntar mientras no cambie (ver mes_core/static_serve.py).
"""

import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()
_FOUND: dict[str, str | None] = {}  # ruta → archivo en disco (buscarlo recorre todas las apps; la fecha sí se lee siempre)


@register.simple_tag
def asset(path):
    url = static(path)
    if path not in _FOUND:
        _FOUND[path] = finders.find(path)
    found = _FOUND[path]
    if not found:
        _FOUND.pop(path, None)
        return url
    try:
        return f"{url}?v={int(os.path.getmtime(found))}"
    except OSError:
        _FOUND.pop(path, None)
        return url
