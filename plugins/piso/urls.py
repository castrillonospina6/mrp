from django.urls import path

from . import overtime_views, views

urlpatterns = [
    # Tablet de planta (operario, tablet de línea, patinador)
    path("", views.tablet, name="tablet"),
    path("app.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="sw"),
    path("api/pulso", views.api_pulse, name="api_pulse"),
    path("api/estado", views.api_state, name="api_state"),
    path("api/entrar", views.api_login, name="api_login"),
    path("api/vincular", views.api_pair, name="api_pair"),
    path("api/salir", views.api_logout, name="api_logout"),
    path("api/llegada", views.api_checkin, name="api_checkin"),
    path("api/extras", views.api_overtime, name="api_overtime"),
    path("api/marcar", views.api_mark, name="api_mark"),
    path("api/paro", views.api_stop, name="api_stop"),
    path("api/paro/cerrar", views.api_stop_close, name="api_stop_close"),
    path("api/pausa", views.api_break, name="api_break"),
    path("api/reanudar", views.api_resume, name="api_resume"),
    path("api/mensaje/leido", views.api_read, name="api_read"),
    path("api/pin", views.api_pin, name="api_pin"),
    path("api/traslado/<str:action>", views.api_transfer, name="api_transfer"),
    path("api/reportar", views.api_report, name="api_report"),
    path("api/mis-registros", views.api_my_records, name="api_my_records"),
    path("api/reporte/anular", views.api_report_undo, name="api_report_undo"),
    path("api/lineas", views.api_sup_lines, name="api_sup_lines"),
    path("api/referencia", views.api_sup_ean, name="api_sup_ean"),
    path("api/mensaje", views.api_sup_message, name="api_sup_message"),
    # MES: supervisores y configuración
    path("en-vivo/", views.live, name="live"),
    path("en-vivo/datos", views.live_data, name="live_data"),
    path("en-vivo/mensaje", views.live_message, name="live_message"),
    path("en-vivo/paro/<int:pk>/cerrar", views.live_close_stop, name="live_close_stop"),
    path("historico/", views.history_view, name="history"),
    path("traslados/", views.transfers_view, name="transfers"),
    path("tablets/", views.codes, name="codes"),
    path("forma-de-reporte/", views.reporting_view, name="reporting"),
    # MES: horas extra (convocatorias de planeación y respuestas desde la tablet)
    path("horas-extra/", overtime_views.overtime_list, name="overtime"),
    path("horas-extra/nueva/", overtime_views.overtime_edit, name="overtime_new"),
    path("horas-extra/<int:pk>/", overtime_views.overtime_detail, name="overtime_detail"),
    path("horas-extra/<int:pk>/editar/", overtime_views.overtime_edit, name="overtime_edit"),
    path("horas-extra/<int:pk>/descargar.xlsx", overtime_views.overtime_xlsx, name="overtime_xlsx"),
]
