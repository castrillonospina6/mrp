"""Permisos: el supervisor consulta el programador y los pedidos, pero no los modifica."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from . import services
from .models import Product, Site, WorkCenter, WorkOrder


class SupervisorReadOnlyTests(TestCase):
    def setUp(self):
        site = Site.objects.create(code="S1", name="Sitio")
        for code, kind in (("P1", "picking"), ("V1", "verificacion"), ("D1", "despacho")):
            WorkCenter.objects.create(site=site, code=code, name=code, kind=kind, capacity=1)
        product = Product.objects.create(sku="X", name="Producto X")
        self.order = services.create_order(site=site, product=product, process_type="picking", quantity=Decimal("10"))
        User = get_user_model()
        self.sup = User.objects.create_user("sup", password="x")
        self.sup.groups.add(Group.objects.get(name="Supervisor"))
        self.planner = User.objects.create_user("plan", password="x")
        self.planner.groups.add(Group.objects.get(name="Planeación"))

    def test_supervisor_cannot_cancel_or_release(self):
        self.client.force_login(self.sup)
        page = self.client.get(reverse("mes_core:order_detail", args=[self.order.pk]))
        self.assertNotContains(page, reverse("mes_core:order_cancel", args=[self.order.pk]))
        self.assertNotContains(page, reverse("mes_core:order_release", args=[self.order.pk]))
        self.client.post(reverse("mes_core:order_cancel", args=[self.order.pk]))
        self.client.post(reverse("mes_core:order_release", args=[self.order.pk]))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.DRAFT)

    def test_planner_can_cancel(self):
        self.client.force_login(self.planner)
        page = self.client.get(reverse("mes_core:order_detail", args=[self.order.pk]))
        self.assertContains(page, reverse("mes_core:order_cancel", args=[self.order.pk]))
        self.client.post(reverse("mes_core:order_cancel", args=[self.order.pk]))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.CANCELLED)

    def test_scheduler_is_read_only_for_supervisor(self):
        self.client.force_login(self.sup)
        page = self.client.get(reverse("mes_core:scheduler"))
        self.assertContains(page, 'data-can-plan="0"')
        self.assertContains(page, "Solo consulta")
        self.assertNotContains(page, 'id="auto"')
        self.assertNotContains(page, 'id="import"')
        op = self.order.operations.first()
        r = self.client.post(reverse("mes_core:api_unschedule"), {"operation": op.pk}, content_type="application/json")
        self.assertEqual(r.status_code, 403)
        self.client.force_login(self.planner)
        page = self.client.get(reverse("mes_core:scheduler"))
        self.assertContains(page, 'data-can-plan="1"')
        self.assertContains(page, 'id="auto"')
