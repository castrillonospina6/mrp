"""Pruebas del calendario laboral y de la planeación avanzada."""

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import horizon, planning, scheduling, simulation, versions, workcal
from .capacity import compute
from .models import (
    CalendarException, ChangeoverTime, Customer, Operation, PlanningRules, PlanVersion, Product, ProductionReport,
    Shift, Site, WorkCenter, WorkOrder,
)

TZ = ZoneInfo("America/Bogota")
MON = date(2026, 11, 16)  # lunes


def at(d, h, m=0):
    return datetime.combine(d, time(h, m), tzinfo=TZ)


class Base(TestCase):
    def setUp(self):
        workcal.invalidate()
        self.site = Site.objects.create(code="CEDI", name="Centro", operators_total=20)
        self.shift = Shift.objects.create(site=self.site, name="Mañana", start_time=time(6), end_time=time(14),
                                          weekdays="01234")  # lunes a viernes
        self.a = WorkCenter.objects.create(site=self.site, code="ETQ1", name="Etiq 1", kind="etiquetado")
        self.b = WorkCenter.objects.create(site=self.site, code="ETQ2", name="Etiq 2", kind="etiquetado")
        self.p1 = Product.objects.create(sku="CAM", name="Camiseta", family="Camisetas")
        self.p2 = Product.objects.create(sku="JEA", name="Jean", family="Jeans")

    def order(self, product=None, qty=60, priority=3, due=None, customer=None, mpu="1", status=WorkOrder.Status.PLANNED):
        o = WorkOrder.objects.create(site=self.site, product=product or self.p1, process_type="x", quantity=Decimal(qty),
                                     priority=priority, due_date=due, customer=customer, status=status)
        Operation.objects.create(order=o, sequence=1, name="Etiquetar", work_center_kind="etiquetado",
                                 minutes_per_unit=Decimal(mpu))
        return o

    def windows(self, wc, start, end):
        return scheduling.working_windows(scheduling.center_shifts(wc), start, end)


class ColombiaHolidaysTests(TestCase):
    def test_2026(self):
        days = dict(workcal.colombia_holidays(2026))
        self.assertEqual(len(days), 18)
        expected = {date(2026, 1, 1), date(2026, 1, 12), date(2026, 3, 23), date(2026, 4, 2), date(2026, 4, 3),
                    date(2026, 5, 1), date(2026, 5, 18), date(2026, 6, 8), date(2026, 6, 15), date(2026, 6, 29),
                    date(2026, 7, 20), date(2026, 8, 7), date(2026, 8, 17), date(2026, 10, 12), date(2026, 11, 2),
                    date(2026, 11, 16), date(2026, 12, 8), date(2026, 12, 25)}
        self.assertEqual(set(days), expected)


