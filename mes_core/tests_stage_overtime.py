"""Planeación por etapas: supervisor por etapa, rendimiento y horas extra sugeridas para cumplir."""

import json
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import stage_planning, workcal
from .models import (
    CalendarException, LineSupervisor, Operation, PlanningRules, PlanStage, Product, Shift, Site, WorkCenter, WorkOrder,
)


class StageOvertimeTests(TestCase):
    def setUp(self):
        workcal.invalidate()
        self.tz = timezone.get_current_timezone()
        today = timezone.localdate()
        self.mon = today + timedelta(days=(7 - today.weekday()) % 7 + 7)  # un lunes futuro
        self.site = Site.objects.create(code="CEDI", name="Centro")
        self.shift = Shift.objects.create(site=self.site, name="Mañana", start_time=time(6), end_time=time(14),
                                          weekdays="01234")
        self.line = WorkCenter.objects.create(site=self.site, code="ETQ", name="Etiquetado", kind="etiquetado")
        self.product = Product.objects.create(sku="CAM", name="Camiseta")
        # Compromiso: martes 14:00 → caben 2 turnos (lunes y martes)
        self.order = WorkOrder.objects.create(site=self.site, product=self.product, process_type="x",
                                              quantity=Decimal("1008"), status=WorkOrder.Status.TO_PLAN,
                                              due_date=self.at(1, 14))
        User = get_user_model()
        self.sup1 = User.objects.create_user("ana", password="x", first_name="Ana")
        self.sup2 = User.objects.create_user("beto", password="x", first_name="Beto")
        for u in (self.sup1, self.sup2):
            u.groups.add(Group.objects.get(name="Supervisor"))
        self.planner = User.objects.create_user("plan", password="x")
        self.planner.groups.add(Group.objects.get(name="Planeación"))

    def at(self, day_offset, h, m=0):
        return datetime.combine(self.mon + timedelta(days=day_offset), time(h, m), tzinfo=self.tz)

    def stage(self, **kw):
        # 1 min/unidad × 1008 unidades con 1 operario = 16,8 h = 2,1 turnos de 8 h
        base = {"name": "Etiquetado", "line": self.line.pk, "quantity": 1008, "std_value": 1, "std_unit": "min_unidad",
                "setup_minutes": 0, "mode": "operarios", "mode_param": 1, "qty_mode": "todo"}
        base.update(kw)
        return base

    def test_efficiency_stage_line_and_plant(self):
        p = stage_planning.preview(self.order, [self.stage(efficiency=50)], self.at(0, 6))
        st = p["stages"][0]
        self.assertEqual((st["std_mpu"], st["mpu"], st["efficiency"], st["efficiency_source"]), (1.0, 2.0, 50, "etapa"))
        self.assertAlmostEqual(st["work_hours"], 33.6)
        self.line.efficiency = 80
        self.line.save()
        st = stage_planning.preview(self.order, [self.stage()], self.at(0, 6))["stages"][0]
        self.assertEqual((st["efficiency"], st["efficiency_source"]), (80, "línea"))
        self.line.efficiency = None
        self.line.save()
        PlanningRules.objects.create(site=self.site, default_efficiency=90)
        st = stage_planning.preview(self.order, [self.stage()], self.at(0, 6))["stages"][0]
        self.assertEqual((st["efficiency"], st["efficiency_source"]), (90, "planta"))

    def test_overtime_suggestions_match_example(self):
        PlanningRules.objects.create(site=self.site, overtime_efficiency=100)
        p = stage_planning.preview(self.order, [self.stage()], self.at(0, 6))
        st = p["stages"][0]
        self.assertAlmostEqual(st["shifts"], 2.1)
        self.assertAlmostEqual(st["shifts_before_due"], 2.0)
        self.assertLess(p["summary"]["slack_hours"], 0)
        res = stage_planning.overtime_options(self.order, [self.stage()], self.at(0, 6))
        self.assertTrue(res["needed"])
        stretch = next(o for o in res["options"] if o["kind"] == "alargar")
        # alargar el turno del lunes: 14:00 → 15:00 (faltaban 0,8 h; se redondea a cuartos de hora)
        self.assertEqual([(i["date"], i["start"], i["end"]) for i in stretch["items"]],
                         [(self.mon.isoformat(), "14:00", "15:00")])
        self.assertTrue(stretch["meets"])
        self.assertEqual(res["options"][res["best"]]["kind"], "alargar")
        full = next(o for o in res["options"] if o["kind"] == "turno")
        self.assertTrue(full["meets"])
        self.assertGreater(full["slack_hours"], stretch["slack_hours"])
        # nada quedó guardado por calcular
        self.assertFalse(CalendarException.objects.exists())

    def test_overtime_fatigue_margin(self):
        PlanningRules.objects.create(site=self.site, overtime_efficiency=50)
        res = stage_planning.overtime_options(self.order, [self.stage()], self.at(0, 6))
        stretch = next(o for o in res["options"] if o["kind"] == "alargar")
        self.assertEqual(stretch["items"][0]["end"], "15:45")  # 0,8 h ÷ 50 % = 1,6 h → 1,75 h
        self.assertIn("rendimiento en horas extra", stretch["detail"])

    def test_save_with_overtime_and_supervisor(self):
        LineSupervisor.objects.create(work_center=self.line, shift=self.shift, supervisor=self.sup2)
        ot = {"kind": "ot", "date": self.mon.isoformat(), "shift": self.shift.pk, "start": "14:00", "end": "15:00"}
        stage_planning.save_plan(self.order, [self.stage(supervisor=self.sup1.pk, overtime=[ot], efficiency=100)],
                                 self.at(0, 6), user=self.planner)
        exc = CalendarException.objects.get()
        self.assertEqual((exc.order_id, exc.kind, list(exc.work_centers.all())), (self.order.pk, "horas_extra", [self.line]))
        ops = list(self.order.operations.all())
        self.assertTrue(all(o.supervisor_id == self.sup1.pk and not o.supervisor_auto for o in ops))
        self.assertTrue(all(o.planned_end <= self.order.due_date for o in ops))
        ps = PlanStage.objects.get(order=self.order)
        self.assertEqual((ps.supervisor_id, ps.efficiency), (self.sup1.pk, 100))
        init = stage_planning.initial_stages(self.order)[0]
        self.assertEqual((init["supervisor"], init["efficiency"], len(init["overtime"])), (self.sup1.pk, 100, 1))
        # Re-planear en automático y sin horas extra: manda la regla y se borran las horas extra del pedido
        stage_planning.save_plan(self.order, [self.stage(supervisor="")], self.at(0, 6), user=self.planner)
        self.assertFalse(CalendarException.objects.exists())
        ops = list(self.order.operations.all())
        self.assertTrue(all(o.supervisor_id == self.sup2.pk and o.supervisor_auto for o in ops))

    def test_measured_efficiency_and_api(self):
        o = WorkOrder.objects.create(site=self.site, product=self.product, process_type="x", quantity=Decimal("240"),
                                     status=WorkOrder.Status.IN_PROGRESS)
        Operation.objects.create(order=o, sequence=1, name="Etiquetar", work_center=self.line, work_center_kind="etiquetado",
                                 minutes_per_unit=Decimal("1"), crew=1, status=Operation.Status.DONE,
                                 # a las 07:00 de hace 3 días: dentro del turno sin importar a qué hora corra la prueba
                                 actual_start=timezone.localtime().replace(hour=7, minute=0, second=0, microsecond=0)
                                 - timedelta(days=3), qty_good=Decimal("240"))
        op = o.operations.get()
        start = op.actual_start
        op.actual_end = start + timedelta(hours=8)
        op.save()
        m = stage_planning.measured_efficiency(self.line)
        self.assertIsNotNone(m)
        self.client.force_login(self.planner)
        page = self.client.get(reverse("mes_core:planning_detail", args=[self.order.pk]))
        self.assertContains(page, "supervisors")
        r = self.client.post(reverse("mes_core:api_plan_overtime", args=[self.order.pk]),
                             json.dumps({"stages": [self.stage()], "start": self.at(0, 6).strftime("%Y-%m-%dT%H:%M")}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["options"])

    def test_multistage_chain_gets_overtime_on_both_lines(self):
        PlanningRules.objects.create(site=self.site, overtime_efficiency=100)
        enc = WorkCenter.objects.create(site=self.site, code="ENC", name="Encajado", kind="encajado")
        stages = [self.stage(std_value=0.5), self.stage(name="Encajado", line=enc.pk, std_value=0.5)]
        res = stage_planning.overtime_options(self.order, stages, self.at(0, 6))
        self.assertTrue(res["needed"])
        self.assertTrue(res["options"])
        best = res["options"][res["best"]]
        self.assertTrue(best["meets"])
        # al aplicarla el plan cumple de verdad
        for it in best["items"]:
            stages[it["stage"]].setdefault("overtime", []).append({k: v for k, v in it.items() if k not in ("stage", "label")})
        p = stage_planning.preview(self.order, stages, self.at(0, 6))
        self.assertGreaterEqual(p["summary"]["slack_hours"], 0)

    def test_takt_time_vs_cycle_and_standard(self):
        p = stage_planning.preview(self.order, [self.stage(efficiency=100)], self.at(0, 6))
        t = p["summary"]["takt"]
        # 16 h hábiles (lunes y martes de 06:00 a 14:00) ÷ 1008 piezas = 0,9524 min por pieza
        self.assertAlmostEqual(t["available_min"], 960)
        self.assertAlmostEqual(t["takt"], 960 / 1008, places=4)
        self.assertEqual((t["std_piece"], t["real_piece"], t["process_ct"]), (1.0, 1.0, 1.0))
        self.assertFalse(t["meets"])            # 1 min por pieza > takt
        self.assertEqual(t["min_operators"], 2)  # ⌈1 ÷ 0,952⌉
        self.assertEqual(t["bottleneck"], 0)
        # con 2 operarios el ciclo baja a 0,5 min y sigue el ritmo; al 50 % de rendimiento vuelve a 1 min
        t2 = stage_planning.preview(self.order, [self.stage(mode_param=2, efficiency=100)], self.at(0, 6))["summary"]["takt"]
        self.assertEqual((t2["process_ct"], t2["meets"]), (0.5, True))
        t3 = stage_planning.preview(self.order, [self.stage(mode_param=2, efficiency=50)], self.at(0, 6))["summary"]["takt"]
        self.assertEqual((t3["std_piece"], t3["real_piece"], t3["process_ct"], t3["meets"]), (1.0, 2.0, 1.0, False))
        # sin compromiso no hay takt
        self.order.due_date = None
        self.order.save()
        self.assertIsNone(stage_planning.preview(self.order, [self.stage()], self.at(0, 6))["summary"]["takt"]["takt"])
