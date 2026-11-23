"""Turnos: desayunos/almuerzos escalonados por grupos y turnos de traslape (se cruzan con otros)."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import scheduling, workcal
from .capacity import compute_many
from .models import Operation, Product, Shift, Site, WorkCenter, WorkOrder
from .shift_config import OverlapSpec, apply_shift_config, build_shift_specs, save_overlap_shifts

T = lambda h, m=0: time(h, m)  # noqa: E731


class BreakGroupsTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="P", name="Planta", operators_total=100)

    def test_staggered_breakfast_counts_one_group_per_person(self):
        sh = Shift(site=self.site, name="Mañana", start_time=T(6), end_time=T(14))
        sh.set_break_groups("breakfast", [(T(8, 40), T(9)), (T(8), T(8, 20)), (T(8, 20), T(8, 40))])
        sh.set_break_groups("lunch", [(T(12), T(12, 30))])
        sh.full_clean()
        sh.save()
        sh.refresh_from_db()
        self.assertEqual(sh.breakfast_slots, [["08:00", "08:20"], ["08:20", "08:40"], ["08:40", "09:00"]])
        self.assertEqual((sh.breakfast_start, sh.breakfast_end), (T(8), T(8, 20)))  # equivale a un grupo
        self.assertEqual(sh.lunch_slots, [])  # un solo horario se guarda en los campos simples
        self.assertEqual(sh.effective_minutes, 480 - 20 - 30)
        self.assertEqual(sh.breakfast_label, "3 grupos: 08:00, 08:20, 08:40 · 20 min")
        self.assertEqual(len(sh.breakfast_groups), 3)

    def test_group_outside_shift_is_rejected(self):
        sh = Shift(site=self.site, name="M", start_time=T(6), end_time=T(14))
        sh.set_break_groups("breakfast", [(T(8), T(8, 20)), (T(14, 10), T(14, 30))])
        with self.assertRaisesMessage(ValidationError, "debe quedar dentro del turno"):
            sh.clean()

    def test_specs_replicate_groups_in_every_shift(self):
        specs = build_shift_specs(first_start=T(6), duration_minutes=480, count=3,
                                  breakfast=[(T(8), T(8, 20)), (T(8, 20), T(8, 40))], lunch=(T(12), T(12, 30)),
                                  operators=[0])
        self.assertEqual(specs[1].breakfast_groups, [(T(16), T(16, 20)), (T(16, 20), T(16, 40))])
        self.assertEqual(specs[2].breakfast_groups[0], (T(0), T(0, 20)))
        self.assertEqual(specs[0].effective_minutes, 430)
        with self.assertRaisesMessage(ValidationError, "se cruzan"):
            build_shift_specs(first_start=T(6), duration_minutes=480, count=1,
                              breakfast=[(T(8), T(8, 30)), (T(12), T(12, 20))], lunch=(T(12), T(12, 30)), operators=[0])

    def test_settings_page_saves_several_breakfast_rows(self):
        user = get_user_model().objects.create_user("p", password="x")
        user.groups.add(Group.objects.get(name="Planeación"))
        self.client.force_login(user)
        url = f"{reverse('mes_core:shift_settings')}?site={self.site.pk}"
        r = self.client.post(url, {
            "first_start": "06:00", "duration_hours": "8", "end_time": "14:00", "count": "2", "operators_total": "100",
            "breakfast_start": ["08:00", "08:20", "08:40", ""], "breakfast_end": ["08:20", "08:40", "09:00", ""],
            "lunch_start": ["12:00"], "lunch_end": ["12:30"]})
        self.assertEqual(r.status_code, 302)
        tarde = self.site.shifts.get(name="Tarde")
        self.assertEqual([f"{a:%H:%M}" for a, _ in tarde.breakfast_groups], ["16:00", "16:20", "16:40"])
        page = self.client.get(url)
        self.assertContains(page, 'value="08:40"')  # vuelve a pintar los tres grupos
        r = self.client.post(url, {"first_start": "06:00", "duration_hours": "8", "count": "1", "operators_total": "100",
                                   "breakfast_start": ["08:00"], "breakfast_end": [""]})
        self.assertContains(r, "Indica inicio y fin de cada horario de desayuno")


class OverlapShiftTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="P", name="Planta", operators_total=0)
        specs = build_shift_specs(first_start=T(6), duration_minutes=480, count=2, breakfast=None,
                                  lunch=(T(12), T(12, 30)), operators=[30, 25])
        apply_shift_config(self.site, specs)
        self.manana = self.site.shifts.get(name="Mañana")
        save_overlap_shifts(self.site, [OverlapSpec(pk=None, name="Traslape", start=T(11), end=T(19), operators=10,
                                                    breakfast_groups=[], lunch_groups=[(T(15), T(15, 30))])])
        self.traslape = self.site.shifts.get(name="Traslape")

    def test_overlap_has_no_breakfast_and_survives_base_config(self):
        self.assertTrue(self.traslape.is_overlap)
        self.assertEqual(self.traslape.breakfast_groups, [])
        self.assertEqual(self.traslape.effective_minutes, 480 - 30)
        specs = build_shift_specs(first_start=T(6), duration_minutes=480, count=3, breakfast=None, lunch=None, operators=[0])
        apply_shift_config(self.site, specs)
        self.traslape.refresh_from_db()
        self.assertTrue(self.traslape.active)
        self.assertEqual(self.site.shifts.filter(active=True).count(), 4)
        with self.assertRaisesMessage(ValidationError, "Ya existe un turno llamado"):
            save_overlap_shifts(self.site, [OverlapSpec(pk=None, name="mañana", start=T(10), end=T(18), operators=0,
                                                        breakfast_groups=[], lunch_groups=[])])
        save_overlap_shifts(self.site, [OverlapSpec(pk=self.traslape.pk, name="", start=None, end=None, operators=0,
                                                    breakfast_groups=[], lunch_groups=[], delete=True)])
        self.traslape.refresh_from_db()
        self.assertFalse(self.traslape.active)

    def test_people_add_up_while_shifts_overlap(self):
        cal = workcal.get_calendar(self.site.pk)
        shifts = scheduling.site_shifts(self.site)
        tz = timezone.get_current_timezone()
        day = date(2026, 3, 3)
        at = lambda h: datetime.combine(day, T(h), tzinfo=tz)  # noqa: E731
        self.assertEqual(cal.slot_at(shifts, at(9)).operators, 30)
        self.assertEqual(cal.slot_at(shifts, at(12)).operators, 40)   # Mañana 30 + Traslape 10
        self.assertEqual(cal.slot_at(shifts, at(16)).operators, 35)   # Tarde 25 + Traslape 10
        self.assertEqual(cal.slot_at(shifts, at(12)).label, "Mañana + Traslape")

    def test_daily_hours_are_the_union(self):
        shifts = scheduling.site_shifts(self.site)
        # 06–22 = 16 h. El traslape cubre el almuerzo de la Mañana (12:00) y la Tarde cubre el del traslape (15:00);
        # el de la Tarde (20:00) queda sin cubrir → 15,5 h. Sin traslape se pierden los dos almuerzos base → 15 h.
        self.assertAlmostEqual(scheduling.daily_work_hours(shifts), 15.5)
        self.assertAlmostEqual(scheduling.daily_work_hours(scheduling.base_shifts(shifts)), 15.0)

    def test_capacity_load_is_shared_not_double_counted(self):
        tz = timezone.get_current_timezone()
        day = date(2026, 3, 3)
        wc = WorkCenter.objects.create(site=self.site, code="L1", name="Línea", kind="x", capacity=1)
        order = WorkOrder.objects.create(site=self.site, product=Product.objects.create(sku="A", name="A"),
                                         process_type="x", quantity=Decimal("1"))
        op = Operation.objects.create(order=order, sequence=1, name="Op", work_center=wc, crew=8,
                                      planned_start=datetime.combine(day, T(9), tzinfo=tz),
                                      planned_end=datetime.combine(day, T(12), tzinfo=tz))
        shifts = scheduling.site_shifts(self.site)
        cells = compute_many(self.site, shifts, [day], [op], {})
        manana, traslape = cells[(self.manana.pk, day)], cells[(self.traslape.pk, day)]
        # 09–11 solo Mañana: 2 h × 8 = 16 HH. 11–12 ambos: 1 h × 8 = 8 HH repartidas 30:10 → 6 y 2
        self.assertAlmostEqual(manana.required_h, 22.0)
        self.assertAlmostEqual(traslape.required_h, 2.0)
        self.assertAlmostEqual(manana.required_h + traslape.required_h, 24.0)

    def test_settings_page_creates_and_edits_overlap(self):
        user = get_user_model().objects.create_user("p", password="x")
        user.groups.add(Group.objects.get(name="Planeación"))
        self.client.force_login(user)
        url = f"{reverse('mes_core:shift_settings')}?site={self.site.pk}"
        page = self.client.get(url)
        self.assertContains(page, "Turnos de traslape")
        self.assertContains(page, 'value="Traslape"')
        k = str(self.traslape.pk)
        r = self.client.post(url, {
            "action": "overlap", "ov_key": [k, "n1"],
            f"ov_{k}_name": "Traslape", f"ov_{k}_start": "11:00", f"ov_{k}_end": "19:00", f"ov_{k}_ops": "12",
            f"ov_{k}_breakfast_start": [], f"ov_{k}_lunch_start": ["15:00"], f"ov_{k}_lunch_end": ["15:30"],
            "ov_n1_name": "Apoyo noche", "ov_n1_start": "18:00", "ov_n1_end": "02:00", "ov_n1_ops": "",
            "ov_n1_breakfast_start": ["22:00", "22:20"], "ov_n1_breakfast_end": ["22:20", "22:40"]})
        self.assertEqual(r.status_code, 302)
        self.traslape.refresh_from_db()
        self.assertEqual(self.traslape.operators, 12)
        apoyo = self.site.shifts.get(name="Apoyo noche")
        self.assertTrue(apoyo.is_overlap)
        self.assertEqual(len(apoyo.breakfast_groups), 2)
        r = self.client.post(url, {"action": "overlap", "ov_key": ["n1"], "ov_n1_name": "Malo", "ov_n1_start": "11:00",
                                   "ov_n1_end": "19:00", "ov_n1_breakfast_start": ["20:00"], "ov_n1_breakfast_end": ["20:15"]})
        self.assertContains(r, "debe quedar dentro de")
        self.assertContains(r, 'value="Malo"')  # conserva lo escrito
