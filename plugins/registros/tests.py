"""Lo que hacía la app anterior, ahora en el MES: reportar por referencia (EAN) con tope por referencia y códigos
sobrantes, estado por referencia, cerrar con faltante justificado (Cumple / No cumple), editar con motivo, anular,
reactivar y eliminar solo lo anulado, buscar registros, seguimiento, «Mis registros» y exportar a Excel."""

import io
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from mes_core import corrections, services
from mes_core.models import Operation, OrderClosure, PlanStage, ProductionReport, VoidedReport, WorkOrder
from plugins.eanes import eans
from plugins.piso import floor, reporting
from plugins.piso.models import IntervalReport, LineCode, Mark
from plugins.piso.tests_reporting import ReportingBase

from . import export, records
from .records import Filters

A, B = "7705675995066", "7705675995073"


class Base(ReportingBase):
    def setUp(self):
        super().setUp()
        self.planner = get_user_model().objects.create_user("plan", password="x", first_name="Paula")
        self.planner.groups.add(Group.objects.get(name="Planeación"))

    def load_eans(self, text=f"{A}\t60\n{B}\t40"):
        eans.apply(self.order, eans.parse_text(text).rows, user=self.planner)

    def rep(self, op=None, **kw):
        return services.report_production(op or self.op1, **kw)


class EanAtReportTests(Base):
    def test_reference_is_required_must_belong_and_cannot_pass_what_is_missing(self):
        self.load_eans()
        with self.assertRaisesMessage(ValidationError, "Elige o escanea la referencia"):
            self.rep(qty_good=5)
        with self.assertRaisesMessage(ValidationError, "no es del pedido"):
            self.rep(qty_good=5, ean="7705675995080")
        r = self.rep(qty_good=50, ean="7705675995066")
        self.assertEqual(r.ean, A)
        with self.assertRaisesMessage(ValidationError, "faltan 10 de 60"):
            self.rep(qty_good=11, ean=A)
        self.rep(qty_good=10, ean=A)
        self.rep(qty_scrap=2)  # solo merma: no hace falta la referencia
        prog = eans.stage_progress(self.op1)
        self.assertEqual((prog[A]["status"], prog[B]["status"]), ("Completado", "Pendiente"))

    def test_upc_from_the_scanner_matches_and_leftovers_complete_the_reference(self):
        self.load_eans(f"769915195934\t30\n{A}\t70")
        self.rep(qty_good=25, ean="0769915195934", qty_leftover=5)  # el 0 del escáner Zebra
        self.op1.refresh_from_db()
        self.assertEqual((self.op1.qty_good, self.op1.qty_leftover, self.op1.qty_pending), (25, 5, 70))
        p = eans.stage_progress(self.op1)["0769915195934"]
        self.assertEqual((p["good"], p["left"], p["status"]), (25, 5, "Completado"))
        self.rep(qty_good=65, ean=A, qty_leftover=5)
        self.op1.refresh_from_db()
        self.assertEqual(self.op1.status, Operation.Status.DONE)  # los sobrantes también completan la operación
        self.assertFalse(Mark.objects.exists())  # los sobrantes no son producción de nadie

    def test_cap_is_per_stage_across_its_lots(self):
        self.load_eans()
        stage = PlanStage.objects.create(order=self.order, sequence=1, name="Armar", work_center=self.wc, quantity=100,
                                         std_value=1)
        lot2 = Operation.objects.create(order=self.order, sequence=5, name="Armar · lote 2", work_center=self.wc,
                                        quantity=50, stage=stage)
        Operation.objects.filter(pk=self.op1.pk).update(stage=stage, quantity=50)
        self.op1.refresh_from_db()
        self.rep(qty_good=40, ean=A)
        with self.assertRaisesMessage(ValidationError, "faltan 20 de 60"):
            self.rep(lot2, qty_good=25, ean=A)

    def test_not_applicable_ignores_the_reference_and_pending_can_be_required(self):
        self.rep(qty_good=1, ean=A)  # sin EAN cargados: se deja reportar (por defecto)
        with override_settings(MES_EAN_EXIGIR=True), self.assertRaisesMessage(ValidationError, "aún no tiene sus EAN"):
            self.rep(qty_good=1)
        eans.mark_not_apply(self.order, user=self.planner)
        self.assertEqual(self.rep(qty_good=1, ean=A).ean, "")

    def test_tablet_counter_and_period_reports_carry_the_reference(self):
        self.load_eans()
        self.login()
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual(s["task"]["ref"]["mode"], "on")
        self.assertEqual([(i["ean"], i["left"]) for i in s["task"]["ref"]["items"]], [(A, 60), (B, 40)])
        r = self.post("piso:api_mark", {"k": "p", "q": 2, "op": self.op1.pk})
        self.assertEqual(r.status_code, 409)
        self.assertIn("referencia", r.json()["e"])
        self.assertEqual(self.post("piso:api_mark", {"k": "p", "q": 2, "op": self.op1.pk, "e": A}).status_code, 200)
        self.assertEqual(ProductionReport.objects.get().ean, A)
        self.line_mode("intervalo")
        r = self.post("piso:api_report", {"op": self.op1.pk, "g": "10", "l": "3", "e": B})
        self.assertEqual(r.status_code, 200, r.content)
        rep = ProductionReport.objects.get(ean=B)
        self.assertEqual((rep.qty_good, rep.qty_leftover), (10, 3))
        r = self.post("piso:api_report", {"op": self.op1.pk, "g": "30", "e": B})
        self.assertEqual(r.status_code, 409)
        self.assertIn("faltan 27 de 40", r.json()["e"])

    def test_order_page_form_reports_with_reference(self):
        self.load_eans()
        self.client.force_login(self.planner)
        url = reverse("mes_core:order_detail", args=[self.order.pk])
        page = self.client.get(url)
        self.assertEqual(page.context["refs"][str(self.op1.pk)]["mode"], "on")
        r = self.client.post(url, {"operation": self.op1.pk, "qty_good": "5", "qty_scrap": "0", "ean": B,
                                   "qty_leftover": "1", "comment": ""})
        self.assertEqual(r.status_code, 302)
        rep = ProductionReport.objects.get()
        self.assertEqual((rep.ean, rep.qty_good, rep.qty_leftover), (B, 5, 1))


