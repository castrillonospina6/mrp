from django.urls import path

from . import views

urlpatterns = [
    path("lotes/", views.lot_list, name="lot_list"),
    path("orden/<int:pk>/rendimiento/", views.order_yield, name="order_yield"),
]
