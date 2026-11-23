"""Automatizaciones: lista de materiales, supervisor por línea y turno, avisos, atrasos, carga de pedidos y polivalencia."""

import io
from datetime import date, datetime, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from . import bom, notify, risk, skill_growth, staffing, supplies
from .models import (
    BomLine, CrewAssignment, Customer, Employee, EmployeeSkill, LineSupervisor, Notification, Operation, Product, Shift,
    Site, Skill, SkillReview, SupplyStock, WorkCenter, WorkOrder,
)

DAY = date(2026, 11, 17)  # martes


def at(h, m=0, day=DAY):
    return datetime.combine(day, time(h, m), tzinfo=timezone.get_current_timezone())


def xlsx(rows) -> bytes:
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class AutomationBase(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="CEDI", name="Centro")
        self.morning = Shift.objects.create(site=self.site, name="Mañana", start_time=time(6), end_time=time(14))
        self.afternoon = Shift.objects.create(site=self.site, name="Tarde", start_time=time(14), end_time=time(22))
        self.wc = WorkCenter.objects.create(site=self.site, code="ETQ-1", name="Etiquetado 1", kind="etiquetado", capacity=1)
        self.product = Product.objects.create(sku="CAM", name="Camiseta")
        User = get_user_model()

        def user(name, group, **kw):
            u = User.objects.create_user(name, password="x", **kw)
            u.groups.add(Group.objects.get(name=group))
            return u

        self.planner = user("plan", "Planeación")
        self.ins = user("ins", "Insumos")
        self.log = user("log", "Logística")
        self.boss = user("jefe", "Jefe de planta")
        self.julian = user("julian", "Supervisor", first_name="Julián")
        self.marta = user("marta", "Supervisor", first_name="Marta")
        self.seller = user("ven", "Ventas")

    def order(self, status=WorkOrder.Status.PLANNED, qty=100, **kw):
        return WorkOrder.objects.create(site=self.site, product=self.product, process_type="", quantity=Decimal(qty),
                                        status=status, **kw)

    def op(self, order, start=None, end=None, **kw):
        kw.setdefault("sequence", order.operations.count() + 1)
        kw.setdefault("name", "Etiquetado")
        return Operation.objects.create(order=order, work_center=self.wc, work_center_kind="etiquetado",
                                        planned_start=start, planned_end=end, **kw)


class BomTests(AutomationBase):
    def setUp(self):
        super().setUp()
        self.etq = Product.objects.create(sku="ETQ", name="Etiqueta", kind="insumo")
        self.cja = Product.objects.create(sku="CJA", name="Caja x24", kind="insumo")
        BomLine.objects.create(product=self.product, component=self.etq, qty_per_unit=1, scrap_pct=2, stage="Etiquetado")
        BomLine.objects.create(product=self.product, component=self.cja, qty_per_unit=Decimal("0.0417"))

    def test_send_to_supplies_computes_lines(self):
        o = self.order()
        etq_op = self.op(o, at(6), at(10))
        self.op(o, at(10), at(12), name="Empaque")
        supplies.send_to_supplies(o, self.planner)
        lines = {ln.stock.product.sku: ln for ln in o.supply_lines.select_related("stock__product")}
        self.assertEqual(lines["ETQ"].qty_required, Decimal("102"))  # 100 × 1 + 2 % desperdicio
        self.assertEqual(lines["ETQ"].operation, etq_op)
        self.assertEqual(lines["CJA"].qty_required, Decimal("5"))  # 4,17 cajas → 5
        self.assertIsNone(lines["CJA"].operation)
        self.assertTrue(SupplyStock.objects.filter(site=self.site, product=self.etq).exists())
        self.assertEqual(supplies.add_bom_lines(o), 0)  # no duplica

    def test_edit_page_and_import(self):
        self.client.force_login(self.ins)
        url = reverse("mes_core:bom_edit", args=[self.product.pk])
        self.assertContains(self.client.get(url), "Lista de materiales")
        line = self.product.bom_lines.get(component=self.cja)
        r = self.client.post(url, {"row": [str(line.pk), "n1"], f"comp_{line.pk}": "CJA", f"qty_{line.pk}": "0,05",
                                   f"del_{line.pk}": "", "comp_n1": "NOEXISTE", "qty_n1": "1"})
        self.assertContains(r, "no encontré el insumo")
        line.refresh_from_db()
        self.assertEqual(line.qty_per_unit, Decimal("0.0417"))  # todo o nada
        up = SimpleUploadedFile("bom.xlsx", xlsx([["producto", "insumo", "cantidad por unidad", "desperdicio %", "etapa"],
                                                   ["CAM", "ETQ", 2, 0, "etiquetado"], ["CAM", "CJA", "0,05", "", ""]]))
        self.client.post(reverse("mes_core:bom_import"), {"file": up})
        self.assertEqual(self.product.bom_lines.get(component=self.etq).qty_per_unit, Decimal("2"))
        self.assertEqual(self.product.bom_lines.count(), 2)
        self.assertContains(self.client.get(reverse("mes_core:bom_list")), "CAM")
        self.client.force_login(self.julian)
        self.assertEqual(self.client.get(reverse("mes_core:bom_list")).status_code, 403)

    def test_import_errors_roll_back(self):
        res = bom.import_bom(xlsx([["producto", "insumo", "cantidad"], ["CAM", "ETQ", 3], ["CAM", "XX", 1]]), "b.xlsx")
        self.assertFalse(res.ok)
        self.assertEqual(self.product.bom_lines.get(component=self.etq).qty_per_unit, Decimal("1"))


