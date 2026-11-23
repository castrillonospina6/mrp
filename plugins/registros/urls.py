from django.urls import path

from . import views

urlpatterns = [
    path("", views.records_view, name="records"),
    path("reporte/<int:pk>/editar/", views.report_edit, name="edit"),
    path("reporte/<int:pk>/anular/", views.report_void, name="void"),
    path("anulado/<int:pk>/reactivar/", views.voided_reactivate, name="reactivate"),
    path("anulado/<int:pk>/eliminar/", views.voided_purge, name="purge"),
    path("seguimiento/", views.tracking_view, name="tracking"),
    path("seguimiento/<int:pk>/", views.tracking_detail, name="tracking_detail"),
    path("seguimiento/<int:pk>/cerrar/", views.close_short, name="close_short"),
    path("seguimiento/<int:pk>/reabrir/", views.undo_close, name="undo_close"),
    path("exportar/", views.export_view, name="export"),
]