class CalendarEngineTests(Base):
    def test_weekdays_holiday_overtime_extra_and_closure(self):
        # sábado: el turno no trabaja
        self.assertEqual(self.windows(self.a, at(MON + timedelta(days=5), 0), at(MON + timedelta(days=6), 0)), [])
        tue = MON + timedelta(days=1)
        CalendarException.objects.create(site=self.site, kind="festivo", date=tue, name="Prueba")
        self.assertEqual(self.windows(self.a, at(tue, 0), at(tue, 23)), [])
        wed = MON + timedelta(days=2)
        CalendarException.objects.create(site=self.site, kind="horas_extra", date=wed, shift=self.shift,
                                         start_time=time(14), end_time=time(16), operators=8)
        self.assertEqual(self.windows(self.a, at(wed, 0), at(wed, 23)), [(at(wed, 6), at(wed, 16))])
        sat = MON + timedelta(days=5)
        extra = CalendarException.objects.create(site=self.site, kind="turno_adicional", date=sat, name="Sábado",
                                                 start_time=time(7), end_time=time(11), operators=10)
        extra.work_centers.set([self.b])
        self.assertEqual(self.windows(self.a, at(sat, 0), at(sat, 23)), [])            # solo para ETQ2
        self.assertEqual(self.windows(self.b, at(sat, 0), at(sat, 23)), [(at(sat, 7), at(sat, 11))])
        thu = MON + timedelta(days=3)
        cl = CalendarException.objects.create(site=self.site, kind="cierre", date=thu, name="Inventario")
        cl.work_centers.set([self.a])
        self.assertEqual(self.windows(self.a, at(thu, 0), at(thu, 23)), [])
        self.assertEqual(self.windows(self.b, at(thu, 0), at(thu, 23)), [(at(thu, 6), at(thu, 14))])

    def test_fit_skips_weekend_and_holiday(self):
        fri = MON + timedelta(days=4)
        CalendarException.objects.create(site=self.site, kind="festivo", date=MON + timedelta(days=7))  # lunes siguiente
        s, e = scheduling.fit(at(fri, 12), 240, scheduling.center_shifts(self.a))
        self.assertEqual((s, e), (at(fri, 12), at(MON + timedelta(days=8), 8)))  # sigue el martes 08:00

    def test_slots_capacity_and_operators(self):
        wed = MON + timedelta(days=2)
        CalendarException.objects.create(site=self.site, kind="horas_extra", date=wed, shift=self.shift,
                                         start_time=time(14), end_time=time(16), operators=8)
        CalendarException.objects.create(site=self.site, kind="turno_adicional", date=MON + timedelta(days=5),
                                         name="Sábado", start_time=time(7), end_time=time(11))
        CalendarException.objects.create(site=self.site, kind="festivo", date=MON + timedelta(days=1), name="Fiesta")
        slots = scheduling.build_slots(scheduling.site_shifts(self.site), "semana", [MON + timedelta(days=i) for i in range(7)],
                                       self.site.pk)
        by_day = {}
        for sl in slots:
            by_day.setdefault(sl.day.weekday(), []).append(sl)
        self.assertIn("Festivo", by_day[1][0].off)
        self.assertEqual(by_day[2][0].note, "+2 h extra")
        self.assertEqual(by_day[2][0].end, at(wed, 16))
        self.assertTrue(any(s.extra for s in by_day[5]))
        self.assertEqual(by_day[6][0].off, "No laboral")
        # capacidad: festivo = 0; horas extra suman 2 h × 8 personas
        cap_tue = compute(self.site, self.shift, MON + timedelta(days=1), [], {})
        self.assertEqual((cap_tue.capacity_h, bool(cap_tue.off)), (0, True))
        cap_wed = compute(self.site, self.shift, wed, [], {})
        self.assertAlmostEqual(cap_wed.capacity_h, 20 * 8 + 2 * 8)
        ledger = scheduling.OperatorLedger(self.site)
        self.assertEqual(ledger.available_at(at(wed, 15)), 8)
        self.assertTrue(ledger.is_working(at(MON + timedelta(days=5), 8)))
        self.assertFalse(ledger.is_working(at(MON + timedelta(days=1), 8)))


