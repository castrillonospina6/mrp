from django.urls import path

from . import views

urlpatterns = [
    path("", views.queue, name="queue"),
    path("pedido/<int:pk>/", views.order_eans, name="order"),
    path("pedido/<int:pk>/plantilla.csv", views.order_csv, name="order_csv"),
]