class CorrectionTests(Base):
    def test_void_takes_it_out_of_every_sum_and_reactivate_puts_it_back_as_it_was(self):
        self.load_eans()
        self.login()
        self.post("piso:api_mark", {"k": "p", "q": 3, "op": self.op1.pk, "e": A, "t": []})
        rep = ProductionReport.objects.get()
        self.assertEqual(Mark.objects.filter(report=rep).count(), 1)
        with self.assertRaisesMessage(ValidationError, "motivo"):
            corrections.void_report(rep, "")
        v = corrections.void_report(rep, "Se marcó en el pedido equivocado", user=self.planner)
        self.op1.refresh_from_db()
        self.assertEqual((self.op1.qty_good, Mark.objects.count(), ProductionReport.objects.count()), (0, 0, 0))
        self.assertEqual((v.ean, v.qty_good, v.who), (A, 3, self.ana.display_name))
        self.assertEqual(eans.stage_progress(self.op1)[A]["done"], 0)
        new = corrections.reactivate_report(v, "Sí era de este pedido", user=self.planner)
        self.op1.refresh_from_db()
        self.assertEqual((self.op1.qty_good, new.ean, new.employee, new.reported_at), (3, A, self.ana, rep.reported_at))
        self.assertEqual(Mark.objects.get().report, new)  # la eficiencia vuelve a contar esa marcación
        self.assertFalse(VoidedReport.objects.exists())

    def test_reactivate_only_if_it_still_fits(self):
        r = self.rep(qty_good=60)
        v = corrections.void_report(r, "error")
        self.rep(qty_good=50)
        with self.assertRaisesMessage(ValidationError, "No se puede reactivar"):
            corrections.reactivate_report(v, "volver")

    def test_only_voided_can_be_deleted_and_order_page_voids(self):
        r = self.rep(qty_good=4)
        self.client.force_login(self.planner)
        self.client.post(reverse("mes_core:report_delete", args=[r.pk]), {"reason": "duplicado"})
        self.assertFalse(ProductionReport.objects.exists())
        v = VoidedReport.objects.get()
        self.client.post(reverse("registros:purge", args=[v.pk]))
        self.assertFalse(VoidedReport.objects.exists())

    def test_edit_with_reason_moves_rescales_and_shifts_the_marks(self):
        self.load_eans()
        self.line_mode("supernumerario")
        ir, _ = reporting.report_interval(operation=self.op1, kind=IntervalReport.Kind.LINE, good=10, reporter=self.carla,
                                          ean=A)
        rep = ir.report
        self.assertEqual(sorted(Mark.objects.filter(report=rep).values_list("qty", flat=True)), [5, 5])
        with self.assertRaisesMessage(ValidationError, "motivo"):
            corrections.edit_report(rep, reason="", qty_good=8)
        with self.assertRaisesMessage(ValidationError, "faltan 40 de 40"):
            corrections.edit_report(rep, reason="x", ean=B, qty_good=41)
        when = rep.reported_at - timedelta(hours=2)
        corrections.edit_report(rep, reason="Se digitó mal", user=self.planner, qty_good=8, ean=B, reported_at=when)
        rep.refresh_from_db()
        self.assertEqual((rep.qty_good, rep.ean, rep.edit_reason, rep.edited_by), (8, B, "Se digitó mal", self.planner))
        marks = list(Mark.objects.filter(report=rep))
        self.assertEqual(sum(m.qty for m in marks), 8)
        self.assertTrue(all(abs((m.at - when).total_seconds()) < 2 for m in marks))
        corrections.edit_report(rep, reason="Era de Sellar", operation=self.op2)
        self.op1.refresh_from_db()
        self.op2.refresh_from_db()
        self.assertEqual((self.op1.qty_good, self.op2.qty_good), (0, 8))
        self.assertTrue(all(m.operation_id == self.op2.pk for m in Mark.objects.filter(report=rep)))

    def test_close_with_justified_shortfall_by_reference(self):
        self.load_eans()
        self.rep(qty_good=60, ean=A)
        self.rep(self.op2, qty_good=60, ean=A)
        self.rep(self.op2, qty_good=30, ean=B)
        self.assertEqual(corrections.compliance(self.order), "")
        with self.assertRaisesMessage(ValidationError, "faltante cambió"):
            corrections.close_with_shortfall(self.order, reason="x", justified_by="Ana", expected_total=1)
        c = corrections.close_with_shortfall(self.order, reason="El cliente envió menos", justified_by="Carla",
                                             user=self.planner, expected_total=50)
        self.assertEqual(c.total, 50)
        self.assertEqual(sorted((s.operation.name, s.ean, s.qty) for s in c.lines.select_related("operation")),
                         [("Armar", B, 40), ("Sellar", B, 10)])
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, corrections.compliance(self.order)), (WorkOrder.Status.DONE, "No cumple"))
        self.assertEqual(eans.stage_progress(self.op2)[B]["status"], "Completado")  # justificado cuenta
        corrections.undo_closure(self.order, "Llegó la mercancía", user=self.planner)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.IN_PROGRESS)
        self.assertFalse(OrderClosure.objects.exists())

    def test_complete_order_is_cumple(self):
        self.rep(qty_good=100, finished=True)
        self.rep(self.op2, qty_good=100, finished=True)
        self.order.refresh_from_db()
        self.assertEqual(corrections.compliance(self.order), "Cumple")


