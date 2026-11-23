"""Reporte de producción: no se puede reportar más de lo programado salvo como «unidades de más» con motivo."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from . import services
from .models import Operation, Product, ProductionReport, Site, WorkCenter


class OverproductionTests(TestCase):
    def setUp(self):
        site = Site.objects.create(code="S1", name="Sitio")
        for code, kind in (("P1", "picking"), ("V1", "verificacion"), ("D1", "despacho")):
            WorkCenter.objects.create(site=site, code=code, name=code, kind=kind, capacity=1)
        product = Product.objects.create(sku="X", name="Producto X")
        self.order = services.release_order(
            services.create_order(site=site, product=product, process_type="picking", quantity=144))
        self.op = self.order.operations.order_by("sequence").first()

    def test_cannot_report_more_than_pending(self):
        services.report_production(self.op, qty_good=60)
        with self.assertRaisesMessage(ValidationError, "faltan 84"):
            services.report_production(self.op, qty_good=90)
        self.op.refresh_from_db()
        self.assertEqual(self.op.qty_good, Decimal("60"))  # no se registró nada
        services.report_production(self.op, qty_good=84)  # justo lo que falta: sí
        self.op.refresh_from_db()
        self.assertEqual((self.op.qty_good, self.op.qty_pending), (Decimal("144"), Decimal("0")))
        self.assertEqual(self.op.status, Operation.Status.DONE)  # al 100 % se cerró sola
        with self.assertRaisesMessage(ValidationError, "ya está terminada"):
            services.report_production(self.op, qty_good=1)

    def test_extra_units_need_reason_and_are_kept_apart(self):
        services.report_production(self.op, qty_good=60)
        with self.assertRaisesMessage(ValidationError, "motivo"):
            services.report_production(self.op, qty_good=90, allow_extra=True, extra_reason="  ")
        rep = services.report_production(self.op, qty_good=90, allow_extra=True, extra_reason="sobrante de material")
        self.assertEqual((rep.qty_good, rep.qty_extra, rep.extra_reason), (Decimal("84"), Decimal("6"), "sobrante de material"))
        self.op.refresh_from_db()
        self.assertEqual((self.op.qty_good, self.op.qty_extra), (Decimal("144"), Decimal("6")))
        # Marcar la casilla sin excedente no guarda motivo ni unidades de más
        op2 = self.order.operations.order_by("sequence")[1]
        rep2 = services.report_production(op2, qty_good=10, allow_extra=True, extra_reason="x")
        self.assertEqual((rep2.qty_extra, rep2.extra_reason), (Decimal("0"), ""))

    def test_order_done_quantity_excludes_extras(self):
        ops = list(self.order.operations.order_by("sequence"))
        for op in ops[:-1]:
            services.report_production(op, qty_good=144, finished=True)
        services.report_production(ops[-1], qty_good=150, allow_extra=True, extra_reason="cliente pidió más", finished=True)
        self.order.refresh_from_db()
        self.assertEqual(self.order.quantity_done, Decimal("144"))
        self.assertEqual(self.order.progress, 100)

    def test_form_keeps_input_and_shows_message(self):
        self.client.force_login(get_user_model().objects.create_superuser("u", password="p"))
        url = reverse("mes_core:order_detail", args=[self.order.pk])
        services.report_production(self.op, qty_good=60)
        page = self.client.get(url)
        self.assertContains(page, "faltan 84 de 144")
        r = self.client.post(url, {"operation": self.op.pk, "qty_good": "90", "qty_scrap": "0", "comment": "turno 2"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Estás reportando 90 (6 de más)")
        self.assertContains(r, 'value="turno 2"')
        self.assertEqual(ProductionReport.objects.count(), 1)
        r = self.client.post(url, {"operation": self.op.pk, "qty_good": "90", "qty_scrap": "0",
                                   "allow_extra": "on", "extra_reason": "sobrante"}, follow=True)
        self.assertContains(r, "84 cuentan para lo programado y 6 quedaron registradas como unidades de más")
        self.assertContains(r, "Unidades de más:</b> sobrante")
        self.assertEqual(Operation.objects.get(pk=self.op.pk).qty_extra, Decimal("6"))
