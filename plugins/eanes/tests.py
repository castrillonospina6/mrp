"""Validación EAN: una persona aparte carga las referencias (EAN) de cada pedido y cuántas unidades trae cada una."""

import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from mes_core import services
from mes_core.models import Customer, Notification, Product, Site, WorkOrder

from . import eans
from .models import CustomerEanFormat, EanLine, EanLog, EanSheet

A, B, C = "7705675995066", "7705675995073", "7705675995080"   # EAN-13 válidos
UPC = "769915195934"                                          # UPC-A (12): se guarda como 0769915195934
BAD = "7705675995067"                                         # dígito de control errado


class NormalizeTests(TestCase):
    def test_forms(self):
        self.assertEqual(eans.normalize(A), (A, "", ""))
        self.assertEqual(eans.normalize(UPC)[0], "0" + UPC)            # el 0 del escáner Zebra
        self.assertEqual(eans.normalize("0" + UPC)[0], "0" + UPC)
        self.assertEqual(eans.normalize(7705675995066.0)[0], A)         # celda numérica de Excel
        self.assertEqual(eans.normalize(" 770-5675995066 ")[0], A)
        self.assertIn("control", eans.normalize(BAD)[1])
        self.assertIn("notación científica", eans.normalize("7,70568E+12")[1])
        self.assertIn("dígitos", eans.normalize("12345")[1])
        code, err, warn = eans.normalize("1234567")                    # código interno: se acepta con aviso
        self.assertEqual((code, err), ("1234567", ""))
        self.assertIn("interno", warn)

    def test_quantities(self):
        self.assertEqual(eans.parse_qty("1.200"), (1200, ""))
        self.assertEqual(eans.parse_qty("1,200"), (1200, ""))
        self.assertEqual(eans.parse_qty(120.0), (120, ""))
        self.assertEqual(eans.parse_qty("Sin límite"), (None, ""))
        self.assertIn("entero", eans.parse_qty("12,5")[1])
        self.assertIn("0", eans.parse_qty("0")[1])
        self.assertIn("Falta", eans.parse_qty("")[1])


class ParseTests(TestCase):
    def test_paste_from_excel_with_header_and_duplicates(self):
        text = (f"EAN\tCantidad\tDescripción\n{A}\t122\tCamiseta azul T-S\n{B}\t61\n\n{A}\t10\tCamiseta azul T-S\n"
                f"{UPC}\tsin límite\n{BAD}\t5\n{C}\t\n")
        p = eans.parse_text(text)
        self.assertEqual(p.skipped, 1)
        self.assertEqual([r.ean for r in p.rows], [A, B, A, "0" + UPC])
        self.assertEqual([e[0] for e in p.errors], [7, 8])            # dígito de control y sin cantidad
        rows, merged = eans.merge(p.rows)
        self.assertEqual({r.ean: r.quantity for r in rows}, {A: 132, B: 61, "0" + UPC: None})
        self.assertEqual(merged, [(A, 2)])

    def test_free_text_any_order(self):
        p = eans.parse_text(f"122 {A} Camiseta talla 12\n{B};61\n{C},7\n{UPC} sin limite polo")
        self.assertFalse(p.errors)
        got = {r.ean: (r.quantity, r.description) for r in p.rows}
        self.assertEqual(got[A], (122, "Camiseta talla 12"))
        self.assertEqual(got[B][0], 61)
        self.assertEqual(got[C][0], 7)
        self.assertEqual(got["0" + UPC], (None, "polo"))

    def test_table_with_title_rows_and_synonyms(self):
        rows = [["PEDIDO 21951", None, None], [None, None, None], ["Código de barras", "Descripción", "Unds pedidas"],
                [int(A), "Camiseta", 122], [B, "Polo", "61"], [None, None, None], ["TOTAL", None, 183]]
        p = eans.parse_table(eans._trim(rows))
        self.assertFalse(p.errors)
        self.assertEqual([(r.ean, r.quantity, r.description) for r in p.rows], [(A, 122, "Camiseta"), (B, 61, "Polo")])

    def test_table_without_headers_detects_columns_by_content(self):
        p = eans.parse_table([["Camiseta", A, 122], ["Polo", B, 61], ["Gorra", C, 3]])
        self.assertEqual(p.mapping, {"ean": 1, "qty": 2})
        self.assertEqual(sum(r.quantity for r in p.rows), 186)

    def test_saved_customer_format_wins(self):
        cust = Customer.objects.create(name="Éxito")
        fmt = CustomerEanFormat.objects.create(customer=cust, ean_column="PLU", qty_column="Cant. final")
        rows = [["PLU", "Cantidad inicial", "Cant. final"], [A, 100, 90]]
        p = eans.parse_table(rows, saved=fmt)
        self.assertEqual([(r.ean, r.quantity) for r in p.rows], [(A, 90)])


class FlowTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="S1", name="Sitio")
        self.product = Product.objects.create(sku="CAM", name="Camiseta polo")
        self.cust = Customer.objects.create(name="Almacenes Éxito")
        self.order = WorkOrder.objects.create(site=self.site, product=self.product, customer=self.cust,
                                              quantity=Decimal("183"), status=WorkOrder.Status.TO_PLAN)
        User = get_user_model()
        self.val = User.objects.create_user("val", password="x", first_name="Ana", last_name="Ruiz")
        self.val.groups.add(Group.objects.get(name="Validación EAN"))
        self.planner = User.objects.create_user("plan", password="x")
        self.planner.groups.add(Group.objects.get(name="Planeación"))

    def rows(self, text):
        return eans.parse_text(text).rows

    def test_role_exists_with_its_permissions(self):
        g = Group.objects.get(name="Validación EAN")
        codes = set(g.permissions.values_list("codename", flat=True))
        self.assertTrue({"manage_eans", "view_workorder"} <= codes)
        self.assertNotIn("plan_workorder", codes)

    def test_apply_replace_add_and_log(self):
        s = eans.apply(self.order, self.rows(f"{A}\t122\n{B}\t61"), user=self.val, source="pegado")
        self.assertEqual((s["first"], s["refs"], s["total"]), (True, 2, 183))
        sheet = self.order.ean_sheet
        self.assertEqual((sheet.status, sheet.source, sheet.loaded_by), (EanSheet.Status.LOADED, "pegado", self.val))
        eans.apply(self.order, self.rows(f"{C}\t5\n{A}\t3"), user=self.val, mode="add", source="escaneo")
        self.assertEqual(dict(self.order.ean_lines.values_list("ean", "quantity")), {A: 125, B: 61, C: 5})
        eans.apply(self.order, self.rows(f"{A}\t100"), user=self.val, source="pegado")
        self.assertEqual(dict(self.order.ean_lines.values_list("ean", "quantity")), {A: 100})
        last = EanLog.objects.filter(order=self.order).first()
        self.assertIn("Quitados", last.detail)
        self.assertIn(f"{A} 125 → 100", last.detail)

    def test_changes_in_production_need_a_reason(self):
        eans.apply(self.order, self.rows(f"{A}\t183"), user=self.val)
        WorkOrder.objects.filter(pk=self.order.pk).update(status=WorkOrder.Status.IN_PROGRESS)
        with self.assertRaisesMessage(ValidationError, "motivo"):
            eans.apply(self.order, self.rows(f"{A}\t180"), user=self.val)
        eans.apply(self.order, self.rows(f"{A}\t180"), user=self.val, reason="El cliente bajó la cantidad")
        self.assertEqual(EanLog.objects.filter(order=self.order).first().reason, "El cliente bajó la cantidad")

    def test_not_apply_and_reopen_and_cancelled(self):
        eans.apply(self.order, self.rows(f"{A}\t183"), user=self.val)
        eans.mark_not_apply(self.order, user=self.val, note="Apoyo interno")
        self.assertEqual(self.order.ean_lines.count(), 0)
        self.assertEqual(eans.status_of(WorkOrder.objects.get(pk=self.order.pk)), EanSheet.Status.NOT_APPLY)
        eans.reopen(self.order, user=self.val)
        self.assertEqual(eans.status_of(WorkOrder.objects.get(pk=self.order.pk)), EanSheet.Status.PENDING)
        WorkOrder.objects.filter(pk=self.order.pk).update(status=WorkOrder.Status.CANCELLED)
        with self.assertRaisesMessage(ValidationError, "cancelado"):
            eans.apply(self.order, self.rows(f"{A}\t1"), user=self.val)

    # ---------------------------------------------------------------- pantallas
    def test_landing_queue_and_menu(self):
        Site.objects.create(code="A0", name="Planta sin pedidos")  # va primero por código, pero no tiene pendientes
        other = WorkOrder.objects.create(site=self.site, product=self.product, quantity=Decimal("5"),
                                         status=WorkOrder.Status.PLANNED)
        eans.apply(other, self.rows(f"{A}\t5"), user=self.val)
        self.client.force_login(self.val)
        self.assertRedirects(self.client.get("/"), reverse("eanes:queue"), fetch_redirect_response=False)
        r = self.client.get(reverse("eanes:queue"))
        self.assertContains(r, self.order.number)
        self.assertNotContains(r, other.number)
        self.assertContains(r, "Pendientes · 1")
        self.assertContains(r, "Con EAN · 1")
        self.assertContains(r, "Cargar EAN")
        r = self.client.get(reverse("eanes:queue"), {"tab": "cargados", "q": A})
        self.assertContains(r, other.number)
        self.assertContains(r, "Cuadra")

    def test_paste_preview_then_save_and_next(self):
        nxt = WorkOrder.objects.create(site=self.site, product=self.product, quantity=Decimal("5"),
                                       status=WorkOrder.Status.TO_PLAN)
        self.client.force_login(self.val)
        url = reverse("eanes:order", args=[self.order.pk])
        r = self.client.post(url, {"action": "preview_text", "source": "pegado",
                                   "text": f"EAN\tCant\n{A}\t100\n{B}\t61\n{A}\t22\n{BAD}\t4"})
        self.assertContains(r, "Revisión antes de guardar")
        self.assertContains(r, "Cuadra con el pedido")
        self.assertContains(r, "se sumaron")
        self.assertContains(r, "dígito de control")
        self.assertFalse(EanLine.objects.exists())  # nada se guarda sin confirmar
        payload = r.context["payload"]
        r = self.client.post(url, {"action": "apply", "source": "pegado", "payload": payload, "had_errors": "1"})
        self.assertFalse(EanLine.objects.exists())  # hay errores: debe aceptar guardar solo las válidas
        r = self.client.post(url, {"action": "apply", "source": "pegado", "payload": payload, "had_errors": "1",
                                   "ignore_errors": "1", "next": "1"})
        self.assertRedirects(r, reverse("eanes:order", args=[nxt.pk]), fetch_redirect_response=False)
        self.assertEqual(dict(self.order.ean_lines.values_list("ean", "quantity")), {A: 122, B: 61})

    def test_file_upload_remembers_customer_columns(self):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["Orden de compra 4500123"])
        ws.append(["Item", "Descripción", "PLU", "Unidades"])
        ws.append([1, "Camiseta", int(A), 122])
        ws.append([2, "Polo", int(B), 61])
        ws.append([None, "TOTAL", None, 183])  # fila de totales: se ignora
        buf = io.BytesIO()
        wb.save(buf)
        self.client.force_login(self.val)
        url = reverse("eanes:order", args=[self.order.pk])
        up = SimpleUploadedFile("oc.xlsx", buf.getvalue())
        r = self.client.post(url, {"action": "preview_file", "file": up})
        self.assertContains(r, "Cuadra con el pedido")
        self.assertFalse(r.context["review"].errors)
        self.assertEqual(r.context["mapping"], {"ean": 2, "qty": 3, "desc": 1})
        r = self.client.post(url, {"action": "apply", "source": "archivo", "payload": r.context["payload"],
                                   "remember": "1", "col_ean": "2", "col_qty": "3", "col_desc": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.order.ean_lines.get(ean=A).description, "Camiseta")
        fmt = CustomerEanFormat.objects.get(customer=self.cust)
        self.assertEqual((fmt.ean_column, fmt.qty_column, fmt.desc_column), ("PLU", "Unidades", "Descripción"))

    def test_copy_from_other_order(self):
        src = WorkOrder.objects.create(site=self.site, product=self.product, customer=self.cust, quantity=Decimal("9"),
                                       status=WorkOrder.Status.DONE)
        eans.apply(src, self.rows(f"{A}\t4\n{B}\t5"), user=self.val)
        self.client.force_login(self.val)
        url = reverse("eanes:order", args=[self.order.pk])
        r = self.client.get(url)
        self.assertContains(r, f"{src.number} · Almacenes Éxito")
        r = self.client.post(url, {"action": "copy", "from": src.pk})
        self.assertContains(r, f"Copiado de {src.number}")
        self.assertContains(r, "Faltan 174 frente al pedido")

    def test_others_can_look_but_not_load(self):
        self.client.force_login(self.planner)
        url = reverse("eanes:order", args=[self.order.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.post(url, {"action": "not_apply"})
        self.assertEqual(r.status_code, 403)
        self.assertContains(self.client.get(reverse("mes_core:order_detail", args=[self.order.pk])), "EAN: pendiente")

    def test_new_order_notifies_validators(self):
        with self.captureOnCommitCallbacks(execute=True):
            order = services.create_order(site=self.site, product=self.product, process_type="picking", quantity=10,
                                          customer=self.cust)
        n = Notification.objects.get(user=self.val)
        self.assertIn(f"{order.number}: cargar sus EAN", n.title)
        self.assertEqual(n.url, reverse("eanes:order", args=[order.pk]))