class PlannerRulesTests(Base):
    def test_priority_rules_put_vip_first(self):
        vip = Customer.objects.create(name="VIP", tier="vip")
        low = self.order(priority=3)
        top = self.order(priority=3, customer=vip)
        PlanningRules.objects.create(site=self.site, w_due=0, w_tier=10, w_age=0, w_priority=0, group_changeovers=False)
        planning.generate_plan(self.site, start=at(MON, 6))
        top.refresh_from_db(); low.refresh_from_db()
        self.assertLess(top.planned_start, low.planned_end)
        self.assertEqual(top.planned_start, at(MON, 6))

    def test_frozen_operations_stay_and_api_asks_confirmation(self):
        o = self.order()
        op = o.operations.get()
        start = timezone.now() + timedelta(hours=2)
        op.work_center, op.planned_start, op.planned_end = self.a, start, start + timedelta(hours=1)
        op.save()
        PlanningRules.objects.create(site=self.site, frozen_hours=24)
        planning.generate_plan(self.site)
        op.refresh_from_db()
        self.assertEqual(op.planned_start, start)  # congelada: no se movió
        user = get_user_model().objects.create_user("plan", password="x")
        user.groups.add(Group.objects.get(name="Planeación"))
        self.client.force_login(user)
        url = reverse("mes_core:api_resize")
        new_end = timezone.localtime(start + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        r = self.client.post(url, json.dumps({"operation": op.pk, "end": new_end}), content_type="application/json")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["frozen"])
        r = self.client.post(url, json.dumps({"operation": op.pk, "end": new_end, "force": True}), content_type="application/json")
        self.assertEqual(r.status_code, 200)

    def test_changeover_between_families(self):
        WorkCenter.objects.filter(pk=self.b.pk).update(active=False)
        first = self.order(product=self.p1, priority=1, qty=30)
        second = self.order(product=self.p2, priority=2, qty=30)
        PlanningRules.objects.create(site=self.site, group_changeovers=False)
        ChangeoverTime.objects.create(site=self.site, from_family="Camisetas", to_family="Jeans", minutes=20)
        res = planning.generate_plan(self.site, start=at(MON, 6))
        op2 = second.operations.get()
        self.assertEqual(op2.changeover_minutes, Decimal("20"))
        self.assertEqual(op2.planned_start, at(MON, 6, 30))
        self.assertEqual(op2.planned_end, at(MON, 7, 20))
        self.assertEqual(res.changeover_minutes, 20)

    def test_auto_split_runs_parts_in_parallel(self):
        o = self.order(qty=960)  # 960 min = 16 h
        PlanningRules.objects.create(site=self.site, allow_split=True, split_threshold_hours=8)
        res = planning.generate_plan(self.site, start=at(MON, 6))
        ops = list(o.operations.order_by("sequence"))
        self.assertEqual(len(ops), 2)
        self.assertEqual(res.split_operations, 1)
        self.assertEqual({op.work_center_id for op in ops}, {self.a.pk, self.b.pk})
        self.assertEqual({op.planned_start for op in ops}, {at(MON, 6)})
        self.assertEqual(sum(op.qty for op in ops), Decimal("960"))

    def test_manual_split_merge_and_sequence(self):
        o = self.order(qty=100)
        nxt = Operation.objects.create(order=o, sequence=2, name="Empacar", work_center_kind="etiquetado",
                                       minutes_per_unit=Decimal("0.1"))
        op = o.operations.get(sequence=1)
        parts = scheduling.split_operation(op, [(self.a, Decimal("60")), (self.b, Decimal("40"))])
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[1].split_of_id, op.pk)
        items = list(o.operations.all())
        self.assertEqual({p.pk for p in scheduling.predecessors(nxt, items)}, {op.pk, parts[1].pk})
        root = scheduling.merge_operation(parts[1])
        self.assertEqual(root.qty, Decimal("100"))
        self.assertEqual(o.operations.count(), 2)

    def test_only_orders_keeps_others(self):
        other = self.order()
        planning.generate_plan(self.site, start=at(MON, 6))
        other_op = other.operations.get()
        before = other_op.planned_start
        new = self.order(priority=1)
        planning.generate_plan(self.site, start=at(MON, 6), only_orders={new.pk})
        other_op.refresh_from_db()
        self.assertEqual(other_op.planned_start, before)
        self.assertIsNotNone(new.operations.get().planned_start)


