from django.urls import path

from . import views

urlpatterns = [
    path("fichas/", views.spec_list, name="spec_list"),
    path("orden/<int:pk>/insumos/", views.order_materials, name="order_materials"),
]
