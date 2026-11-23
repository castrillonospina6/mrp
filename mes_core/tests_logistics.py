"""Logística: ve los pedidos en tarjetas y solo los marca como mercados (quién, ubicación, observaciones)."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from . import logistics
from .models import Customer, Product, ScheduleChange, Site, WorkOrder


class LogisticsTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="S1", name="Sitio")
        product = Product.objects.create(sku="CAM", name="Camiseta polo")
        cust = Customer.objects.create(name="Almacenes Éxito")

        def order(status, **kw):
            return WorkOrder.objects.create(site=self.site, product=product, customer=cust, process_type="",
                                            quantity=Decimal("144"), status=status, **kw)

        self.planned = order(WorkOrder.Status.PLANNED, external_op="OP-77", notes="Frágil: no apilar")
        self.released = order(WorkOrder.Status.RELEASED)
        self.to_plan = order(WorkOrder.Status.TO_PLAN)
        self.done = order(WorkOrder.Status.DONE)
        User = get_user_model()
        self.log = User.objects.create_user("log", password="x", first_name="Pedro", last_name="Gil")
        self.log.groups.add(Group.objects.get(name="Logística"))
        self.seller = User.objects.create_user("ven", password="x")
        self.seller.groups.add(Group.objects.get(name="Ventas"))

    def test_queue_cards_and_landing(self):
        self.client.force_login(self.log)
        self.assertRedirects(self.client.get("/"), reverse("mes_core:goods_queue"), fetch_redirect_response=False)
        r = self.client.get(reverse("mes_core:goods_queue"), {"site": self.site.pk})
        for text in (self.planned.number, self.released.number, "Almacenes Éxito", "Camiseta polo", "OP-77",
                     "Frágil: no apilar", "Marcar como mercado", 'value="Pedro Gil"', "Por mercar · 2"):
            self.assertContains(r, text)
        self.assertNotContains(r, self.to_plan.number)  # aún sin planear
        self.assertNotContains(r, self.done.number)
        self.assertContains(r, reverse("mes_core:goods_queue"))  # menú «Mercar pedidos»

    def test_mark_picked_and_correct(self):
        self.client.force_login(self.log)
        url = reverse("mes_core:goods_mark", args=[self.planned.pk])
        r = self.client.post(url, {"picked_by": "  Pedro   Gil ", "location": "", "note": ""})
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.goods_status, "pendiente")  # falta la ubicación
        r = self.client.post(url, {"picked_by": "  Pedro   Gil ", "location": "Bodega 2 · estiba 14",
                                   "note": "faltan 2 cajas", "tab": "mercar"})
        self.assertEqual(r.status_code, 302)
        o = WorkOrder.objects.get(pk=self.planned.pk)
        self.assertEqual((o.goods_status, o.goods_picked_by, o.goods_location, o.goods_note, o.goods_pct, o.goods_marked_by),
                         ("mercado", "Pedro Gil", "Bodega 2 · estiba 14", "faltan 2 cajas", 100, self.log))
        self.assertEqual((o.status, o.quantity), (WorkOrder.Status.PLANNED, Decimal("144")))  # el pedido no cambia
        first_at = o.goods_marked_at
        self.client.post(url, {"picked_by": "Pedro Gil", "location": "Bodega 3", "note": ""})
        o.refresh_from_db()
        self.assertEqual((o.goods_location, o.goods_marked_at), ("Bodega 3", first_at))
        r = self.client.get(reverse("mes_core:goods_queue"), {"site": self.site.pk, "tab": "mercados"})
        self.assertContains(r, "Bodega 3")
        self.assertContains(r, "Corregir datos")
        self.assertEqual(ScheduleChange.objects.filter(action="mercancia").count(), 2)
        # la ubicación se sugiere en los siguientes
        self.assertContains(self.client.get(reverse("mes_core:goods_queue"), {"site": self.site.pk}), '<option value="Bodega 3">')

    def test_unmark_needs_reason(self):
        logistics.mark_picked(self.released, "Ana", "Muelle 1", user=self.log)
        with self.assertRaisesMessage(ValidationError, "por qué"):
            logistics.unmark(self.released, "  ", self.log)
        logistics.unmark(self.released, "se marcó el pedido equivocado", self.log)
        self.released.refresh_from_db()
        self.assertEqual((self.released.goods_status, self.released.goods_pct), ("pendiente", 0))
        self.assertIn("se marcó el pedido equivocado", self.released.goods_note)

    def test_rules_and_permissions(self):
        with self.assertRaisesMessage(ValidationError, "no se puede mercar"):
            logistics.mark_picked(self.to_plan, "Ana", "Muelle 1")
        # Ventas ve la pantalla pero no marca
        self.client.force_login(self.seller)
        r = self.client.get(reverse("mes_core:goods_queue"), {"site": self.site.pk})
        self.assertContains(r, self.planned.number)
        self.assertNotContains(r, "Marcar como mercado")
        r = self.client.post(reverse("mes_core:goods_mark", args=[self.planned.pk]), {"picked_by": "x", "location": "y"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(WorkOrder.objects.get(pk=self.planned.pk).goods_status, "pendiente")
        # Logística no edita ni cancela pedidos
        self.client.force_login(self.log)
        r = self.client.get(reverse("mes_core:order_detail", args=[self.planned.pk]))
        self.assertContains(r, "Mercancía (logística)")
        self.client.post(reverse("mes_core:order_cancel", args=[self.planned.pk]))
        self.assertEqual(WorkOrder.objects.get(pk=self.planned.pk).status, WorkOrder.Status.PLANNED)
