"""Cierre automático: al llegar al 100 % de lo programado la operación (y el pedido) pasan solos a terminada."""

from datetime import date, datetime

from django.test import TestCase

from . import corrections, services, signals, stage_planning
from .models import Operation, Product, Site, WorkCenter, WorkOrder
from .tests_planning import TZ, day_shift


class AutoCloseTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="P", name="Planta")
        day_shift(self.site, operators=10)
        self.a = WorkCenter.objects.create(site=self.site, code="ACO", name="Acondicionado", kind="acondicionado")
        self.b = WorkCenter.objects.create(site=self.site, code="EMP", name="Empaque", kind="acondicionado")
        product = Product.objects.create(sku="CAM", name="Camiseta")
        self.order = services.receive_order(site=self.site, product=product, process_type="acondicionado",
                                            quantity=100, received_on=date(2026, 11, 14),
                                            delivery_date=date(2026, 11, 25), transit_days=2)
        stages = [{"name": n, "line": wc.pk, "quantity": 100, "std_value": 0.5, "std_unit": "min_unidad",
                   "setup_minutes": 0, "mode": "turno", "qty_mode": "todo"} for n, wc in (("Etiquetado", self.a),
                                                                                         ("Empaque", self.b))]
        stage_planning.save_plan(self.order, stages, datetime(2026, 11, 16, 6, 0, tzinfo=TZ))
        WorkOrder.objects.filter(pk=self.order.pk).update(status=WorkOrder.Status.RELEASED)
        self.op1, self.op2 = self.order.operations.order_by("sequence")
        self.completed = []
        signals.order_completed.connect(self._done, sender=WorkOrder)
        self.addCleanup(signals.order_completed.disconnect, self._done, sender=WorkOrder)

    def _done(self, sender, order, **kw):
        self.completed.append(order.pk)

    def st(self, *objs):
        for o in objs:
            o.refresh_from_db()
        return tuple(o.status for o in objs)

    def test_operation_and_order_close_themselves_at_100_percent(self):
        services.report_production(self.op1, qty_good=60)
        self.assertEqual(self.st(self.op1), (Operation.Status.IN_PROGRESS,))
        rep = services.report_production(self.op1, qty_good=40)  # sin marcar «terminada»
        self.assertTrue(rep.finished)
        self.assertEqual(self.st(self.op1, self.order), (Operation.Status.DONE, WorkOrder.Status.IN_PROGRESS))
        self.assertIsNotNone(self.op1.actual_end)
        services.report_production(self.op2, qty_good=70, qty_leftover=30)  # sobrantes completan lo programado
        self.assertEqual(self.st(self.op2, self.order), (Operation.Status.DONE, WorkOrder.Status.DONE))
        self.assertEqual(self.completed, [self.order.pk])

    def test_voiding_below_100_reopens_and_reactivating_closes_again(self):
        services.report_production(self.op1, qty_good=50)
        r2 = services.report_production(self.op1, qty_good=50)
        self.assertEqual(self.st(self.op1), (Operation.Status.DONE,))
        v = corrections.void_report(r2, "error de conteo", user=None)
        self.assertEqual(self.st(self.op1), (Operation.Status.IN_PROGRESS,))
        corrections.reactivate_report(v, "era correcto", user=None)
        self.assertEqual(self.st(self.op1), (Operation.Status.DONE,))

    def test_edit_that_reaches_100_closes(self):
        rep = services.report_production(self.op1, qty_good=90)
        corrections.edit_report(rep, reason="recuento", user=None, qty_good=100)
        self.assertEqual(self.st(self.op1), (Operation.Status.DONE,))

    def test_already_full_but_open_operations_are_closed_by_the_check(self):
        services.report_production(self.op1, qty_good=100)
        services.report_production(self.op2, qty_good=40)
        # como quedaban antes: al 100 % pero abiertas
        Operation.objects.filter(pk__in=[self.op1.pk, self.op2.pk]).update(status=Operation.Status.IN_PROGRESS)
        Operation.objects.filter(pk=self.op2.pk).update(qty_good=100)
        WorkOrder.objects.filter(pk=self.order.pk).update(status=WorkOrder.Status.IN_PROGRESS)
        self.completed.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(services.close_completed(), 2)
        self.assertEqual(self.st(self.op1, self.op2, self.order),
                         (Operation.Status.DONE, Operation.Status.DONE, WorkOrder.Status.DONE))
        self.assertEqual(self.completed, [self.order.pk])
        self.assertEqual(services.close_completed(), 0)  # ya no hay nada que cerrar

    def test_partial_operations_stay_open(self):
        services.report_production(self.op1, qty_good=99)
        self.assertEqual(services.close_completed(), 0)
        self.assertEqual(self.st(self.op1), (Operation.Status.IN_PROGRESS,))