class DecisionToolsTests(Base):
    def test_simulation_does_not_persist_and_apply_does(self):
        self.order(due=at(MON + timedelta(days=30), 12))
        n_orders, n_exc = WorkOrder.objects.count(), CalendarException.objects.count()
        change = {"kind": "turno_adicional", "date": (MON + timedelta(days=5)).isoformat(), "start_time": "06:00",
                  "end_time": "14:00", "operators": 10}
        order_change = {"kind": "pedido", "process_type": "picking", "quantity": 100, "priority": 1}
        out = simulation.compare(self.site, [{"name": "A", "changes": [change]}, {"name": "B", "changes": [order_change]}])
        self.assertEqual(WorkOrder.objects.count(), n_orders)
        self.assertEqual(CalendarException.objects.count(), n_exc)
        self.assertEqual(len(out["results"]), 2)
        simulation.apply_scenario(self.site, [change])
        self.assertEqual(CalendarException.objects.count(), n_exc + 1)

    def test_promise_and_versions_with_approval(self):
        r = simulation.promise(self.site, process_type="picking", quantity=10, transit_days=2)
        self.assertNotIn("error", r)
        self.assertEqual(r["delivery"], r["dispatch"] + timedelta(days=2))
        self.assertEqual(WorkOrder.objects.filter(reference="Promesa").count(), 0)

        o = self.order(due=at(MON + timedelta(days=10), 12))
        planning.generate_plan(self.site, start=at(MON, 6))
        v = versions.snapshot(self.site, name="Semana", propose=True)
        self.assertEqual(v.status, PlanVersion.Status.PROPOSED)
        self.assertEqual(v.summary["ops"], 1)
        diff = versions.compare(v.data, versions.current_plan(self.site))
        self.assertEqual(diff["changes"], 0)
        with self.assertRaises(Exception):
            versions.reject(v, note="")
        res = versions.approve(v)
        o.refresh_from_db()
        self.assertEqual(o.status, WorkOrder.Status.SUPPLIES)  # aprobado → pasa a insumos, que libera a planta
        self.assertIn(o.number, res["released"])

    def test_adherence(self):
        o = self.order(qty=100)
        op = o.operations.get()
        op.work_center, op.planned_start, op.planned_end = self.a, at(MON, 6), at(MON, 8)
        op.status = Operation.Status.IN_PROGRESS
        op.actual_start = at(MON, 6, 10)
        op.save()
        versions.snapshot(self.site)
        rep = ProductionReport.objects.create(operation=op, qty_good=Decimal("80"))
        ProductionReport.objects.filter(pk=rep.pk).update(reported_at=at(MON, 7))
        PlanVersion.objects.update(created_at=at(MON, 5))
        data = versions.adherence(self.site, at(MON, 6), at(MON, 14))
        self.assertEqual(data["adherence"], 80)
        self.assertEqual(data["items"][0]["delay"], 10)
        self.assertEqual(data["counts"]["parcial"], 1)

    def test_medium_term_and_forecast(self):
        today = MON + timedelta(days=2)
        for w in range(1, 11):  # 10 semanas de historia
            o = self.order(qty=100)
            o.received_on = MON - timedelta(weeks=w)
            o.process_type = "picking"
            o.save(update_fields=["received_on", "process_type"])
        fc = horizon.forecast(self.site, weeks=4, today=today)
        row = next(r for r in fc["rows"] if r["key"] == "picking")
        self.assertEqual(row["qty"], [100.0] * 4)
        data = horizon.medium_term(self.site, weeks=4, today=today)
        self.assertEqual(len(data["people"]), 4)
        self.assertTrue(data["recommendations"])