class ProductVsSupplyTests(AutomationBase):
    """Productos (lo que se procesa) e insumos (lo que se consume) nunca se mezclan."""

    def test_never_mixed(self):
        from . import services
        from django.core.exceptions import ValidationError

        etq = Product.objects.create(sku="ETQ", name="Etiqueta", kind="insumo")
        # Inventario: un insumo con el mismo nombre que un producto NO reutiliza el producto
        self.client.force_login(self.ins)
        self.client.post(reverse("mes_core:supply_inventory"), {"site": self.site.pk, "action": "new", "product": "Camiseta",
                                                                "unit": "UN", "on_hand": "5"})
        new = Product.objects.get(name="Camiseta", kind="insumo")
        self.assertNotEqual(new.pk, self.product.pk)
        self.assertEqual(self.product.kind, "producto")
        self.assertFalse(SupplyStock.objects.filter(product=self.product).exists())
        # Inventario con el código de un producto: se rechaza
        self.client.post(reverse("mes_core:supply_inventory"), {"site": self.site.pk, "action": "new", "product": "CAM"})
        self.assertFalse(SupplyStock.objects.filter(product=self.product).exists())
        # Pedido: un insumo no puede ser el producto de una orden
        with self.assertRaisesMessage(ValidationError, "es un insumo"):
            services.resolve_product("ETQ")
        self.assertEqual(services.resolve_product("Etiqueta")[0].kind, "producto")  # mismo nombre → producto aparte
        with self.assertRaisesMessage(ValidationError, "es un insumo"):
            services.create_order(site=self.site, product=etq, process_type="", quantity=1)
        with self.assertRaises(ValidationError):
            supplies.ensure_stock(self.site, self.product)
        # Lista de materiales: el componente debe ser insumo y el dueño, producto
        with self.assertRaisesMessage(ValidationError, "es un producto"):
            bom.save_lines(self.product, [bom.LineInput(None, "CAM", 1)])
        with self.assertRaisesMessage(ValidationError, "Solo los productos"):
            bom.save_lines(etq, [bom.LineInput(None, "ETQ", 1)])
        res = supplies.import_inventory(xlsx([["codigo", "nombre", "existencia"], ["CAM", "Camiseta", 3]]), "i.xlsx",
                                        self.site)
        self.assertIn("es un producto", res.errors[0])

    def test_admin_lists_are_separate(self):
        Product.objects.create(sku="ETQ", name="Etiqueta", kind="insumo")
        root = get_user_model().objects.create_superuser("root", password="x")
        self.client.force_login(root)
        products = self.client.get(reverse("admin:mes_core_product_changelist"))
        self.assertContains(products, "CAM")
        self.assertNotContains(products, "ETQ")
        supplies_page = self.client.get(reverse("admin:mes_core_supplyitem_changelist"))
        self.assertContains(supplies_page, "ETQ")
        self.assertNotContains(supplies_page, ">CAM<")
        # corregir un artículo mal clasificado (sin usos)
        loose = Product.objects.create(sku="BOLSA", name="Bolsa")
        self.client.post(reverse("admin:mes_core_product_changelist"),
                         {"action": "to_supply", "_selected_action": [str(loose.pk)]})
        loose.refresh_from_db()
        self.assertEqual(loose.kind, "insumo")
        o = self.order()  # CAM está en un pedido: no se puede pasar a insumo
        self.client.post(reverse("admin:mes_core_product_changelist"),
                         {"action": "to_supply", "_selected_action": [str(o.product_id)]})
        self.assertEqual(Product.objects.get(pk=o.product_id).kind, "producto")


