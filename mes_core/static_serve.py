"""Archivos estáticos (.css, .js, íconos) servidos por el propio MES con caché del navegador.

Todas las páginas los piden con ``?v=<fecha de modificación>`` (etiqueta ``{% asset %}``): esa URL no cambia mientras el
archivo no cambie, así que el navegador puede guardarlo un año sin volver a preguntar («immutable»). Al actualizar un
archivo cambia el ``?v=`` y el navegador baja el nuevo. Resultado: al pasar de una pantalla a otra no se descarga ni se
revisa ningún estilo o script; solo llega el HTML.

``iniciar.ps1`` arranca con ``runserver --nostatic`` para que los estáticos pasen por aquí (y por la compresión gzip).
"""

from django.contrib.staticfiles.views import serve

YEAR = 365 * 24 * 3600
FONTS = (".woff2", ".woff", ".ttf")


def serve_static(request, path):
    resp = serve(request, path, insecure=True)
    if resp.status_code in (200, 304):
        if "v" in request.GET:
            resp["Cache-Control"] = f"public, max-age={YEAR}, immutable"
        elif path.endswith(FONTS):  # las fuentes se piden desde el .css (sin ?v=) y no cambian
            resp["Cache-Control"] = f"public, max-age={30 * 24 * 3600}"
        else:
            resp["Cache-Control"] = "no-cache"
    return resp
