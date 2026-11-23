"""Bolsa global de personal: reparto por bodega, quién está dónde y capacidad global."""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import capacity, workcal, workforce
from .models import (
    Attendance, CrewAssignment, Employee, Operation, Product, Shift, Site, StaffAllocation, StaffMove, WorkCenter,
    WorkOrder,
)
from .scheduling import OperatorLedger, operators_available, site_shifts


class WorkforceTests(TestCase):
    def setUp(self):
        workcal.invalidate()
        self.tz = timezone.get_current_timezone()
        today = timezone.localdate()
        self.mon = today + timedelta(days=(7 - today.weekday()) % 7 + 7)  # un lunes futuro
        self.b230 = Site.objects.create(code="B230", name="Bodega 230", operators_total=3)
        self.b232 = Site.objects.create(code="B232", name="Bodega 232")
        self.frios = Site.objects.create(code="FRI", name="Fríos")
        self.m230 = Shift.objects.create(site=self.b230, name="Mañana", start_time=time(6), end_time=time(14),
                                         weekdays="01234", operators=180)
        self.t230 = Shift.objects.create(site=self.b230, name="Tarde", start_time=time(14), end_time=time(22),
                                         weekdays="01234", operators=12)
        self.m232 = Shift.objects.create(site=self.b232, name="Mañana", start_time=time(6), end_time=time(14),
                                         weekdays="01234")
        self.pick = WorkCenter.objects.create(site=self.b232, code="PICK", name="Picking", kind="picking")
        self.vel = WorkCenter.objects.create(site=self.frios, code="VEL", name="Vélez", kind="velez")
        self.emps = [Employee.objects.create(document=str(1000 + i), full_name=f"persona {i}", site=self.b232)
                     for i in range(10)]
        User = get_user_model()
        self.planner = User.objects.create_user("plan", password="x")
        self.planner.groups.add(Group.objects.get(name="Planeación"))
        self.sup = User.objects.create_user("sup", password="x")
        self.sup.groups.add(Group.objects.get(name="Supervisor"))

    def at(self, d, h, m=0):
        return datetime.combine(self.mon + timedelta(days=d), time(h, m), tzinfo=self.tz)

    # ------------------------------------------------------------ reparto
    def test_allocation_precedence_and_fallback(self):
        cal = workcal.get_calendar(self.b230.pk)
        self.assertEqual(cal.people(self.m230, self.mon), 180)  # sin reparto: lo del turno
        StaffAllocation.objects.create(site=self.b230, headcount=150)  # base de toda la bodega
        cal = workcal.get_calendar(self.b230.pk)
        self.assertEqual((cal.people(self.m230, self.mon), cal.people(self.t230, self.mon)), (150, 150))
        StaffAllocation.objects.create(site=self.b230, shift=self.t230, headcount=20)  # base del turno gana
        StaffAllocation.objects.create(site=self.b230, date_from=self.mon, date_to=self.mon + timedelta(days=1),
                                       headcount=60)  # cambio por fecha gana sobre el base
        cal = workcal.get_calendar(self.b230.pk)
        self.assertEqual(cal.people(self.t230, self.mon - timedelta(days=1)), 20)
        self.assertEqual(cal.people(self.m230, self.mon), 60)
        self.assertEqual(cal.people(self.t230, self.mon + timedelta(days=1)), 60)
        self.assertEqual(cal.people(self.m230, self.mon + timedelta(days=2)), 150)
        n, src, _ = workforce.shift_people(self.b230, self.m230, self.mon)
        self.assertEqual((n, src), (60, "cambio"))
        self.assertEqual(workforce.shift_people(self.b232, self.m232, self.mon)[:2], (0, ""))

    def test_allocation_reaches_planner_and_ledger(self):
        StaffAllocation.objects.create(site=self.b232, shift=self.m232, headcount=8)
        shifts = site_shifts(self.b232)
        self.assertEqual(operators_available(shifts, 0, self.mon), 8)
        ledger = OperatorLedger(self.b232, shifts)
        self.assertFalse(ledger.unlimited)  # solo con reparto ya hay límite
        self.assertEqual(ledger.available_at(self.at(0, 9)), 8)
        cap = capacity.compute(self.b232, self.m232, self.mon, [], {})
        self.assertEqual(cap.scheduled, 8)
        # un cambio por fecha se refleja ese día
        StaffAllocation.objects.create(site=self.b232, shift=self.m232, date_from=self.mon, date_to=self.mon,
                                       headcount=40)
        self.assertEqual(OperatorLedger(self.b232, site_shifts(self.b232)).available_at(self.at(0, 9)), 40)
        self.assertEqual(OperatorLedger(self.b232, site_shifts(self.b232)).available_at(self.at(1, 9)), 8)

    def test_day_split_and_base_save(self):
        workforce.save_base_split({(self.b230.pk, self.m230.pk): 5, (self.b230.pk, self.t230.pk): 2,
                                   (self.b232.pk, self.m232.pk): 2}, self.planner)
        Attendance.objects.create(employee=self.emps[0], day=self.mon, status=Attendance.Status.SICK)
        split = workforce.day_split(self.mon)
        self.assertEqual((split["pool"], split["allocated"], split["absent"], split["free"]), (10, 9, 1, 0))
        # vaciar un campo devuelve ese turno a su configuración
        workforce.save_base_split({(self.b230.pk, self.t230.pk): None})
        self.assertFalse(StaffAllocation.objects.filter(shift=self.t230).exists())
        with self.assertRaises(ValidationError):
            StaffAllocation(site=self.b232, shift=self.m230, headcount=1).full_clean()

    # ------------------------------------------------------------ quién está dónde
    def test_locate_precedence_and_temporary_moves(self):
        e = self.emps[1]
        self.assertEqual(workforce.locate(self.at(0, 8))[e.pk].site_id, self.b232.pk)  # habitual
        workforce.move([e.pk], self.b230, self.at(0, 6), user=self.planner)  # hasta nuevo aviso
        workforce.move([e.pk], self.frios, self.at(0, 10), self.at(0, 12), self.vel, "apoyo Vélez")  # unas horas
        loc = workforce.locate(self.at(0, 11))[e.pk]
        self.assertEqual((loc.site_id, loc.work_center_id, loc.source), (self.frios.pk, self.vel.pk, "traslado"))
        self.assertEqual(workforce.locate(self.at(0, 13))[e.pk].site_id, self.b230.pk)  # vuelve a donde estaba
        self.assertEqual(workforce.locate(self.at(3, 8))[e.pk].site_id, self.b230.pk)
        # otro traslado hasta nuevo aviso cierra el anterior
        workforce.move([e.pk], self.b232, self.at(2, 6))
        first = StaffMove.objects.filter(employee=e, site=self.b230).get()
        self.assertEqual(first.end, self.at(2, 6))
        self.assertEqual(workforce.locate(self.at(3, 8))[e.pk].site_id, self.b232.pk)
        # devolver termina el traslado vigente
        workforce.move([e.pk], self.frios, self.at(4, 6))
        workforce.send_back([e.pk], self.at(4, 9))
        self.assertEqual(workforce.locate(self.at(4, 10))[e.pk].site_id, self.b232.pk)
        with self.assertRaises(ValidationError):
            workforce.move([e.pk], self.b230, self.at(0, 6), work_center=self.vel)

    def test_crew_assignment_wins(self):
        e = self.emps[2]
        product = Product.objects.create(sku="X", name="X")
        order = WorkOrder.objects.create(site=self.b232, product=product, process_type="x", quantity=Decimal(10),
                                         status=WorkOrder.Status.PLANNED)
        op = Operation.objects.create(order=order, sequence=1, name="Picking", work_center=self.pick,
                                      work_center_kind="picking", planned_start=self.at(0, 6), planned_end=self.at(0, 10))
        CrewAssignment.objects.create(operation=op, employee=e)
        workforce.move([e.pk], self.frios, self.at(0, 6))
        loc = workforce.locate(self.at(0, 8))[e.pk]
        self.assertEqual((loc.site_id, loc.work_center_id, loc.source), (self.b232.pk, self.pick.pk, "cuadrilla"))
        self.assertEqual(workforce.locate(self.at(0, 11))[e.pk].site_id, self.frios.pk)

    def test_location_board(self):
        StaffAllocation.objects.create(site=self.b232, shift=self.m232, headcount=12)
        Employee.objects.create(document="9", full_name="sin bodega")
        Attendance.objects.create(employee=self.emps[0], day=self.mon, status=Attendance.Status.VACATION)
        workforce.move([self.emps[3].pk], self.b230, self.at(0, 6))
        b = workforce.location_board(self.at(0, 8))
        col = {c["site"].code: c for c in b["columns"]}
        self.assertEqual((col["B232"]["here"], col["B232"]["quota"], col["B232"]["gap"]), (8, 12, -4))
        self.assertEqual(col["B230"]["here"], 1)
        self.assertEqual((len(b["unplaced"]), b["n_absent"], b["pool"]), (1, 1, 11))

    # ------------------------------------------------------------ capacidad global y pantallas
    def test_global_capacity(self):
        StaffAllocation.objects.create(site=self.b230, shift=self.m230, headcount=4)
        StaffAllocation.objects.create(site=self.b230, shift=self.t230, headcount=2)
        StaffAllocation.objects.create(site=self.b232, shift=self.m232, headcount=3)
        g = workforce.global_capacity(self.mon, 7)
        mon = g["totals"][0]
        self.assertEqual((mon["pool"], mon["people"], mon["free_people"]), (10, 9, 1))
        self.assertGreater(mon["cap_h"], 0)
        self.assertEqual(g["totals"][5]["people"], 0)  # sábado no trabaja
        self.assertEqual([r["site"].code for r in g["rows"]], ["B230", "B232"])

    def test_views_and_permissions(self):
        self.client.force_login(self.planner)
        r = self.client.get(reverse("mes_core:global_capacity"))
        self.assertContains(r, "Capacidad global")
        r = self.client.post(reverse("mes_core:staff_split"), {
            "action": "base", f"b_{self.b230.pk}_{self.m230.pk}": "150", f"b_{self.b230.pk}_{self.t230.pk}": "",
            f"b_{self.b232.pk}_{self.m232.pk}": "60", f"b_{self.frios.pk}_0": "18"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(StaffAllocation.objects.get(site=self.frios).headcount, 18)
        r = self.client.post(reverse("mes_core:staff_split"), {
            "action": "add", "site": self.b232.pk, "shift": self.m232.pk, "date_from": self.mon.isoformat(),
            "headcount": "40", "note": "inventario"})
        self.assertTrue(StaffAllocation.objects.filter(date_from=self.mon, headcount=40).exists())
        self.assertContains(self.client.get(reverse("mes_core:staff_split")), "Reparto base")
        r = self.client.post(reverse("mes_core:staff_location"), {
            "action": "move", "emp": [self.emps[0].pk, self.emps[1].pk], "site": self.b230.pk,
            "start": self.at(0, 6).strftime("%Y-%m-%dT%H:%M"), "until": "nuevo", "en": self.at(0, 8).strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(StaffMove.objects.count(), 2)
        r = self.client.get(reverse("mes_core:staff_location"), {"en": self.at(0, 8).strftime("%Y-%m-%dT%H:%M")})
        self.assertContains(r, "¿Quién está dónde?")
        self.assertContains(r, self.emps[0].display_name)
        # El supervisor ve el reparto y mueve gente, pero no cambia el reparto
        self.client.force_login(self.sup)
        self.assertEqual(self.client.get(reverse("mes_core:staff_location")).status_code, 200)
        self.client.post(reverse("mes_core:staff_split"), {"action": "add", "site": self.b232.pk,
                                                            "date_from": self.mon.isoformat(), "headcount": "1"})
        self.assertFalse(StaffAllocation.objects.filter(headcount=1).exists())
        ventas = get_user_model().objects.create_user("ventas", password="x")
        ventas.groups.add(Group.objects.get(name="Ventas"))
        self.client.force_login(ventas)
        self.assertEqual(self.client.get(reverse("mes_core:global_capacity")).status_code, 403)


class CompanyKpiTests(TestCase):
    def setUp(self):
        workcal.invalidate()
        self.tz = timezone.get_current_timezone()
        today = timezone.localdate()
        self.mon = today + timedelta(days=(7 - today.weekday()) % 7 + 7)
        self.site = Site.objects.create(code="B230", name="Bodega 230")
        # 06:00–15:00 con 35 min de descansos = 8 h 25 min hábiles
        self.shift = Shift.objects.create(site=self.site, name="Mañana", start_time=time(6), end_time=time(15),
                                          weekdays="01234", operators=6)
        self.emps = [Employee.objects.create(document=str(5000 + i), full_name=f"op {i}") for i in range(10)]

    def test_company_kpis(self):
        Attendance.objects.create(employee=self.emps[0], day=self.mon, status=Attendance.Status.PERMIT)
        k = workforce.company_kpis(self.mon, {"unscheduled_hh": 40, "done_30": 4, "on_time_n": 3})
        jornada = self.shift.effective_minutes / 60
        self.assertAlmostEqual(k["jornada"], jornada)
        self.assertEqual((k["pool"], k["absent"], k["present"], k["allocated"], k["free"]), (10, 1, 9, 6, 3))
        self.assertAlmostEqual(k["potential_h"], 9 * jornada)
        self.assertAlmostEqual(k["free_h"], 3 * jornada)
        self.assertEqual(k["absent_pct"], 10.0)
        self.assertEqual(k["on_time_pct"], 75)
        self.assertAlmostEqual(k["backlog_days"], round(40 / (9 * jornada), 1))

    def test_dashboard_all_sites_shows_company(self):
        user = get_user_model().objects.create_user("plan", password="x")
        user.groups.add(Group.objects.get(name="Planeación"))
        self.client.force_login(user)
        r = self.client.get(reverse("mes_core:dashboard"), {"site": "todas"})
        self.assertContains(r, "Libres para repartir")
        self.assertContains(r, "Capacidad disponible")
        self.assertContains(r, "Desempeño de la empresa")