class LineSupervisorTests(AutomationBase):
    def test_rule_assigns_and_respects_manual(self):
        LineSupervisor.objects.create(work_center=self.wc, shift=self.morning, supervisor=self.julian)
        LineSupervisor.objects.create(work_center=self.wc, shift=self.afternoon, supervisor=self.marta)
        o = self.order()
        op = self.op(o, at(7), at(9))
        op.refresh_from_db()
        self.assertEqual((op.supervisor, op.supervisor_auto), (self.julian, True))
        op.planned_start, op.planned_end = at(15), at(17)  # se mueve a la tarde: cambia solo
        op.save()
        op.refresh_from_db()
        self.assertEqual(op.supervisor, self.marta)
        staffing.assign_supervisor([op], self.julian, self.planner)  # elegido a mano
        op.planned_start, op.planned_end = at(16), at(18)
        op.save()
        op.refresh_from_db()
        self.assertEqual((op.supervisor, op.supervisor_auto), (self.julian, False))

    def test_rules_page_applies_to_scheduled(self):
        o = self.order()
        op = self.op(o, timezone.now() + timezone.timedelta(days=1), timezone.now() + timezone.timedelta(days=1, hours=1))
        self.client.force_login(self.planner)
        url = reverse("mes_core:line_supervisors")
        self.assertContains(self.client.get(url, {"site": self.site.pk}), "Supervisor por línea y turno")
        data = {"site": self.site.pk, f"r_{self.wc.pk}_{self.morning.pk}": self.julian.pk,
                f"r_{self.wc.pk}_{self.afternoon.pk}": self.julian.pk}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(LineSupervisor.objects.count(), 2)
        op.refresh_from_db()
        shift, _ = staffing.shift_day_of(self.site, op.planned_start)
        if shift is not None:
            self.assertEqual(op.supervisor, self.julian)
        self.client.force_login(self.julian)
        self.assertEqual(self.client.post(url, data).status_code, 403)


