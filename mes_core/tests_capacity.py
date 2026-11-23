"""Pruebas del tablero principal de capacidad de planta."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from .capacity import dashboard_data
from .models import Operation, Product, ScheduleChange, Shift, ShiftAttendance, Site, WorkCenter, WorkOrder

TZ = ZoneInfo("America/Bogota")
DAY = date(2026, 11, 17)
NOW = datetime(2026, 11, 17, 7, 0, tzinfo=TZ)


def at(h, m=0, day=DAY):
    return datetime.combine(day, time(h, m), tzinfo=TZ)


class CapacityTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="CEDI", name="Centro", operators_total=100)
        # 06:00–14:00 con 15 + 30 min de descanso → 7,25 h hábiles
        self.morning = Shift.objects.create(site=self.site, name="Mañana", start_time=time(6), end_time=time(14),
                                            breakfast_start=time(9), breakfast_end=time(9, 15),
                                            lunch_start=time(12), lunch_end=time(12, 30))
        self.afternoon = Shift.objects.create(site=self.site, name="Tarde", start_time=time(14), end_time=time(22),
                                              operators=40)
        self.wc = WorkCenter.objects.create(site=self.site, code="ACO", name="Acondicionado", kind="acondicionado", capacity=4)
        self.product = Product.objects.create(sku="CAM", name="Camiseta")

    def _order(self, **kw):
        kw.setdefault("status", WorkOrder.Status.PLANNED)
        return WorkOrder.objects.create(site=self.site, product=self.product, process_type="acondicionado",
                                        quantity=Decimal("1000"), **kw)

    def _op(self, order, start, end, crew, seq=1):
        return Operation.objects.create(order=order, sequence=seq, name="Embolsado", work_center_kind="acondicionado",
                                        work_center=self.wc, planned_start=start, planned_end=end, crew=crew)

    def test_people_capacity_and_overload(self):
        ShiftAttendance.objects.create(shift=self.morning, day=DAY, absent=15)
        a = self._order(goods_pct=100, supplies_pct=100)
        b = self._order(goods_pct=50, supplies_pct=0)
        self._op(a, at(6), at(14), crew=60)  # 60 × 7,25 = 435 HH
        self._op(b, at(6), at(14), crew=40)  # 40 × 7,25 = 290 HH
        d = dashboard_data(self.site, now=NOW)
        sel = d["sel"]
        self.assertEqual((d["shift"], d["day"]), (self.morning, DAY))
        self.assertEqual((sel.scheduled, sel.absent, sel.available), (100, 15, 85))
        self.assertAlmostEqual(sel.capacity_h, 85 * 7.25)
        self.assertAlmostEqual(sel.required_h, 725)
        self.assertEqual(sel.occ_pct, 118)
        self.assertEqual(sel.level, "over")
        self.assertAlmostEqual(-sel.balance_h, 108.75)
        # Recursos ponderados por horas: (435×100 + 290×50) / 725 = 80 %
        self.assertEqual(d["goods_pct"], 80)
        self.assertEqual(d["supplies_pct"], 60)
        self.assertEqual(d["resource_rows"][0]["order"], b)  # primero lo que falta
        # Sugerencias: operarios que faltan, cubrir ausencias y mover a la Tarde (40 × 8 h libres)
        text = " ".join(d["suggestions"])
        self.assertIn("equivale a 15 operarios", text)
        self.assertIn("Cubrir 15 de las 15 ausencias", text)
        self.assertIn("Mover carga a Tarde mar 17/11", text)
        # Carga por línea: 14,5 h de trabajo en 4 estaciones × 7,25 h = 50 %
        line = d["lines"][0]
        self.assertEqual(line["occ_pct"], 50)
        self.assertAlmostEqual(line["hh"], 725)

    def test_week_grid_and_shift_operators(self):
        self._op(self._order(), at(14), at(18), crew=10)
        d = dashboard_data(self.site, day=DAY, shift=self.afternoon, now=NOW)
        sel = d["sel"]
        self.assertEqual(sel.scheduled, 40)  # número propio del turno
        self.assertAlmostEqual(sel.required_h, 40)
        self.assertEqual(sel.occ_pct, 12)  # 40 / (40 × 8)
        self.assertEqual(len(d["grid"]), 2)
        self.assertEqual(len(d["grid"][0]["cells"]), 7)
        self.assertEqual(d["grid"][1]["cells"][0].required_h, 40)

    def test_orders_block(self):
        self._order(status=WorkOrder.Status.TO_PLAN)
        self._order(due_date=NOW - timedelta(hours=1))
        self._order(due_date=NOW + timedelta(days=1), planned_end=NOW + timedelta(days=2))
        d = dashboard_data(self.site, now=NOW)
        self.assertEqual((d["to_plan"], d["late"], d["at_risk"]), (1, 1, 1))

    def test_unlimited_when_no_operators(self):
        self.site.operators_total = 0
        self.site.save()
        sel = dashboard_data(self.site, now=NOW)["sel"]
        self.assertTrue(sel.unlimited)
        self.assertIsNone(sel.occ_pct)


class CapacityViewTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="CEDI", name="Centro", operators_total=100)
        self.shift = Shift.objects.create(site=self.site, name="Mañana", start_time=time(6), end_time=time(14))
        self.order = WorkOrder.objects.create(site=self.site, product=Product.objects.create(sku="X", name="X"),
                                              process_type="acondicionado", quantity=10)
        User = get_user_model()
        self.planner = User.objects.create_user("plan", password="x")
        self.planner.groups.add(Group.objects.get(name="Planeación"))
        self.viewer = User.objects.create_user("ver", password="x")

    def test_dashboard_renders(self):
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("mes_core:dashboard"), {"site": self.site.pk, "day": "2026-11-17", "shift": self.shift.pk})
        self.assertContains(r, "Personal disponible")
        self.assertContains(r, "Capacidad ocupada")
        self.assertNotContains(r, "Registrar ausencias")  # sin permiso de planeación

    def test_planner_saves_attendance_and_resources(self):
        self.client.force_login(self.planner)
        r = self.client.post(reverse("mes_core:save_attendance"),
                             {"shift": self.shift.pk, "day": "2026-11-17", "absent": "15", "scheduled": "", "notes": "2 vacaciones"})
        self.assertEqual(r.status_code, 302)
        att = ShiftAttendance.objects.get(shift=self.shift, day=DAY)
        self.assertEqual((att.absent, att.scheduled, att.notes), (15, None, "2 vacaciones"))
        # más ausencias que programados → rechazado
        self.client.post(reverse("mes_core:save_attendance"), {"shift": self.shift.pk, "day": "2026-11-17", "absent": "150"})
        att.refresh_from_db()
        self.assertEqual(att.absent, 15)

        url = reverse("mes_core:save_resources", args=[self.order.pk])
        self.client.post(url, {"goods_pct": "92", "supplies_pct": "78"})
        self.order.refresh_from_db()
        # planeación maneja la mercancía; el % de insumos lo maneja el equipo de insumos
        self.assertEqual((self.order.goods_pct, self.order.supplies_pct), (92, 0))
        self.client.post(url, {"goods_pct": "120", "supplies_pct": "10"})  # fuera de rango
        self.order.refresh_from_db()
        self.assertEqual(self.order.goods_pct, 92)
        self.client.post(url, {"goods_pct": "0", "supplies_pct": "0", "all": "1"})
        self.order.refresh_from_db()
        self.assertEqual((self.order.goods_pct, self.order.supplies_pct), (100, 0))
        self.assertEqual(ScheduleChange.objects.filter(site=self.site).count(), 3)
        boss = get_user_model().objects.create_user("jefe", password="x")
        boss.groups.add(Group.objects.get(name="Jefe de planta"))  # tiene el permiso de insumos
        self.client.force_login(boss)
        self.client.post(url, {"goods_pct": "100", "supplies_pct": "78"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.supplies_pct, 78)

    def test_viewer_cannot_change(self):
        self.client.force_login(self.viewer)
        self.client.post(reverse("mes_core:save_attendance"), {"shift": self.shift.pk, "day": "2026-11-17", "absent": "5"})
        self.client.post(reverse("mes_core:save_resources", args=[self.order.pk]), {"goods_pct": "50", "supplies_pct": "50"})
        self.assertFalse(ShiftAttendance.objects.exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.goods_pct, 0)
