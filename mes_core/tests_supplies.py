"""Insumos: planeación envía → insumos confirma, entrega y libera a planta → el supervisor ejecuta."""

import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from . import logistics, services, supplies
from .models import PlanningRules, Product, Site, SupplyMovement, SupplyRequirement, SupplyStock, WorkCenter, WorkOrder


def xlsx(rows) -> bytes:
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class SuppliesFlowTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="S1", name="Sitio")
        for code, kind in (("P1", "picking"), ("V1", "verificacion"), ("D1", "despacho")):
            WorkCenter.objects.create(site=self.site, code=code, name=code, kind=kind, capacity=1)
        product = Product.objects.create(sku="CAM", name="Camiseta")
        self.order = services.create_order(site=self.site, product=product, process_type="picking", quantity=Decimal("144"))
        self.label = SupplyStock.objects.create(site=self.site, product=Product.objects.create(sku="ETQ", name="Etiqueta",
                                                                                               kind="insumo"),
                                                safety_stock=Decimal("50"))
        supplies.receive(self.label, 100)
        User = get_user_model()

        def user(name, group):
            u = User.objects.create_user(name, password="x")
            u.groups.add(Group.objects.get(name=group))
            return u

        self.planner, self.ins, self.sup, self.seller = (user("plan", "Planeación"), user("ins", "Insumos"),
                                                         user("sup", "Supervisor"), user("ven", "Ventas"))

    def _send(self):
        self.client.force_login(self.planner)
        self.client.post(reverse("mes_core:order_send_supplies", args=[self.order.pk]))
        self.order.refresh_from_db()

    def test_planner_sends_and_cannot_release(self):
        self.client.force_login(self.planner)
        page = self.client.get(reverse("mes_core:order_detail", args=[self.order.pk]))
        self.assertContains(page, "Enviar a insumos")
        self.assertNotContains(page, "Revisar insumos y liberar")
        self._send()
        self.assertEqual(self.order.status, WorkOrder.Status.SUPPLIES)
        self.client.post(reverse("mes_core:order_release", args=[self.order.pk]))
        r = self.client.post(reverse("mes_core:supply_order", args=[self.order.pk]), {"action": "release"})
        self.assertEqual(r.status_code, 403)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.SUPPLIES)
        self.assertEqual(self.client.get(reverse("mes_core:supply_order", args=[self.order.pk])).status_code, 200)  # ve

    def test_lines_deliveries_and_release(self):
        self._send()
        o = self.order
        supplies.save_lines(o, [supplies.LineInput(pk=None, stock_id=self.label.pk, qty_required="144")], self.ins)
        line = o.supply_lines.get()
        with self.assertRaisesMessage(ValidationError, "no alcanza"):
            supplies.deliver(o, {line.pk: "120"}, self.ins)   # solo hay 100
        supplies.deliver(o, {line.pk: "100"}, self.ins)
        self.label.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual((self.label.on_hand, line.qty_released, line.qty_pending), (Decimal("0"), Decimal("100"), Decimal("44")))
        self.assertEqual(SupplyMovement.objects.filter(kind="entrega").get().balance, Decimal("0"))
        with self.assertRaisesMessage(ValidationError, "Faltan insumos"):
            supplies.release_to_plant(o, self.ins)
        with self.assertRaisesMessage(ValidationError, "por qué"):
            supplies.release_to_plant(o, self.ins, partial_ok=True)
        supplies.release_to_plant(o, self.ins, partial_ok=True, note="las 44 llegan mañana")
        o.refresh_from_db()
        # insumos liberó su parte, pero falta logística: aún no pasa a planta
        self.assertEqual((o.status, o.supplies_pct, o.supplies_released_by), (WorkOrder.Status.SUPPLIES, 69, self.ins))
        self.assertIn("llegan mañana", o.supplies_note)
        logistics.mark_picked(o, "Ana", "Muelle 1", user=self.ins)
        o.refresh_from_db()
        self.assertEqual(o.status, WorkOrder.Status.RELEASED)
        supplies.receive(self.label, 50)
        supplies.deliver(o, {line.pk: "44"}, self.ins)  # entrega posterior, ya en planta
        o.refresh_from_db()
        self.assertEqual(o.supplies_pct, 100)
        with self.assertRaisesMessage(ValidationError, "faltan 0"):
            supplies.deliver(o, {line.pk: "1"}, self.ins)

    def test_order_page_release_and_return(self):
        self._send()
        logistics.mark_picked(self.order, "Ana", "Muelle 1")  # logística ya mercó
        self.client.force_login(self.ins)
        url = reverse("mes_core:supply_order", args=[self.order.pk])
        self.assertContains(self.client.get(url), "Entregar y liberar a planta")
        r = self.client.post(url, {"action": "release", "line": ["n1"], "stock_n1": self.label.pk, "req_n1": "80",
                                   "op_n1": "", "to_n1": str(self.sup.pk), "note_n1": "", "now_n1": "80"})
        self.assertEqual(r.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.RELEASED)
        line = SupplyRequirement.objects.get()
        self.assertEqual((line.qty_released, line.deliver_to), (Decimal("80"), self.sup))
        # otro pedido: se devuelve a planeación con motivo
        other = services.create_order(site=self.site, product=self.order.product, process_type="picking", quantity=10)
        supplies.send_to_supplies(other)
        url2 = reverse("mes_core:supply_order", args=[other.pk])
        self.client.post(url2, {"action": "return", "reason": ""})
        other.refresh_from_db()
        self.assertEqual(other.status, WorkOrder.Status.SUPPLIES)
        self.client.post(url2, {"action": "return", "reason": "no hay etiqueta hasta el viernes"})
        other.refresh_from_db()
        self.assertEqual(other.status, WorkOrder.Status.DRAFT)
        self.client.force_login(self.planner)
        self.assertContains(self.client.get(reverse("mes_core:order_detail", args=[other.pk])), "no hay etiqueta hasta el viernes")

    def test_errors_keep_typed_rows(self):
        self._send()
        self.client.force_login(self.ins)
        url = reverse("mes_core:supply_order", args=[self.order.pk])
        r = self.client.post(url, {"action": "deliver", "line": ["n1"], "stock_n1": self.label.pk, "req_n1": "500",
                                   "now_n1": "500", "op_n1": "", "to_n1": "", "note_n1": "urgente"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "no alcanza")
        self.assertContains(r, 'value="urgente"')
        self.assertFalse(SupplyRequirement.objects.exists())  # todo o nada

    def test_only_execution_roles_report_production(self):
        self._send()
        supplies.release_to_plant(self.order, self.ins)  # sin líneas: no necesita insumos
        logistics.mark_picked(self.order, "Ana", "Muelle 1")
        op = self.order.operations.first()
        url = reverse("mes_core:order_detail", args=[self.order.pk])
        self.client.force_login(self.seller)
        self.assertNotContains(self.client.get(url), "Registrar reporte")
        self.client.post(url, {"operation": op.pk, "qty_good": "5", "qty_scrap": "0"})
        op.refresh_from_db()
        self.assertEqual(op.qty_good, 0)
        self.client.force_login(self.sup)
        self.assertContains(self.client.get(url), "Registrar reporte")
        self.client.post(url, {"operation": op.pk, "qty_good": "5", "qty_scrap": "0"})
        op.refresh_from_db()
        self.assertEqual(op.qty_good, 5)

    def test_inventory_screens_and_import(self):
        self.client.force_login(self.ins)
        url = reverse("mes_core:supply_inventory")
        self.client.post(url, {"site": self.site.pk, "action": "new", "product": "Caja corrugada x24", "unit": "UN",
                               "on_hand": "30", "safety": "40", "location": "B-3"})
        caja = SupplyStock.objects.get(product__name="Caja corrugada x24")
        self.assertEqual((caja.on_hand, caja.safety_stock), (Decimal("30"), Decimal("40")))
        page = self.client.get(url, {"site": self.site.pk})
        self.assertContains(page, "Bajo stock de seguridad")
        self.client.post(url, {"site": self.site.pk, "action": "receive", "id": caja.pk, "qty": "20", "note": "factura 1"})
        self.client.post(url, {"site": self.site.pk, "action": "adjust", "id": caja.pk, "qty": "48"})
        caja.refresh_from_db()
        self.assertEqual(caja.on_hand, Decimal("48"))
        self.assertEqual(list(caja.movements.order_by("pk").values_list("kind", flat=True)), ["ajuste", "entrada", "ajuste"])
        up = SimpleUploadedFile("inv.xlsx", xlsx([["Código", "Nombre", "Unidad", "Existencia", "Stock de seguridad"],
                                                   ["ETQ", "Etiqueta", "und", 500, 100], ["BLS-1", "Bolsa", "UN", 80, ""]]))
        r = self.client.post(reverse("mes_core:supply_import"), {"site": self.site.pk, "file": up})
        self.assertEqual(r.status_code, 302)
        self.label.refresh_from_db()
        self.assertEqual((self.label.on_hand, self.label.safety_stock), (Decimal("500"), Decimal("100")))
        self.assertTrue(SupplyStock.objects.filter(product__sku="BLS-1", on_hand=80).exists())
        bad = SimpleUploadedFile("inv.xlsx", xlsx([["codigo", "nombre", "existencia"], ["X1", "Nuevo", "-5"]]))
        self.client.post(reverse("mes_core:supply_import"), {"site": self.site.pk, "file": bad})
        self.assertFalse(Product.objects.filter(sku="X1").exists())
        self.assertEqual(self.client.get(reverse("mes_core:supply_movements"), {"site": self.site.pk}).status_code, 200)
        self.client.force_login(self.planner)  # planeación consulta pero no modifica
        r = self.client.post(url, {"site": self.site.pk, "action": "adjust", "id": caja.pk, "qty": "1"})
        self.assertEqual(r.status_code, 403)

    def _line(self, qty="80"):
        supplies.save_lines(self.order, [supplies.LineInput(pk=None, stock_id=self.label.pk, qty_required=qty)], self.ins)
        return self.order.supply_lines.get()

    def test_auto_release_insumos_first_then_logistics(self):
        self._send()
        line = self._line()
        supplies.deliver(self.order, {line.pk: "50"}, self.ins)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.supplies_released_at)  # 62 %: aún no
        supplies.deliver(self.order, {line.pk: "30"}, self.ins)
        self.order.refresh_from_db()
        # 100 % entregado → insumos liberado solo, pero el pedido espera a logística
        self.assertEqual((self.order.status, self.order.supplies_released_by), (WorkOrder.Status.SUPPLIES, self.ins))
        self.assertEqual(supplies.waiting_for(self.order), ["logística"])
        self.client.force_login(self.ins)
        page = self.client.get(reverse("mes_core:supplies_queue"), {"tab": "logistica"})
        self.assertContains(page, self.order.number)
        logistics.mark_picked(self.order, "Ana", "Muelle 1")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.RELEASED)

    def test_auto_release_logistics_first(self):
        self._send()
        logistics.mark_picked(self.order, "Ana", "Muelle 1")  # logística termina antes que insumos
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.SUPPLIES)
        line = self._line()
        self.client.force_login(self.ins)
        r = self.client.post(reverse("mes_core:supply_order", args=[self.order.pk]),
                             {"action": "deliver", "line": [str(line.pk)], f"stock_{line.pk}": self.label.pk,
                              f"req_{line.pk}": "80", f"now_{line.pk}": "80", f"op_{line.pk}": "", f"to_{line.pk}": "",
                              f"note_{line.pk}": ""}, follow=True)
        self.assertContains(r, "pasó a planta")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.RELEASED)

    def test_auto_release_off(self):
        PlanningRules.objects.create(site=self.site, auto_release=False)
        self._send()
        logistics.mark_picked(self.order, "Ana", "Muelle 1")
        line = self._line()
        supplies.deliver(self.order, {line.pk: "80"}, self.ins)
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.order.supplies_released_at), (WorkOrder.Status.SUPPLIES, None))
        supplies.release_to_plant(self.order, self.ins)  # a mano
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.RELEASED)

    def test_returned_order_needs_insumos_again(self):
        self._send()
        supplies.release_to_plant(self.order, self.ins)  # sin líneas
        supplies.return_to_planning(self.order, "cambia la cantidad", self.planner)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.supplies_released_at)
        self._send()
        logistics.mark_picked(self.order, "Ana", "Muelle 1")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.SUPPLIES)  # insumos debe liberar de nuevo

    def test_menu_and_queue(self):
        self._send()
        self.client.force_login(self.ins)
        page = self.client.get(reverse("mes_core:supplies_queue"))
        self.assertContains(page, self.order.number)
        self.assertContains(page, "Revisar y liberar")
        self.assertContains(page, reverse("mes_core:supply_inventory"))
        self.client.force_login(self.sup)
        self.assertNotContains(self.client.get(reverse("mes_core:order_list")), reverse("mes_core:supplies_queue"))