class NotificationTests(AutomationBase):
    def test_flow_notifications(self):
        from . import services

        with self.captureOnCommitCallbacks(execute=True):
            o = services.receive_order(site=self.site, product=self.product, process_type="", quantity=10,
                                       delivery_date=timezone.localdate() + timezone.timedelta(days=10), user=self.seller)
        self.assertTrue(Notification.objects.filter(user=self.planner, kind="pedido_nuevo").exists())
        self.assertFalse(Notification.objects.filter(user=self.seller).exists())
        o.status = WorkOrder.Status.PLANNED
        o.save()
        self.op(o, at(7), at(9), supervisor=self.julian)
        with self.captureOnCommitCallbacks(execute=True):
            supplies.send_to_supplies(o, self.planner)
        self.assertTrue(Notification.objects.filter(user=self.ins, kind="a_insumos").exists())
        self.assertTrue(Notification.objects.filter(user=self.log, kind="a_insumos").exists())
        with self.captureOnCommitCallbacks(execute=True):
            supplies.release_to_plant(o, self.ins)
            from . import logistics
            logistics.mark_picked(o, "Ana", "Muelle 1", user=self.log)
        n = Notification.objects.get(user=self.julian, kind="a_planta")
        self.assertIn("Muelle 1", n.body)
        # bandeja: campana, lista y abrir marca leído
        self.client.force_login(self.julian)
        page = self.client.get(reverse("mes_core:inbox"))
        self.assertContains(page, o.number)
        self.assertContains(page, "tb-badge")
        r = self.client.get(reverse("mes_core:inbox_open", args=[n.pk]))
        self.assertEqual(r.status_code, 302)
        n.refresh_from_db()
        self.assertIsNotNone(n.read_at)
        self.client.force_login(self.planner)
        self.assertEqual(self.client.get(reverse("mes_core:inbox_open", args=[n.pk])).status_code, 404)  # ajeno

    def test_low_stock_notice_once(self):
        stock = SupplyStock.objects.create(site=self.site, product=Product.objects.create(sku="E", name="Etiqueta",
                                                                                          kind="insumo"),
                                           safety_stock=50)
        supplies.receive(stock, 100)
        with self.captureOnCommitCallbacks(execute=True):
            supplies.adjust(stock, 40)
            supplies.adjust(stock, 30)
        self.assertEqual(Notification.objects.filter(user=self.ins, kind="stock_bajo").count(), 1)

    def test_attendance_check(self):
        emps = [Employee.objects.create(document=str(i), full_name=f"op {i}", shift=self.morning) for i in range(3)]
        o = self.order()
        self.op(o, at(7), at(12), supervisor=self.julian)
        self.op(o, at(8), at(12), supervisor=self.marta, name="Empaque")
        self.assertEqual(notify.attendance_check(self.site, at(6, 30)), 0)  # aún dentro del plazo
        staffing.save_attendance(DAY, [staffing.AttendanceEntry(emps[0], "presente")], user=self.julian)
        notify.attendance_check(self.site, at(8))
        boss = Notification.objects.get(user=self.boss, kind="asistencia")
        self.assertIn("2 de 3 operarios", boss.body)
        self.assertIn("Marta", boss.body)  # tiene operaciones y no ha marcado a nadie
        self.assertTrue(Notification.objects.filter(user=self.marta, kind="asistencia").exists())
        self.assertFalse(Notification.objects.filter(user=self.julian, kind="asistencia").exists())  # ya marcó
        self.assertEqual(notify.attendance_check(self.site, at(9)), 0)  # no repite


class RiskTests(AutomationBase):
    def test_behind_schedule_suggests_and_notifies(self):
        o = self.order(WorkOrder.Status.IN_PROGRESS, qty=800)
        op = self.op(o, at(6), at(14), crew=4, qty_good=100, status=Operation.Status.IN_PROGRESS, supervisor=self.julian)
        rows = risk.site_risks(self.site, at(10))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r.expected, r.actual, r.behind_pct, r.overdue), (Decimal("400"), Decimal("100"), 75, False))
        text = " ".join(r.suggestions)
        self.assertIn("24 persona(s) más", text)
        self.assertIn("h más", text)
        self.assertEqual(risk.check(self.site, at(10)), 3)  # planeación, jefe de planta y el supervisor
        self.assertEqual(risk.check(self.site, at(11)), 0)  # una vez al día
        op.qty_good = 380
        op.save()
        self.assertEqual(risk.site_risks(self.site, at(10)), [])  # dentro de la tolerancia (10 %)
        self.client.force_login(self.planner)
        op.qty_good = 0
        op.save()
        page = self.client.get(reverse("mes_core:at_risk"), {"site": self.site.pk, "mios": "0"})
        self.assertEqual(page.status_code, 200)


