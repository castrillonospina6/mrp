"""Tipos de servicio: catálogo activable, texto libre o vacío en «Nuevo pedido»."""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import service_types
from .models import ServiceType, Site, WorkOrder


class ServiceTypeTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="CEDI", name="Centro")
        User = get_user_model()
        self.seller = User.objects.create_user("ventas", password="x")
        self.seller.groups.add(Group.objects.get(name="Ventas"))
        self.planner = User.objects.create_user("plan", password="x")
        self.planner.groups.add(Group.objects.get(name="Planeación"))

    def _intake(self, process_type, **kw):
        today = timezone.localdate()
        data = {"site": self.site.pk, "process_type": process_type, "product": "Camiseta", "quantity": "10",
                "received_on": today.isoformat(), "delivery_date": (today + timedelta(days=10)).isoformat(),
                "transit_days": "1", "priority": "3", **kw}
        self.client.force_login(self.seller)
        return self.client.post(reverse("mes_core:order_create"), data)

    def test_plugin_types_are_synced_and_can_be_deactivated(self):
        service_types.sync()
        food = ServiceType.objects.get(key="deshuese")
        self.assertEqual((food.plugin, food.active, food.has_route), ("food", True, True))
        self.client.force_login(self.planner)
        url = reverse("mes_core:service_type_list")
        page = self.client.get(url)
        self.assertContains(page, "Deshuese de pollo")
        post = {"action": "save", "st": [st.pk for st in ServiceType.objects.all()]}
        for st in ServiceType.objects.all():
            post[f"name_{st.pk}"] = st.name
            if st.key != "deshuese":
                post[f"active_{st.pk}"] = "on"
        post[f"name_{ServiceType.objects.get(key='picking').pk}"] = "Alistamiento de pedidos"
        self.assertEqual(self.client.post(url, post).status_code, 302)
        food.refresh_from_db()
        self.assertFalse(food.active)
        self.client.force_login(self.seller)
        form = self.client.get(reverse("mes_core:order_create"))
        self.assertNotContains(form, "Deshuese de pollo")
        self.assertContains(form, 'data-value="Alistamiento de pedidos"')

    def test_intake_accepts_list_typed_or_blank(self):
        r = self._intake("ACONDICIONADO de producto")  # de la lista, escrito distinto
        self.assertEqual(r.status_code, 302)
        self.assertEqual(WorkOrder.objects.latest("pk").process_type, "acondicionado")
        r = self._intake("  MAQUILA DE JABÓN  ")  # nuevo: se crea como tipo manual
        self.assertEqual(r.status_code, 302)
        st = ServiceType.objects.get(name="Maquila de jabón")
        self.assertEqual((st.key, st.plugin, st.active), ("maquila-de-jabon", "", True))
        order = WorkOrder.objects.latest("pk")
        self.assertEqual((order.process_type, order.process_label), ("maquila-de-jabon", "Maquila de jabón"))
        self.assertEqual(order.status, WorkOrder.Status.TO_PLAN)
        self._intake("maquila de jabon")  # mismo nombre sin tilde: no duplica
        self.assertEqual(ServiceType.objects.filter(plugin="").count(), 1)
        r = self._intake("")  # en blanco
        self.assertEqual(r.status_code, 302)
        blank = WorkOrder.objects.latest("pk")
        self.assertEqual((blank.process_type, blank.process_label), ("", "Sin tipo de servicio"))
        self.assertContains(self.client.get(reverse("mes_core:order_list")), "Sin tipo de servicio")

    def test_promise_needs_a_route(self):
        self.client.force_login(self.seller)
        url = reverse("mes_core:api_promise")
        body = {"site": self.site.pk, "process_type": "", "quantity": 10}
        r = self.client.post(url, body, content_type="application/json")
        self.assertIn("Sin tipo de servicio", r.json()["error"])
        service_types.resolve("Lavado")
        r = self.client.post(url, {**body, "process_type": "lavado"}, content_type="application/json")
        self.assertIn("no tiene ruta estándar", r.json()["error"])

    def test_add_and_delete_manual_type(self):
        self.client.force_login(self.planner)
        url = reverse("mes_core:service_type_list")
        self.client.post(url, {"action": "add", "name": "Reempaque"})
        st = ServiceType.objects.get(name="Reempaque")
        self.client.post(url, {"action": "add", "name": "REEMPAQUE"})
        self.assertEqual(ServiceType.objects.filter(name__iexact="reempaque").count(), 1)
        WorkOrder.objects.create(site=self.site, product_id=self._product(), process_type=st.key, quantity=1)
        self.client.post(url, {"action": "delete", "id": st.pk})
        self.assertTrue(ServiceType.objects.filter(pk=st.pk).exists())  # tiene pedidos: no se borra
        WorkOrder.objects.filter(process_type=st.key).delete()
        self.client.post(url, {"action": "delete", "id": st.pk})
        self.assertFalse(ServiceType.objects.filter(pk=st.pk).exists())
        food = ServiceType.objects.get(key="deshuese")
        self.client.post(url, {"action": "delete", "id": food.pk})
        self.assertTrue(ServiceType.objects.filter(pk=food.pk).exists())  # los de módulo solo se desactivan

    def test_sellers_cannot_open_settings(self):
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(reverse("mes_core:service_type_list")).status_code, 403)

    def _product(self):
        from .models import Product

        return Product.objects.get_or_create(sku="P1", defaults={"name": "P"})[0].pk