class PlannerPagesTests(Base):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user("plan", password="x")
        self.user.groups.add(Group.objects.get(name="Planeación"))
        self.boss = get_user_model().objects.create_user("jefe", password="x")
        self.boss.groups.add(Group.objects.get(name="Jefe de planta"))

    def test_pages_render(self):
        self.order(due=at(MON + timedelta(days=3), 12))
        self.client.force_login(self.user)
        for name in ("work_calendar", "planning_rules", "what_if", "overtime_suggestions", "medium_term", "plan_versions",
                     "plan_adherence", "scheduler", "plan", "dashboard"):
            r = self.client.get(reverse(f"mes_core:{name}"), {"site": self.site.pk})
            self.assertEqual(r.status_code, 200, name)
        api = self.client.get(reverse("mes_core:api_board"), {"site": self.site.pk, "view": "semana"})
        self.assertIn("line_windows", api.json())

    def test_calendar_crud_and_holidays(self):
        self.client.force_login(self.user)
        url = reverse("mes_core:work_calendar")
        r = self.client.post(url, {"action": "holidays", "site": self.site.pk, "year": 2026})
        self.assertEqual(CalendarException.objects.filter(kind="festivo").count(), 18)
        r = self.client.post(url, {"action": "create", "site": self.site.pk, "kind": "horas_extra", "date": "2026-11-18",
                                   "shift": self.shift.pk, "start_time": "14:00", "end_time": "16:00", "operators": "5",
                                   "lines": [self.a.pk]})
        self.assertEqual(r.status_code, 302)
        exc = CalendarException.objects.get(kind="horas_extra")
        self.assertEqual(list(exc.work_centers.all()), [self.a])
        self.client.post(url, {"action": "create", "site": self.site.pk, "kind": "horas_extra", "date": "2026-11-18"})
        self.assertEqual(CalendarException.objects.filter(kind="horas_extra").count(), 1)  # inválida: sin turno
        self.client.post(url, {"action": "delete", "site": self.site.pk, "id": exc.pk})
        self.assertFalse(CalendarException.objects.filter(kind="horas_extra").exists())

    def test_rules_simulation_and_approval_flow(self):
        o = self.order(due=at(MON + timedelta(days=3), 12))
        self.client.force_login(self.user)
        self.client.post(reverse("mes_core:planning_rules"), {
            "action": "rules", "site": self.site.pk, "w_due": 5, "w_tier": 2, "w_age": 0, "w_priority": 4,
            "frozen_hours": 0, "default_changeover_minutes": 10, "split_threshold_hours": 16, "require_approval": "on"})
        self.assertTrue(PlanningRules.objects.get(site=self.site).require_approval)
        url = reverse("mes_core:what_if")
        self.client.post(url, {"site": self.site.pk, "action": "add_change", "scenario": 0, "kind": "festivo",
                               "date": (MON + timedelta(days=1)).isoformat()})
        r = self.client.post(url, {"site": self.site.pk, "action": "run"})
        self.assertContains(r, "Resultado por pedido")
        # liberar exige aprobación para planeación
        planning.generate_plan(self.site)
        r = self.client.post(reverse("mes_core:order_release", args=[o.pk]))
        o.refresh_from_db()
        self.assertNotEqual(o.status, WorkOrder.Status.RELEASED)
        self.client.post(reverse("mes_core:plan_versions"), {"action": "snapshot", "site": self.site.pk, "propose": "on"})
        v = PlanVersion.objects.get()
        self.client.post(reverse("mes_core:plan_versions"), {"action": "approve", "site": self.site.pk, "id": v.pk})
        v.refresh_from_db()
        self.assertEqual(v.status, PlanVersion.Status.PROPOSED)  # planeación no puede aprobar
        self.client.force_login(self.boss)
        self.client.post(reverse("mes_core:plan_versions"), {"action": "approve", "site": self.site.pk, "id": v.pk})
        v.refresh_from_db()
        o.refresh_from_db()
        self.assertEqual((v.status, o.status), (PlanVersion.Status.APPROVED, WorkOrder.Status.SUPPLIES))

    def test_promise_api_and_weekday_settings(self):
        admin = get_user_model().objects.create_superuser("adm", password="x")
        self.client.force_login(admin)
        r = self.client.post(reverse("mes_core:api_promise"), json.dumps({
            "site": self.site.pk, "process_type": "picking", "quantity": 50, "transit_days": 1,
            "delivery_date": (timezone.localdate() + timedelta(days=60)).isoformat()}), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["ok"])
        self.client.post(reverse("mes_core:shift_settings") + f"?site={self.site.pk}",
                         {"action": "weekdays", **{f"wd_{self.shift.pk}_{i}": "on" for i in range(6)}})
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.weekdays, "012345")