class OrderImportTests(AutomationBase):
    def test_import_recognizes_and_rolls_back(self):
        Customer.objects.create(name="Almacenes Éxito")
        self.client.force_login(self.seller)
        url = reverse("mes_core:order_import")
        bad = SimpleUploadedFile("p.xlsx", xlsx([
            ["cliente", "producto", "tipo de servicio", "cantidad", "fecha de entrega", "op"],
            ["ALMACENES EXITO", "CAM", "", 100, "30/12/2026", "OP-1"],
            ["Otro", "", "", "abc", "", "OP-1"]]))
        r = self.client.post(url, {"site": self.site.pk, "file": bad})
        self.assertContains(r, "falta el producto")
        self.assertContains(r, "repetida")
        self.assertFalse(WorkOrder.objects.filter(external_op="OP-1").exists())
        good = SimpleUploadedFile("p.xlsx", xlsx([
            ["Cliente", "Producto", "Tipo de servicio", "Cantidad", "Fecha de entrega", "OP", "Prioridad"],
            ["  almacenes   exito ", "cam", "Maquila Especial", "1.200", "30/12/2026", "OP-1", 2],
            ["Falabella", "Jean nuevo", "", 50, datetime(2026, 12, 31), "OP-2", ""]]))
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(url, {"site": self.site.pk, "file": good})
        self.assertEqual(r.status_code, 302)
        a = WorkOrder.objects.get(external_op="OP-1")
        self.assertEqual((a.customer.name, a.product, a.quantity, a.priority, a.status),
                         ("Almacenes Éxito", self.product, Decimal("1200"), 2, WorkOrder.Status.TO_PLAN))
        self.assertTrue(a.process_type)
        self.assertEqual(Customer.objects.filter(name__icontains="xito").count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.planner, kind="pedido_nuevo").count(), 1)  # uno solo
        again = SimpleUploadedFile("p.xlsx", xlsx([["producto", "cantidad", "fecha de entrega", "op"],
                                                    ["CAM", 1, "30/12/2026", "OP-2"]]))
        self.assertContains(self.client.post(url, {"site": self.site.pk, "file": again}), "ya existe un pedido con la OP")
        self.assertEqual(self.client.get(reverse("mes_core:order_import_template")).status_code, 200)
        self.client.force_login(self.log)
        self.assertEqual(self.client.get(url).status_code, 403)


@override_settings(MES_SKILL_HOURS={1: 1, 2: 5, 3: 100})
class SkillGrowthTests(AutomationBase):
    def test_suggest_approve_and_dismiss(self):
        skill = Skill.objects.create(name="Etiquetado")
        ana = Employee.objects.create(site=self.site, document="1", full_name="ana", supervisor=self.julian)
        beto = Employee.objects.create(site=self.site, document="2", full_name="beto", supervisor=self.julian)
        o = self.order(WorkOrder.Status.DONE)
        op = self.op(o, at(6), at(14), status=Operation.Status.DONE, qty_good=100)
        for e in (ana, beto):
            CrewAssignment.objects.create(operation=op, employee=e)
        EmployeeSkill.objects.create(employee=beto, skill=skill, level=2)
        now = at(8, day=date(2026, 11, 20))
        rows = skill_growth.suggestions(self.site, now=now)
        self.assertEqual([(s.employee, s.level, s.hours) for s in rows], [(ana, 2, 8.0)])  # beto ya es intermedio
        self.assertEqual(skill_growth.check(self.site, now), 1)
        self.client.force_login(self.julian)
        url = reverse("mes_core:skill_suggestions")
        self.assertContains(self.client.get(url, {"site": self.site.pk}), "Subir a intermedio")
        r = self.client.post(url, {"site": self.site.pk, "employee": ana.pk, "skill": skill.pk, "level": 2,
                                   "hours": "8", "decision": "aprobar"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(EmployeeSkill.objects.get(employee=ana, skill=skill).level, 2)
        self.assertTrue(SkillReview.objects.filter(employee=ana, decision="aprobada").exists())
        self.assertEqual(skill_growth.suggestions(self.site, now=now), [])
        # otro supervisor no decide por el equipo de Julián
        self.client.force_login(self.marta)
        r = self.client.post(url, {"site": self.site.pk, "employee": beto.pk, "skill": skill.pk, "level": 3,
                                   "hours": "8", "decision": "aprobar"})
        self.assertEqual(r.status_code, 403)
