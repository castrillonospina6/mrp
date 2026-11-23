"""Correcciones desde la aplicación: editar el pedido, editar / eliminar reportes y «Editar todo» en planeación."""

import json
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from . import corrections, services, stage_planning
from .models import Operation, Product, ProductionReport, ScheduleChange, Site, WorkCenter, WorkOrder
from .tests_planning import TZ, day_shift


class CorrectionsTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="P", name="Planta")
        day_shift(self.site, operators=10)
        self.line = WorkCenter.objects.create(site=self.site, code="ACO", name="Acondicionado", kind="acondicionado")
        self.product = Product.objects.create(sku="CAM", name="Camiseta")
        self.start = datetime(2026, 11, 16, 6, 0, tzinfo=TZ)
        self.order = services.receive_order(site=self.site, product=self.product, process_type="acondicionado",
                                            quantity=100, received_on=date(2026, 11, 14),
                                            delivery_date=date(2026, 11, 25), transit_days=2)
        self.stages = [{"name": "Etiquetado", "line": self.line.pk, "quantity": 100, "std_value": 0.5,
                        "std_unit": "min_unidad", "setup_minutes": 0, "mode": "turno", "qty_mode": "todo"}]
        stage_planning.save_plan(self.order, self.stages, self.start)
        WorkOrder.objects.filter(pk=self.order.pk).update(status=WorkOrder.Status.RELEASED)
        self.order.refresh_from_db()
        self.op = self.order.operations.get()
        self.planner = get_user_model().objects.create_user("plan", password="x")
        self.planner.groups.add(Group.objects.get(name="Planeación"))

    def test_delete_report_reverts_operation_and_order(self):
        r1 = services.report_production(self.op, qty_good=40)
        r2 = services.report_production(self.op, qty_good=60, finished=True)
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.order.quantity_done), (WorkOrder.Status.DONE, 100))
        corrections.delete_report(r2, user=self.planner)
        self.op.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual((self.op.status, self.op.qty_good), (Operation.Status.IN_PROGRESS, 40))
        self.assertEqual((self.order.status, self.order.quantity_done), (WorkOrder.Status.IN_PROGRESS, 40))
        corrections.delete_report(r1, user=self.planner)
        self.op.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual((self.op.status, self.op.actual_start), (Operation.Status.PENDING, None))
        self.assertEqual((self.order.status, self.order.quantity_done), (WorkOrder.Status.RELEASED, 0))
        self.assertTrue(ScheduleChange.objects.filter(description__contains="eliminó un reporte").exists())

    def test_update_report_recomputes_and_keeps_extra_apart(self):
        rep = services.report_production(self.op, qty_good=30, qty_scrap=2)
        corrections.update_report(rep, qty_good="25", qty_scrap="0", finished=False, comment="conteo", user=self.planner)
        self.op.refresh_from_db()
        self.assertEqual((self.op.qty_good, self.op.qty_scrap), (25, 0))
        corrections.update_report(rep, qty_good="110", qty_scrap="0", finished=True, user=self.planner)
        rep.refresh_from_db()
        self.op.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual((rep.qty_good, rep.qty_extra), (100, 10))
        self.assertEqual((self.op.status, self.order.status), (Operation.Status.DONE, WorkOrder.Status.DONE))
        with self.assertRaises(ValidationError):
            corrections.update_report(rep, qty_good="-1", qty_scrap="0", finished=True)

    def test_edit_everything_in_planning_even_when_started_or_done(self):
        services.report_production(self.op, qty_good=100, finished=True)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.DONE)
        self.assertTrue(stage_planning.is_locked(self.order))
        with self.assertRaises(ValidationError):
            stage_planning.save_plan(self.order, self.stages, self.start)
        two = [*self.stages, {**self.stages[0], "name": "Empaque"}]
        stage_planning.save_plan(self.order, two, self.start, user=self.planner, reopen=True)
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.order.quantity_done), (WorkOrder.Status.RELEASED, 0))
        self.assertEqual(list(self.order.plan_stages.values_list("name", flat=True)), ["Etiquetado", "Empaque"])
        self.assertFalse(ProductionReport.objects.filter(operation__order=self.order).exists())
        self.assertTrue(ScheduleChange.objects.filter(description__contains="reabrió y re-planeó todo").exists())

    def test_cancelled_order_can_be_replanned_and_comes_back_as_draft(self):
        services.cancel_order(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.CANCELLED)
        stage_planning.save_plan(self.order, self.stages, self.start, user=self.planner, reopen=True)
        self.order.refresh_from_db()
        self.assertIn(self.order.status, (WorkOrder.Status.DRAFT, WorkOrder.Status.PLANNED))  # reactivada

    def test_planning_api_and_page_offer_edit_everything(self):
        services.report_production(self.op, qty_good=10)
        self.client.force_login(self.planner)
        page = self.client.get(reverse("mes_core:planning_detail", args=[self.order.pk]))
        self.assertContains(page, "Editar todo")
        cfg = page.context["config"]
        self.assertTrue(cfg["can_reopen"])
        self.assertEqual((cfg["progress"]["reports"], cfg["progress"]["good"]), (1, 10.0))
        url = reverse("mes_core:api_plan_save", args=[self.order.pk])
        body = {"start": "2026-11-17T06:00", "stages": self.stages, "respect_operators": True}
        self.assertEqual(self.client.post(url, json.dumps(body), content_type="application/json").status_code, 400)
        r = self.client.post(url, json.dumps({**body, "reopen": True}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.order.operations.get().qty_good, 0)

    def test_order_edit_and_report_buttons_without_admin(self):
        rep = services.report_production(self.op, qty_good=10)
        self.client.force_login(self.planner)
        detail = self.client.get(reverse("mes_core:order_detail", args=[self.order.pk]))
        self.assertContains(detail, reverse("mes_core:order_edit", args=[self.order.pk]))
        self.assertNotContains(detail, "/admin/mes_core/workorder/")
        self.assertContains(detail, reverse("mes_core:report_delete", args=[rep.pk]))
        form = self.client.get(reverse("mes_core:order_edit", args=[self.order.pk])).context["form"]
        data = {k: v for k, v in form.initial.items() if v is not None and k in form.fields}
        data.update({"quantity": "120", "priority": "1", "reference": "OLA 7", "site": self.site.pk,
                     "product": self.product.pk, "due_date": "2026-11-23T06:00", "status": self.order.status,
                     "received_on": "2026-11-14", "delivery_date": "2026-11-25"})
        r = self.client.post(reverse("mes_core:order_edit", args=[self.order.pk]), data)
        self.assertEqual(r.status_code, 302, r.context["form"].errors if r.context else "")
        self.order.refresh_from_db()
        self.assertEqual((self.order.quantity, self.order.priority, self.order.reference), (Decimal("120"), 1, "OLA 7"))
        # Corregir y eliminar desde el pedido
        self.client.post(reverse("mes_core:report_edit", args=[rep.pk]), {"qty_good": "8", "qty_scrap": "1"})
        self.op.refresh_from_db()
        self.assertEqual((self.op.qty_good, self.op.qty_scrap), (10, 0))  # sin motivo no se corrige
        self.client.post(reverse("mes_core:report_edit", args=[rep.pk]),
                         {"qty_good": "8", "qty_scrap": "1", "reason": "Conteo físico"})
        self.op.refresh_from_db()
        self.assertEqual((self.op.qty_good, self.op.qty_scrap), (8, 1))
        rep.refresh_from_db()
        self.assertEqual((rep.edit_reason, rep.edited_by), ("Conteo físico", self.planner))
        self.client.post(reverse("mes_core:report_delete_all", args=[self.order.pk]))
        self.assertTrue(ProductionReport.objects.filter(operation__order=self.order).exists())  # sin motivo, nada
        self.client.post(reverse("mes_core:report_delete_all", args=[self.order.pk]), {"reason": "Pedido mal digitado"})
        self.assertFalse(ProductionReport.objects.filter(operation__order=self.order).exists())
        self.assertEqual(self.order.voided_reports.count(), 1)  # anulado, no borrado: se puede reactivar

    def test_stage_summary_adds_up_reports_per_stage(self):
        # Dos etapas; la primera en 2 lotes, con varios reportes que hay que sumar
        lots = [{**self.stages[0], "qty_mode": "lotes", "qty_param": 60},
                {**self.stages[0], "name": "Empaque", "std_value": 0.2}]
        stage_planning.save_plan(self.order, lots, self.start, reopen=True)
        WorkOrder.objects.filter(pk=self.order.pk).update(status=WorkOrder.Status.RELEASED)
        ops = list(self.order.operations.order_by("sequence"))
        self.assertEqual([o.name for o in ops], ["Etiquetado · lote 1/2", "Etiquetado · lote 2/2", "Empaque"])
        services.report_production(ops[0], qty_good=30, qty_scrap=1)
        services.report_production(ops[0], qty_good=20)
        services.report_production(ops[1], qty_good=15)
        services.report_production(ops[2], qty_good=10)
        self.client.force_login(self.planner)
        page = self.client.get(reverse("mes_core:order_detail", args=[self.order.pk]))
        self.assertContains(page, "Resumen por etapa")
        rows = page.context["stage_summary"]
        self.assertEqual([(r["name"], r["planned"], r["good"], r["pending"], r["scrap"], r["reports"], r["parts"])
                          for r in rows],
                         [("Etiquetado", 100, 65, 35, 1, 3, 2), ("Empaque", 100, 10, 90, 0, 1, 1)])

    def test_route_without_matching_line_kind(self):
        # La ruta estándar busca líneas por tipo; si no hay de ese tipo y la planta tiene una sola línea, usa esa
        order = services.receive_order(site=self.site, product=self.product, process_type="picking", quantity=10,
                                       received_on=date(2026, 11, 14), delivery_date=date(2026, 11, 25))
        self.assertTrue(all(s["line"] == self.line.pk for s in stage_planning.initial_stages(order)))
        WorkCenter.objects.create(site=self.site, code="L2", name="Otra", kind="proceso")
        self.assertTrue(all(s["line"] is None for s in stage_planning.initial_stages(order)))  # dos líneas: elige
        with self.assertRaisesMessage(ValidationError, "no tiene línea"):
            stage_planning.save_plan(order, [{**self.stages[0], "line": None}], self.start)

    def test_viewer_cannot_fix(self):
        rep = services.report_production(self.op, qty_good=10)
        viewer = get_user_model().objects.create_user("v", password="x")
        self.client.force_login(viewer)
        self.client.post(reverse("mes_core:report_delete", args=[rep.pk]))
        self.assertTrue(ProductionReport.objects.filter(pk=rep.pk).exists())
        r = self.client.get(reverse("mes_core:order_edit", args=[self.order.pk]))
        self.assertEqual(r.status_code, 302)
