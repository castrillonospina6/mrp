import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from . import planning, scheduling, services
from .importers import import_orders_csv
from .models import Customer, Downtime, Operation, Product, Shift, Site, WorkCenter, WorkOrder
from .plugins import registry

TZ = ZoneInfo("America/Bogota")  # = settings.TIME_ZONE por defecto


def _shift(a, b, name="T"):
    return Shift(name=name, start_time=a, end_time=b)


class ShiftEngineTests(TestCase):
    def test_within_shift(self):
        s, e = scheduling.fit(datetime(2026, 1, 5, 8, 0, tzinfo=TZ), 60, [_shift(time(6), time(14))])
        self.assertEqual((s.hour, e.hour), (8, 9))

    def test_before_shift_moves_to_start(self):
        s, _ = scheduling.fit(datetime(2026, 1, 5, 3, 0, tzinfo=TZ), 30, [_shift(time(6), time(14))])
        self.assertEqual(s, datetime(2026, 1, 5, 6, 0, tzinfo=TZ))

    def test_spills_to_next_day(self):
        s, e = scheduling.fit(datetime(2026, 1, 5, 13, 0, tzinfo=TZ), 120, [_shift(time(6), time(14))])
        self.assertEqual(s, datetime(2026, 1, 5, 13, 0, tzinfo=TZ))
        self.assertEqual(e, datetime(2026, 1, 6, 7, 0, tzinfo=TZ))

    def test_no_shifts_is_24h(self):
        s, e = scheduling.fit(datetime(2026, 1, 5, 23, 0, tzinfo=TZ), 120, [])
        self.assertEqual(e - s, timedelta(hours=2))

    def test_night_shift_crosses_midnight(self):
        night = _shift(time(22), time(6), "Noche")
        # La noche "del martes" va de lunes 22:00 a martes 06:00.
        a, b = scheduling.shift_window(night, date(2026, 1, 6), TZ)
        self.assertEqual((a, b), (datetime(2026, 1, 5, 22, 0, tzinfo=TZ), datetime(2026, 1, 6, 6, 0, tzinfo=TZ)))
        _, e = scheduling.fit(datetime(2026, 1, 5, 23, 0, tzinfo=TZ), 240, [night])
        self.assertEqual(e, datetime(2026, 1, 6, 3, 0, tzinfo=TZ))

    def test_contiguous_shifts_merge(self):
        shifts = [_shift(time(6), time(14)), _shift(time(14), time(22))]
        wins = scheduling.working_windows(shifts, datetime(2026, 1, 5, 0, 0, tzinfo=TZ),
                                          datetime(2026, 1, 6, 0, 0, tzinfo=TZ))
        self.assertEqual(wins, [(datetime(2026, 1, 5, 6, 0, tzinfo=TZ), datetime(2026, 1, 5, 22, 0, tzinfo=TZ))])

    def test_fit_avoiding_downtime(self):
        blocked = [(datetime(2026, 1, 5, 8, 30, tzinfo=TZ), datetime(2026, 1, 5, 10, 0, tzinfo=TZ))]
        s, _ = scheduling.fit_avoiding(datetime(2026, 1, 5, 8, 0, tzinfo=TZ), 60, [], blocked)
        self.assertEqual(s, datetime(2026, 1, 5, 10, 0, tzinfo=TZ))

    def test_weekly_slots(self):
        shifts = [_shift(time(14), time(22), "Tarde"), _shift(time(22), time(6), "Noche"), _shift(time(6), time(14), "Mañana")]
        days = scheduling.view_days("semana", date(2026, 11, 18))
        self.assertEqual(days[0], date(2026, 11, 16))  # lunes
        slots = scheduling.build_slots(scheduling.sort_shifts(shifts), "semana", days)
        self.assertEqual(len(slots), 21)
        self.assertEqual([s.label for s in slots[:3]], ["Noche", "Mañana", "Tarde"])


class CoreFlowTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="S1", name="Sitio")
        # Sin turnos en la planta = operación 24 h.
        WorkCenter.objects.create(site=self.site, code="P1", name="Picking", kind="picking", capacity=1)
        WorkCenter.objects.create(site=self.site, code="V1", name="Verif", kind="verificacion", capacity=1)
        WorkCenter.objects.create(site=self.site, code="D1", name="Desp", kind="despacho", capacity=1)
        self.product = Product.objects.create(sku="X", name="Producto X")

    def _order(self, qty=100, **kw):
        return services.create_order(site=self.site, product=self.product, process_type="picking", quantity=qty, **kw)

    def test_plugins_register_process_types(self):
        keys = {pt.key for pt in registry.process_types()}
        self.assertTrue({"picking", "acondicionado", "deshuese"} <= keys)

    def test_create_order_generates_operations_and_number(self):
        order = self._order()
        self.assertEqual(order.number, f"OP-{order.pk:06d}")
        self.assertEqual(order.operations.count(), len(registry.get_process_type("picking").operations))

    def test_unknown_process_type_rejected(self):
        with self.assertRaises(ValidationError):
            services.create_order(site=self.site, product=self.product, process_type="nope", quantity=1)

    def test_plan_is_sequential_and_respects_capacity(self):
        a = self._order(priority=1)
        b = self._order(priority=2)
        result = planning.generate_plan(self.site, start=datetime(2026, 1, 5, 8, 0, tzinfo=TZ))
        self.assertEqual(len(result.orders), 2)
        ops_a = list(a.operations.all())
        for prev, nxt in zip(ops_a, ops_a[1:]):
            self.assertLessEqual(prev.planned_end, nxt.planned_start)
        # Capacidad 1 en picking: la operación de picking de B empieza cuando termina la de A.
        self.assertGreaterEqual(b.operations.get(name="Picking").planned_start, a.operations.get(name="Picking").planned_end)
        a.refresh_from_db()
        self.assertEqual(a.status, WorkOrder.Status.PLANNED)

    def test_plan_reports_missing_work_centers(self):
        other = Site.objects.create(code="S2", name="Sin centros")
        services.create_order(site=other, product=self.product, process_type="picking", quantity=10)
        result = planning.generate_plan(other)
        self.assertEqual(len(result.unassigned_operations), 4)

    def test_report_requires_released(self):
        order = self._order()
        with self.assertRaises(ValidationError):
            services.report_production(order.operations.first(), qty_good=1)

    def test_full_production_flow(self):
        order = services.release_order(self._order(qty=10))
        for op in order.operations.all():
            services.report_production(op, qty_good=10, finished=True)
        order.refresh_from_db()
        self.assertEqual(order.status, WorkOrder.Status.DONE)
        self.assertEqual(order.quantity_done, Decimal("10"))
        self.assertFalse(order.operations.exclude(status=Operation.Status.DONE).exists())

    def test_pages_render(self):
        self.client.force_login(get_user_model().objects.create_superuser("u", password="p"))
        order = self._order()
        planning.generate_plan(self.site)
        for url in [reverse("mes_core:dashboard"), reverse("mes_core:order_list"), reverse("mes_core:order_create"),
                    order.get_absolute_url(), reverse("mes_core:plan") + f"?site={self.site.pk}",
                    reverse("mes_core:plugins"), reverse("copacking:spec_list"), reverse("food:lot_list"),
                    reverse("mes_core:scheduler")]:
            self.assertEqual(self.client.get(url).status_code, 200, url)


class SchedulerTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="S1", name="Sitio")
        for name, a, b in [("Noche", time(22), time(6)), ("Mañana", time(6), time(14)), ("Tarde", time(14), time(22))]:
            Shift.objects.create(site=self.site, name=name, start_time=a, end_time=b)
        self.pick = WorkCenter.objects.create(site=self.site, code="P1", name="Picking", kind="picking", capacity=1)
        self.ver = WorkCenter.objects.create(site=self.site, code="V1", name="Verif", kind="verificacion", capacity=1)
        self.product = Product.objects.create(sku="X", name="Producto X")
        self.user = get_user_model().objects.create_user("planner", password="p")
        self.user.groups.add(Group.objects.get(name="Planeación"))
        self.client.force_login(self.user)

    def _order(self, qty=100, **kw):
        return services.create_order(site=self.site, product=self.product, process_type="picking", quantity=qty, **kw)

    def _slot(self, day=date(2026, 11, 16), shift="Mañana"):
        return scheduling.shift_window(Shift.objects.get(site=self.site, name=shift), day)

    def test_place_stacks_inside_shift_and_logs(self):
        a, b = self._order(qty=10), self._order(qty=10)
        op_a, op_b = a.operations.get(name="Picking"), b.operations.get(name="Picking")
        start, end = self._slot()
        scheduling.place_operation(op_a, self.pick, start, end, user=self.user)
        scheduling.place_operation(op_b, self.pick, start, end, user=self.user)
        op_a.refresh_from_db()
        op_b.refresh_from_db()
        self.assertEqual(op_a.planned_start, start)
        self.assertEqual(op_b.planned_start, op_a.planned_end)  # capacidad 1 → se apila dentro del turno
        self.assertEqual(self.site.schedule_changes.count(), 2)

    def test_place_rejects_incompatible_line(self):
        op = self._order().operations.get(name="Picking")
        start, end = self._slot()
        with self.assertRaises(ValidationError):
            scheduling.place_operation(op, self.ver, start, end)

    def test_capacity_and_downtime_conflicts(self):
        start, end = self._slot()
        for order in (self._order(), self._order()):
            op = order.operations.get(name="Picking")
            op.work_center, op.planned_start, op.planned_end = self.pick, start, end
            op.save()
        Downtime.objects.create(work_center=self.pick, start=start, end=start + timedelta(hours=1), reason="Mant")
        ops = list(Operation.objects.select_related("work_center").filter(work_center=self.pick))
        conflicts = scheduling.detect_conflicts(ops, list(Downtime.objects.all()))
        for op in ops:
            self.assertEqual(conflicts[op.pk], {"solape", "mantenimiento"})

    def test_sequence_conflict(self):
        order = self._order()
        first, second = list(order.operations.all())[:2]
        start, end = self._slot()
        first.planned_start, first.planned_end = start, end
        second.planned_start, second.planned_end = start, start + timedelta(hours=1)
        first.save()
        second.save()
        conflicts = scheduling.detect_conflicts(list(order.operations.all()), [])
        self.assertIn("secuencia", conflicts[second.pk])

    def test_unschedule_updates_order_dates(self):
        order = self._order()
        op = order.operations.get(name="Picking")
        start, end = self._slot()
        scheduling.place_operation(op, self.pick, start, end)
        order.refresh_from_db()
        self.assertEqual(order.planned_start, start)
        scheduling.unschedule_operation(op)
        order.refresh_from_db()
        self.assertIsNone(order.planned_start)

    def test_auto_plan_keeps_manual_placement(self):
        order = self._order(qty=10)
        op = order.operations.get(name="Picking")
        start, end = self._slot(day=date(2026, 11, 18))
        scheduling.place_operation(op, self.pick, start, end)
        planning.generate_plan(self.site, start=datetime(2026, 11, 16, 6, 0, tzinfo=TZ), keep_existing=True)
        op.refresh_from_db()
        self.assertEqual(op.planned_start, start)
        self.assertGreaterEqual(order.operations.get(name="Verificación").planned_start, op.planned_end)

    def test_api_board_and_actions(self):
        order = self._order(customer=Customer.objects.create(name="Cliente VIP", tier="vip"))
        board = reverse("mes_core:api_board")
        data = self.client.get(board, {"site": self.site.pk, "view": "semana", "date": "2026-11-16"}).json()
        self.assertEqual(len(data["slots"]), 21)
        self.assertEqual(len(data["backlog"]), order.operations.count())
        self.assertEqual(data["backlog"][0]["tier"], "vip")

        op = order.operations.get(name="Picking")
        slot = data["slots"][1]
        payload = {"operation": op.pk, "line": self.pick.pk, "slot_start": slot["start"], "slot_end": slot["end"]}
        r = self.client.post(reverse("mes_core:api_place"), json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        payload["line"] = self.ver.pk
        r = self.client.post(reverse("mes_core:api_place"), json.dumps(payload), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("requiere", r.json()["error"])

        data = self.client.get(board, {"site": self.site.pk, "view": "semana", "date": "2026-11-16"}).json()
        self.assertEqual([b["id"] for b in data["blocks"]], [op.pk])
        for view in ("dia", "mes"):
            self.assertEqual(self.client.get(board, {"site": self.site.pk, "view": view}).status_code, 200)

    def test_csv_import(self):
        text = "planta;sku;proceso;cantidad;compromiso;prioridad;cliente\nS1;X;picking;12,5;2026-11-20;1;Nuevo\n"
        created, errors = import_orders_csv(text.encode("utf-8"))
        self.assertEqual(errors, [])
        order = WorkOrder.objects.get(number=created[0])
        self.assertEqual(order.quantity, Decimal("12.5"))
        self.assertEqual(order.customer.name, "Nuevo")

    def test_csv_import_all_or_nothing(self):
        text = "planta,sku,proceso,cantidad\nS1,X,picking,10\nS1,NOPE,picking,5\n"
        created, errors = import_orders_csv(text.encode("utf-8"))
        self.assertEqual(created, [])
        self.assertTrue(any("NOPE" in e for e in errors))
        self.assertFalse(WorkOrder.objects.exists())


class EditPanelTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="S1", name="Sitio")
        self.pick = WorkCenter.objects.create(site=self.site, code="P1", name="Picking", kind="picking", capacity=1)
        self.pick2 = WorkCenter.objects.create(site=self.site, code="P2", name="Picking 2", kind="picking", capacity=1)
        self.ver = WorkCenter.objects.create(site=self.site, code="V1", name="Verif", kind="verificacion", capacity=1)
        self.product = Product.objects.create(sku="X", name="Producto X")
        planner = get_user_model().objects.create_user("planner", password="p")
        planner.groups.add(Group.objects.get(name="Planeación"))
        self.client.force_login(planner)
        self.order = services.create_order(site=self.site, product=self.product, process_type="picking", quantity=10,
                                           due_date=datetime(2026, 11, 20, 18, 0, tzinfo=TZ))
        self.op = self.order.operations.get(name="Picking")

    def _post(self, **extra):
        body = {"operation": self.op.pk, "line": self.pick2.pk, "start": "2026-11-16T06:00", "end": "2026-11-16T14:00", **extra}
        return self.client.post(reverse("mes_core:api_edit"), json.dumps(body), content_type="application/json")

    def test_edit_sets_exact_times_line_and_order_terms(self):
        r = self._post(priority=1, due_date="2026-11-18")
        self.assertEqual(r.status_code, 200, r.content)
        self.op.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.op.work_center, self.pick2)
        self.assertEqual(self.op.planned_start, datetime(2026, 11, 16, 6, 0, tzinfo=TZ))
        self.assertEqual(self.op.planned_end, datetime(2026, 11, 16, 14, 0, tzinfo=TZ))
        self.assertEqual(self.order.priority, 1)
        self.assertEqual(self.order.due_date, datetime(2026, 11, 18, 18, 0, tzinfo=TZ))  # conserva la hora
        self.assertEqual(self.site.schedule_changes.filter(action="editar").count(), 1)

    def test_edit_rejects_end_before_start_and_wrong_line_atomically(self):
        r = self._post(end="2026-11-16T05:00", priority=1)
        self.assertEqual(r.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.priority, 3)  # no se aplicó nada
        r = self._post(line=self.ver.pk)
        self.assertEqual(r.status_code, 400)
        self.assertIn("requiere", r.json()["error"])

    def test_board_payload_has_routes_and_shift_times(self):
        Shift.objects.create(site=self.site, name="Mañana", start_time=time(6), end_time=time(14))
        data = self.client.get(reverse("mes_core:api_board"), {"site": self.site.pk}).json()
        self.assertEqual(data["shifts"][0]["start"], "06:00")
        self.assertEqual(len(data["routes"][str(self.order.pk)]), self.order.operations.count())
        self.assertEqual(data["all_lines"][0]["kind"], "picking")