class RecordsTests(Base):
    def test_rows_per_person_with_chained_time_and_filters(self):
        self.load_eans()
        t0 = timezone.now() - timedelta(minutes=8)
        floor.add_mark(operation=self.op1, employee=self.ana, qty=1, ean=A, times=[t0.timestamp() * 1000])
        floor.add_mark(operation=self.op1, employee=self.ana, qty=1, ean=A)  # una más, 8 min después
        self.line_mode("supernumerario")
        reporting.report_interval(operation=self.op1, kind=IntervalReport.Kind.LINE, good=10, reporter=self.carla, ean=B)
        rows, cut = records.search(Filters(d_from=timezone.localdate(), d_to=timezone.localdate()))
        self.assertFalse(cut)
        ana = [r for r in rows if r.employee_id == self.ana.pk]
        self.assertEqual(sum(r.unidades for r in ana), 7)  # 2 marcadas + 5 de su parte del tramo
        line = [r for r in rows if r.ean == B]
        self.assertEqual(sorted(r.employee_id for r in line), sorted([self.ana.pk, self.beto.pk]))
        self.assertTrue(all(r.supernumerario == self.carla.display_name for r in line))
        counter = [r for r in ana if r.ean == A]
        # encadenado: la segunda unidad cuenta desde la primera
        self.assertTrue(any(r.minutos and 7 < r.minutos < 9 for r in counter), [r.minutos for r in counter])
        f = Filters(d_from=timezone.localdate(), d_to=timezone.localdate(), operario="beto")
        self.assertEqual({r.employee_id for r in records.search(f)[0]}, {self.beto.pk})
        f = Filters(d_from=timezone.localdate(), d_to=timezone.localdate(), ean=A)
        self.assertEqual({r.ean for r in records.search(f)[0]}, {A})
        f = Filters(d_from=timezone.localdate(), d_to=timezone.localdate(), supernumerario=str(self.carla.pk))
        self.assertEqual({r.ean for r in records.search(f)[0]}, {B})

    def test_pages_render_and_actions_need_reason(self):
        self.load_eans()
        r = self.rep(qty_good=5, ean=A)
        self.client.force_login(self.planner)
        page = self.client.get(reverse("registros:records"))
        self.assertContains(page, self.order.number)
        self.assertContains(page, A)
        self.client.post(reverse("registros:void", args=[r.pk]), {"reason": ""})
        self.assertTrue(ProductionReport.objects.filter(pk=r.pk).exists())
        edit = reverse("registros:edit", args=[r.pk])
        self.assertContains(self.client.get(edit), "Motivo de la edición")
        self.client.post(edit, {"qty_good": "4", "qty_scrap": "0", "qty_leftover": "0", "ean": A,
                                "operation": self.op1.pk, "reason": "Conteo", "employee": ""})
        self.assertEqual(ProductionReport.objects.get(pk=r.pk).qty_good, 4)
        self.client.post(reverse("registros:void", args=[r.pk]), {"reason": "duplicado"})
        page = self.client.get(reverse("registros:records"), {"estado": "anulados"})
        self.assertContains(page, "Reactivar")
        self.assertContains(page, "duplicado")
        det = self.client.get(reverse("registros:tracking_detail", args=[self.order.pk]))
        self.assertContains(det, "Avance por referencia")
        self.assertContains(det, "Cerrar con faltante")
        trk = self.client.get(reverse("registros:tracking"))
        self.assertContains(trk, self.order.number)

    def test_my_records_on_the_tablet(self):
        self.load_eans()
        self.login()
        self.post("piso:api_mark", {"k": "p", "q": 2, "op": self.op1.pk, "e": A})
        j = self.client.get(reverse("piso:api_my_records")).json()
        self.assertEqual([(r["pedido"], r["ean"], r["u"]) for r in j["rows"]], [(self.order.number, A, 2)])
        old = (timezone.localdate() - timedelta(days=3)).isoformat()
        self.assertEqual(self.client.get(reverse("piso:api_my_records"), {"fecha": old}).json()["rows"], [])
        self.assertEqual(len(self.client.get(reverse("piso:api_my_records"), {"h": "1"}).json()["rows"]), 1)

    def test_export_has_the_same_sheets_and_columns(self):
        from openpyxl import load_workbook

        self.load_eans()
        self.rep(qty_good=5, ean=A)
        today = timezone.localdate()
        wb = load_workbook(io.BytesIO(export.workbook(today, today)))
        self.assertEqual(wb.sheetnames, ["Registros", "Seguimiento paquetes", "Validacion eanes", "Control_Horas"])
        head = [c.value for c in wb["Registros"][1]]
        self.assertEqual(head[:26], export.REG_COLS[:26])
        self.assertEqual(head[11:14], ["Numero pedido", "EAN", "Unidades totales procesadas"])
        reg = [c.value for c in wb["Registros"][2]]
        self.assertEqual((reg[11], reg[12], reg[13]), (self.order.number, A, 5))
        ve = [[c.value for c in row] for row in wb["Validacion eanes"].iter_rows(min_row=2)]
        self.assertEqual([(r[2], r[3], r[6]) for r in ve], [(A, 60, "Pendiente"), (B, 40, "Pendiente")])  # última etapa
        self.client.force_login(self.planner)
        r = self.client.get(reverse("registros:export"), {"descargar": "1", "desde": today.isoformat(),
                                                          "hasta": today.isoformat(), "hoja": ["registros"]})
        self.assertEqual(r["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertEqual(load_workbook(io.BytesIO(r.content)).sheetnames, ["Registros"])
        out = load_workbook(io.BytesIO(export.workbook(today - timedelta(days=10), today - timedelta(days=5))))
        self.assertEqual(out["Registros"].max_row, 1)  # fuera del rango: solo encabezados
