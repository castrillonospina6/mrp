"""Etapas en paralelo (en cola / a la par / al mismo tiempo) y re-planear lo pendiente de órdenes que ya empezaron."""

import json
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import planning, scheduling, stage_planning, workcal
from .models import Operation, PlanStage, Product, Shift, Site, WorkCenter, WorkOrder


class StageLinkTests(TestCase):
    def setUp(self):
        workcal.invalidate()
        self.tz = timezone.get_current_timezone()
        today = timezone.localdate()
        self.mon = today + timedelta(days=(7 - today.weekday()) % 7 + 7)  # un lunes futuro
        self.site = Site.objects.create(code="CEDI", name="Centro")
        Shift.objects.create(site=self.site, name="Mañana", start_time=time(6), end_time=time(14), weekdays="01234")
        self.etq = WorkCenter.objects.create(site=self.site, code="ETQ", name="Etiquetado", kind="etiquetado")
        self.pre = WorkCenter.objects.create(site=self.site, code="PRE", name="Presintos", kind="presinto")
        self.enc = WorkCenter.objects.create(site=self.site, code="ENC", name="Encajado", kind="encajado")
        self.product = Product.objects.create(sku="CAM", name="Camisa")
        self.order = WorkOrder.objects.create(site=self.site, product=self.product, process_type="x",
                                              quantity=Decimal("480"), status=WorkOrder.Status.TO_PLAN,
                                              due_date=self.at(4, 14))
        self.planner = get_user_model().objects.create_user("plan", password="x")
        self.planner.groups.add(Group.objects.get(name="Planeación"))

    def at(self, day_offset, h, m=0):
        return datetime.combine(self.mon + timedelta(days=day_offset), time(h, m), tzinfo=self.tz)

    def stage(self, name, line, std, **kw):
        base = {"name": name, "line": line.pk, "quantity": 480, "std_value": std, "std_unit": "min_unidad",
                "setup_minutes": 0, "mode": "operarios", "mode_param": 1, "qty_mode": "todo", "efficiency": 100}
        base.update(kw)
        return base

    def windows(self, p):
        return [(datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])) for s in p["stages"]]

    # ------------------------------------------------------------ motor de planeación por etapas
    def test_queue_parallel_and_flow(self):
        # Presintos: 1 min/unidad × 480 = 8 h (lunes 06–14). Etiquetas: 0,5 min/unidad = 4 h.
        cola = stage_planning.preview(self.order, [self.stage("Presintos", self.pre, 1),
                                                   self.stage("Etiquetas", self.etq, 0.5)], self.at(0, 6))
        (a0, a1), (b0, b1) = self.windows(cola)
        self.assertEqual((a0, a1, b0, b1), (self.at(0, 6), self.at(0, 14), self.at(1, 6), self.at(1, 10)))
        self.assertEqual(cola["summary"]["max_crew"], 1)

        par = stage_planning.preview(self.order, [self.stage("Presintos", self.pre, 1),
                                                  self.stage("Etiquetas", self.etq, 0.5, link="paralelo")], self.at(0, 6))
        (a0, a1), (b0, b1) = self.windows(par)
        self.assertEqual((b0, b1), (self.at(0, 6), self.at(0, 10)))
        self.assertEqual(par["summary"]["max_crew"], 2)  # dos cuadrillas a la vez
        self.assertEqual(par["stages"][1]["link"], "paralelo")

        # A la par: arranca cuando Presintos lleva 60 unidades (07:00) pero, como es más rápida, no termina antes que
        # Presintos (14:00): arranca a las 10:00 y termina a las 14:00.
        flow = stage_planning.preview(self.order, [self.stage("Presintos", self.pre, 1),
                                                   self.stage("Etiquetas", self.etq, 0.5, link="flujo", link_lag=60)],
                                      self.at(0, 6))
        (a0, a1), (b0, b1) = self.windows(flow)
        self.assertEqual((b0, b1), (self.at(0, 10), self.at(0, 14)))
        # Si la de a la par es más lenta, arranca apenas tiene las unidades
        slow = stage_planning.preview(self.order, [self.stage("Presintos", self.pre, 0.5),
                                                   self.stage("Etiquetas", self.etq, 1, link="flujo", link_lag=60)],
                                      self.at(0, 6))
        (a0, a1), (b0, b1) = self.windows(slow)
        self.assertEqual((a1, b0, b1), (self.at(0, 10), self.at(0, 6, 30), self.at(1, 6, 30)))

    def test_queue_after_parallel_group_waits_for_all(self):
        p = stage_planning.preview(self.order, [
            self.stage("Presintos", self.pre, 1),
            self.stage("Etiquetas", self.etq, 0.5, link="paralelo"),
            self.stage("Encajado", self.enc, 0.25),  # en cola: espera a las dos
        ], self.at(0, 6))
        w = self.windows(p)
        self.assertEqual(w[2], (self.at(1, 6), self.at(1, 8)))

    def test_deadline_mode_shares_window_in_parallel(self):
        # Compromiso martes 14:00 = 16 h hábiles. Dos etapas de 16 h de trabajo al mismo tiempo: 1 operario cada una.
        self.order.due_date = self.at(1, 14)
        self.order.save()
        stages = [self.stage("Presintos", self.pre, 2, mode="compromiso", mode_param=None),
                  self.stage("Etiquetas", self.etq, 2, mode="compromiso", mode_param=None, link="paralelo")]
        p = stage_planning.preview(self.order, stages, self.at(0, 6))
        self.assertEqual([s["crew"] for s in p["stages"]], [1, 1])
        self.assertGreaterEqual(p["summary"]["slack_hours"], 0)
        stages[1]["link"] = "cola"  # en cola se reparten las 16 h: 2 operarios cada una
        p = stage_planning.preview(self.order, stages, self.at(0, 6))
        self.assertEqual([s["crew"] for s in p["stages"]], [2, 2])

    def test_first_stage_is_always_queue_and_bad_link(self):
        st = stage_planning.parse_stages(self.order, [self.stage("Presintos", self.pre, 1, link="paralelo")])
        self.assertEqual(st[0].link, "cola")
        with self.assertRaises(ValidationError):
            stage_planning.parse_stages(self.order, [self.stage("A", self.pre, 1), self.stage("B", self.etq, 1, link="x")])

    def test_save_predecessors_and_generate_plan(self):
        stages = [self.stage("Presintos", self.pre, 1, qty_mode="lotes", qty_param=240),
                  self.stage("Etiquetas", self.etq, 0.5, qty_mode="lotes", qty_param=240, link="paralelo"),
                  self.stage("Encajado", self.enc, 0.25)]
        stage_planning.save_plan(self.order, stages, self.at(0, 6), user=self.planner)
        ps = list(self.order.plan_stages.order_by("sequence"))
        self.assertEqual([s.link for s in ps], ["cola", "paralelo", "cola"])
        self.assertEqual(stage_planning.initial_stages(self.order)[1]["link"], "paralelo")
        ops = list(self.order.operations.order_by("sequence"))
        etq = [o for o in ops if o.sequence // 100 == 2]
        enc = [o for o in ops if o.sequence // 100 == 3]
        self.assertEqual(scheduling.predecessors(etq[0], ops), [])            # no espera a Presintos
        self.assertEqual(len(scheduling.predecessors(enc[0], ops)), 4)        # espera a todo el grupo
        conflicts = scheduling.detect_conflicts(ops, [])
        self.assertFalse(any("secuencia" in c for c in conflicts.values()), conflicts)
        # el plan automático también las pone al mismo tiempo
        planning.generate_plan(self.site, start=self.at(0, 6), only_orders={self.order.pk}, log=False)
        ops = {o.sequence: o for o in self.order.operations.all()}
        self.assertEqual(ops[101].planned_start, ops[201].planned_start)
        self.assertGreaterEqual(ops[301].planned_start, max(ops[102].planned_end, ops[202].planned_end))

    def test_generate_plan_flow_stage(self):
        stages = [self.stage("Presintos", self.pre, 1), self.stage("Etiquetas", self.etq, 0.5, link="flujo", link_lag=60)]
        stage_planning.save_plan(self.order, stages, self.at(0, 6), user=self.planner)
        planning.generate_plan(self.site, start=self.at(0, 6), only_orders={self.order.pk}, log=False)
        ops = {o.sequence: o for o in self.order.operations.all()}
        self.assertEqual(ops[101].planned_end, self.at(0, 14))
        self.assertEqual((ops[201].planned_start, ops[201].planned_end), (self.at(0, 10), self.at(0, 14)))

    def test_stage_link_api_from_board(self):
        stage_planning.save_plan(self.order, [self.stage("Presintos", self.pre, 1), self.stage("Etiquetas", self.etq, 0.5)],
                                 self.at(0, 6), user=self.planner)
        op = self.order.operations.get(sequence=201)
        self.client.force_login(self.planner)
        url = reverse("mes_core:api_stage_link")
        first = self.order.operations.get(sequence=101)
        r = self.client.post(url, json.dumps({"operation": first.pk, "link": "paralelo"}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(url, json.dumps({"operation": op.pk, "link": "flujo", "link_lag": "30"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        ps = PlanStage.objects.get(pk=op.stage_id)
        self.assertEqual((ps.link, float(ps.link_lag)), ("flujo", 30.0))
        board = self.client.get(reverse("mes_core:api_board"),
                                {"site": self.site.pk, "view": "semana", "date": self.mon.isoformat()}).json()
        blk = next(b for b in board["blocks"] if b["id"] == op.pk)
        self.assertEqual((blk["link"], blk["link_lag"], blk["stage_first"]), ("flujo", 30.0, False))

    # ------------------------------------------------------------ re-planear lo pendiente
    def _started(self):
        stages = [self.stage("Presintos", self.pre, 1, qty_mode="lotes", qty_param=120),
                  self.stage("Etiquetas", self.etq, 0.5, qty_mode="lotes", qty_param=120)]
        stage_planning.save_plan(self.order, stages, self.at(0, 6), user=self.planner)
        self.order.status = WorkOrder.Status.IN_PROGRESS
        self.order.save()
        ops = {o.sequence: o for o in self.order.operations.all()}
        ops[101].status, ops[101].qty_good = Operation.Status.DONE, Decimal("120")
        ops[101].actual_start, ops[101].actual_end = self.at(0, 6), self.at(0, 8)
        ops[101].save()
        ops[102].status, ops[102].actual_start = Operation.Status.IN_PROGRESS, self.at(0, 8)
        ops[102].save()
        return stages

    def test_replan_pending_keeps_started_work(self):
        stages = self._started()
        self.assertTrue(stage_planning.is_locked(self.order))
        self.assertTrue(stage_planning.can_replan(self.order))
        with self.assertRaises(ValidationError):  # sin el modo re-planear sigue bloqueada
            stage_planning.save_plan(self.order, stages, self.at(0, 9), user=self.planner)
        with self.assertRaises(ValidationError):  # no se agregan etapas
            stage_planning.preview(self.order, stages + [self.stage("Encajado", self.enc, 1)], self.at(0, 9), replan=True)
        info = stage_planning.replan_info(self.order)
        self.assertEqual((info[0]["pending"], info[0]["kept_qty"], info[0]["in_progress"]), (240, 240, 1))
        self.assertEqual(info[1]["pending"], 480)

        # Lo pendiente de Presintos con 2 operarios; Etiquetas pasa a ir a la par
        stages[0]["mode_param"] = 2
        stages[1]["link"] = "flujo"
        p = stage_planning.preview(self.order, stages, self.at(0, 9), replan=True)
        self.assertTrue(p["replan"])
        self.assertEqual(p["stages"][0]["units_now"], 240)
        self.assertEqual(p["summary"]["takt"]["demand"], 480)
        # no arranca antes de que termine el lote en proceso (planeado 08:00–10:00)
        self.assertGreaterEqual(datetime.fromisoformat(p["stages"][0]["start"]), self.at(0, 10))

        stage_planning.save_plan(self.order, stages, self.at(0, 9), user=self.planner, replan=True)
        ops = {o.sequence: o for o in self.order.operations.all()}
        self.assertEqual(ops[101].status, Operation.Status.DONE)
        self.assertEqual(ops[102].status, Operation.Status.IN_PROGRESS)
        self.assertEqual(ops[102].planned_end, self.at(0, 10))       # lo que está en proceso no se mueve
        new = [o for s, o in ops.items() if s // 100 == 1 and s % 100 > 2]
        self.assertEqual(sorted(o.sequence for o in new), [103, 104])  # los lotes siguen la numeración
        self.assertTrue(all(o.crew == 2 and o.status == Operation.Status.PENDING for o in new))
        self.assertTrue(all(o.planned_start >= self.at(0, 10) for o in new))
        self.assertEqual(sum(o.quantity for o in new), 240)
        self.assertEqual(self.order.plan_stages.get(sequence=2).link, "flujo")
        self.assertEqual(self.order.plan_stages.get(sequence=1).quantity, 480)  # la etapa conserva su total
        self.assertEqual(WorkOrder.objects.get(pk=self.order.pk).status, WorkOrder.Status.IN_PROGRESS)

    def test_replan_stage_without_pending_is_kept(self):
        stages = self._started()
        ops = {o.sequence: o for o in self.order.operations.all()}
        for seq in (102, 103, 104):
            ops[seq].status, ops[seq].qty_good = Operation.Status.DONE, Decimal("120")
            ops[seq].actual_start = ops[seq].planned_start
            ops[seq].actual_end = ops[seq].planned_end
            ops[seq].save()
        p = stage_planning.preview(self.order, stages, self.at(1, 6), replan=True)
        self.assertTrue(p["stages"][0]["done"])
        self.assertFalse(p["stages"][1]["done"])
        self.assertTrue(p["summary"]["takt"]["stages"][0]["done"])
        stage_planning.save_plan(self.order, stages, self.at(1, 6), user=self.planner, replan=True)
        self.assertEqual(self.order.operations.filter(sequence__lt=200, status=Operation.Status.DONE).count(), 4)
        self.assertEqual(self.order.operations.filter(sequence__gt=200).count(), 4)

    def test_replan_views(self):
        stages = self._started()
        self.client.force_login(self.planner)
        page = self.client.get(reverse("mes_core:planning_detail", args=[self.order.pk]))
        self.assertContains(page, '"can_replan": true')
        self.assertContains(page, "Re-planear lo pendiente")
        url = reverse("mes_core:api_plan_save", args=[self.order.pk])
        body = {"stages": stages, "start": self.at(0, 9).strftime("%Y-%m-%dT%H:%M")}
        r = self.client.post(url, json.dumps(body), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(url, json.dumps({**body, "replan": True}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        # sin nada pendiente ya no se puede re-planear
        self.order.operations.update(status=Operation.Status.DONE)
        self.assertFalse(stage_planning.can_replan(self.order))
