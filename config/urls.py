from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path

from mes_core.plugins import plugin_urlpatterns
from mes_core.static_serve import serve_static

urlpatterns = [
    # estáticos con caché larga (ver mes_core/static_serve.py); con «runserver» normal los sirve Django antes de llegar aquí
    re_path(r"^static/(?P<path>.+)$", serve_static, name="static"),
    path("admin/", admin.site.urls),
    path("acceder/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"),
    path("salir/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("mes_core.urls")),
    *plugin_urlpatterns(),
]
