"""Indicadores: OEE, OLE, Pareto de paros, pausas excedidas, OTIF, adherencia, puntualidad y horas extra.

Escenario (lunes de la semana pasada, turno 06:00–14:00 sin descansos, 480 min):
  * Línea L1, operación de 06:00 a 14:00 con 2 personas, estándar 1 min/unidad.
  * Paro de línea de 10:00 a 11:00 (falta de material) → produciendo 420 min → Disponibilidad 87,5 %.
  * 700 buenas + 20 rechazos (+ 30 reprocesos marcados) → 720 min estándar ÷ (420 × 2) = Rendimiento 85,7 %;
    Calidad 700 ÷ 750 = 93,3 %. OEE = 0,875 × 0,857 × 0,933 = 70 %.
  * Reparto de 3 personas → 1 440 min-persona planeados; productivos 840 − 30 (paro individual) − 20 (almuerzo de
    más) = 790 → OLE = 0,549 × 0,911 × 0,933 = 47 %.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import indicators as ind
from . import workcal
from .models import (
    Employee, Operation, OrderClosure, ProductionReport, Product, Shift, Site, StaffAllocation, WorkCenter, WorkOrder,
)


class IndicatorTests(TestCase):
    def setUp(self):
        workcal.invalidate()
        self.tz = timezone.get_current_timezone()
        today = timezone.localdate()
        self.mon = today - timedelta(days=today.weekday() + 7)  # lunes de la semana pasada
        self.site = Site.objects.create(code="B230", name="Bodega 230")
        self.shift = Shift.objects.create(site=self.site, name="Mañana", start_time=time(6), end_time=time(14),
                                          weekdays="01234")
        StaffAllocation.objects.create(site=self.site, shift=self.shift, headcount=3)
        self.line = WorkCenter.objects.create(site=self.site, code="L1", name="Línea 1", kind="acond")
        self.product = Product.objects.create(sku="CAM", name="Camisa")
        self.order = self._order(due=self.at(0, 15))
        self.op = Operation.objects.create(
            order=self.order, sequence=1, name="Acondicionar", work_center=self.line, work_center_kind="acond",
            crew=2, minutes_per_unit=Decimal("1"), quantity=Decimal("720"),
            planned_start=self.at(0, 6), planned_end=self.at(0, 14), actual_start=self.at(0, 6), actual_end=self.at(0, 14),
            status=Operation.Status.DONE)
        rep = ProductionReport.objects.create(operation=self.op, qty_good=Decimal("700"), qty_scrap=Decimal("20"))
        ProductionReport.objects.filter(pk=rep.pk).update(reported_at=self.at(0, 13))
        self.emps = [Employee.objects.create(document=str(100 + i), full_name=f"op {i}", site=self.site, shift=self.shift)
                     for i in range(4)]
        self.piso = apps.is_installed("plugins.piso")
        if self.piso:
            Stop = apps.get_model("piso", "Stop")
            Mark = apps.get_model("piso", "Mark")
            Stop.objects.create(work_center=self.line, cause="falta de material", start=self.at(0, 10), end=self.at(0, 11))
            Stop.objects.create(work_center=self.line, employee=self.emps[0], cause="Baño", start=self.at(0, 8),
                                end=self.at(0, 8, 30))
            Stop.objects.create(work_center=self.line, employee=self.emps[1], cause="Almuerzo", is_break=True,
                                start=self.at(0, 11, 30), planned_end=self.at(0, 12), end=self.at(0, 12, 20))
            Mark.objects.create(operation=self.op, work_center=self.line, kind="w", qty=Decimal("30"), at=self.at(0, 12))

    def at(self, d, h, m=0):
        return datetime.combine(self.mon + timedelta(days=d), time(h, m), tzinfo=self.tz)

    def _order(self, due, status=WorkOrder.Status.DONE):
        return WorkOrder.objects.create(site=self.site, product=self.product, process_type="x", quantity=Decimal("720"),
                                        status=status, due_date=due)

    def week(self):
        return ind._bounds(self.mon, self.mon + timedelta(days=7))

    def test_oee_line(self):
        if not self.piso:
            self.skipTest("control de piso no instalado")
        a, b = self.week()
        e = ind.line_effectiveness([self.line], a, b)[self.line.pk].as_dict()
        self.assertAlmostEqual(e["planned_h"], 8)
        self.assertAlmostEqual(e["run_h"], 7)
        self.assertEqual((e["availability"], e["performance"], e["quality"], e["value"]), (88, 86, 93, 70))
        self.assertEqual(e["level"], "warn")

    def test_ole_site_and_company(self):
        if not self.piso:
            self.skipTest("control de piso no instalado")
        a, b = ind._bounds(self.mon, self.mon + timedelta(days=1))  # solo el lunes
        eff = ind.effectiveness([self.site], a, b)
        ole = eff["ole"]
        self.assertAlmostEqual(ole["planned_h"], 24)          # 3 personas × 8 h
        # en la semana entera cuentan los 5 días del reparto: la gente sin tarea también es pérdida
        self.assertAlmostEqual(ind.effectiveness([self.site], *self.week())["ole"]["planned_h"], 120)
        self.assertAlmostEqual(ole["run_h"], 790 / 60)
        self.assertEqual((ole["availability"], ole["performance"], ole["value"]), (55, 91, 47))
        self.assertEqual(eff["oee"]["value"], 70)
        self.assertEqual(eff["lines"][0]["wc"], self.line)

    def test_stops_and_breaks(self):
        if not self.piso:
            self.skipTest("control de piso no instalado")
        a, b = self.week()
        p = ind.stops_pareto([self.site], a, b)
        self.assertEqual([(r["cause"], round(r["minutes"])) for r in p["rows"]], [("Falta de material", 60), ("Baño", 30)])
        self.assertEqual((p["rows"][0]["pct"], p["rows"][0]["cum_pct"], p["rows"][0]["vital"]), (67, 67, True))
        br = ind.exceeded_breaks([self.site], a, b)
        self.assertEqual((br["n"], br["over"], round(br["minutes"])), (1, 1, 20))
        self.assertEqual(br["by_line"][0]["name"], "L1")

    def test_setup_reliability_and_teep(self):
        """+ alistamiento de 13:00 a 13:20 (planeado 15 min): produciendo 400 min, A 83,3 %, P 90 %, Q 93,3 % → OEE 70 %.
        Turno de la semana 5 × 8 h = 40 h; calendario 168 h → carga 20 %, utilización 24 %, TEEP 70 % × 8/168 = 3 %."""
        if not self.piso:
            self.skipTest("control de piso no instalado")
        from plugins.piso.floor import STOP_CAUSES

        setup_cause = next(c for c in STOP_CAUSES if ind.is_setup(c))
        self.assertFalse(any(ind.is_setup(c) for c in ("Baño", "Falta de material", "Falla mecánica")))
        Stop = apps.get_model("piso", "Stop")
        Stop.objects.create(work_center=self.line, cause=setup_cause, start=self.at(0, 13), end=self.at(0, 13, 20))
        Operation.objects.filter(pk=self.op.pk).update(setup_minutes=Decimal("15"))
        e = ind.line_effectiveness([self.line], *self.week())[self.line.pk].as_dict()
        self.assertAlmostEqual(e["run_h"], 400 / 60)
        self.assertEqual(e["value"], 70)
        self.assertEqual((round(e["setup_min"]), e["n_setup"], e["setup_pct"]), (20, 1, 4.2))
        self.assertEqual((e["setup_plan_min"], e["n_setup_plan"], e["setup_plan_avg"]), (15, 1, 15))
        # el alistamiento no cuenta como falla: solo el paro por falta de material
        self.assertEqual((e["n_fail"], round(e["fail_min"]), round(e["mttr_min"])), (1, 60, 60))
        self.assertAlmostEqual(e["mtbf_h"], 400 / 60)
        self.assertAlmostEqual(e["shift_h"], 40)
        self.assertAlmostEqual(e["calendar_h"], 168)
        self.assertEqual((e["load"], e["utilization"], e["teep"]), (20, 24, 3))
        rd = ind.capacity_reading(e)
        self.assertEqual(rd["kind"], "slack")
        self.assertIn("32 h", rd["text"])
        # una línea sin trabajo no suma calendario al total
        WorkCenter.objects.create(site=self.site, code="L2", name="Línea 2", kind="acond")
        tot = ind.effectiveness([self.site], *self.week())["oee"]
        self.assertAlmostEqual(tot["calendar_h"], 168)
        self.assertEqual(tot["teep"], 3)
        self.assertEqual(ind.capacity_reading({**e, "load": 95, "value": 80})["kind"], "full")
        self.assertEqual(ind.capacity_reading({**e, "load": 95, "value": 50})["kind"], "warn")

    def test_otif_and_adherence(self):
        late = self._order(due=self.at(1, 8))
        Operation.objects.create(order=late, sequence=1, name="Acondicionar", work_center=self.line,
                                 work_center_kind="acond", planned_start=self.at(1, 6), planned_end=self.at(1, 10),
                                 actual_start=self.at(1, 6), actual_end=self.at(1, 12), status=Operation.Status.DONE)
        OrderClosure.objects.create(order=late, reason="Faltó insumo", justified_by="Ana", total=Decimal("15"))
        a, b = self.week()
        o = ind.otif([self.site], a, b)
        self.assertEqual((o["n"], o["otif"], o["on_time"], o["in_full"]), (2, 50, 50, 50))
        self.assertEqual(o["misses"][0]["order"], late)
        ad = ind.plan_adherence([self.site], a, b)
        self.assertEqual((ad["n"], ad["hit"], ad["pct"]), (2, 1, 50))

    def test_punctuality_and_overtime(self):
        if not self.piso:
            self.skipTest("control de piso no instalado")
        CheckIn = apps.get_model("piso", "CheckIn")
        CheckIn.objects.create(employee=self.emps[0], day=self.mon, at=self.at(0, 6, 3), work_center=self.line)
        CheckIn.objects.create(employee=self.emps[1], day=self.mon, at=self.at(0, 6, 20), work_center=self.line)
        a, b = self.week()
        pu = ind.punctuality([self.site], a, b)
        self.assertEqual((pu["n"], pu["late"], pu["pct"], round(pu["avg_late"])), (2, 1, 50, 20))
        Call = apps.get_model("piso", "OvertimeCall")
        Day = apps.get_model("piso", "OvertimeDay")
        Req = apps.get_model("piso", "OvertimeRequest")
        Ans = apps.get_model("piso", "OvertimeAnswer")
        call = Call.objects.create(site=self.site, title="Sábado", status="enviada", sent_at=self.at(1, 9))
        day = Day.objects.create(call=call, date=self.mon + timedelta(days=5), start=time(6), end=time(12), needed=3)
        answers = ["si", "si", "no", "pendiente"]
        for e, ans in zip(self.emps, answers):
            r = Req.objects.create(call=call, employee=e, answer=ans, reason="estudio" if ans == "no" else "")
            if ans == "si":
                Ans.objects.create(request=r, day=day, until=time(12))
        ot = ind.overtime_response([self.site], a, b)
        self.assertEqual((ot["calls"], ot["yes"], ot["no"], ot["pending"]), (1, 2, 1, 1))
        self.assertEqual((ot["response_pct"], ot["accept_pct"], ot["coverage_pct"]), (75, 67, 67))
        self.assertEqual(ot["reasons"], [("Estudio", 1)])

    def test_trend_and_pages(self):
        tr = ind.trend([self.site], self.mon, 8)
        self.assertEqual(len(tr), 8)
        self.assertEqual(tr[-1]["week"], self.mon)
        planner = get_user_model().objects.create_user("plan", password="x")
        planner.groups.add(Group.objects.get(name="Planeación"))
        self.client.force_login(planner)
        r = self.client.get(reverse("mes_core:indicators"), {"site": "todas", "semana": self.mon.isoformat()})
        self.assertContains(r, "OEE por línea")
        self.assertContains(r, "Pausas excedidas")
        self.assertContains(r, "Alistamiento y cambios")
        self.assertContains(r, "MTBF / MTTR por línea")
        self.assertContains(r, "TEEP: ¿horas extra u otro turno?")
        if self.piso:
            self.assertContains(r, "70 %")
            self.assertContains(r, "Alistamiento (esperando patinador)")  # cómo registrarlo en la tablet
        r = self.client.get(reverse("mes_core:dashboard"), {"site": "todas"})
        self.assertContains(r, "Eficiencia y servicio")
        ventas = get_user_model().objects.create_user("ventas", password="x")
        ventas.groups.add(Group.objects.get(name="Ventas"))
        self.client.force_login(ventas)
        self.assertEqual(self.client.get(reverse("mes_core:indicators")).status_code, 403)


class PeriodTests(TestCase):
    """Calendario de «Indicadores»: atajos, rango a mano, histórico y en vivo."""

    T = date(2026, 11, 18)  # miércoles

    def test_presets_and_ranges(self):
        p = lambda **q: ind.parse_period(q, self.T)[0]  # noqa: E731
        self.assertEqual((p().start, p().end, p().preset), (date(2026, 11, 16), date(2026, 11, 22), "semana"))
        self.assertEqual((p(periodo="hoy").start, p(periodo="hoy").days), (self.T, 1))
        self.assertEqual((p(periodo="mes-pasado").start, p(periodo="mes-pasado").end), (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual(p(periodo="30d").days, 30)
        r = p(desde="2026-11-12", hasta="2026-11-03")  # al revés: se ordena
        self.assertEqual((r.start, r.end, r.preset), (date(2026, 11, 3), date(2026, 11, 12), ""))
        self.assertEqual(p(desde="2026-11-09", hasta="2026-11-15").preset, "semana-pasada")  # coincide con un atajo
        self.assertEqual(p(semana="2026-11-04").start, date(2026, 11, 2))  # compatibilidad con ?semana=
        per, warn = ind.parse_period({"desde": "2026-12-01"}, self.T)
        self.assertEqual((per.start, per.end), (self.T, self.T))
        self.assertTrue(warn)
        per, warn = ind.parse_period({"desde": "2020-01-01", "hasta": "2026-11-18"}, self.T)
        self.assertEqual(per.days, ind.MAX_DAYS)
        self.assertTrue(warn)
        self.assertEqual(p(desde="nada").preset, "semana")

    def test_previous_and_buckets(self):
        oct_ = ind.Period(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual((oct_.previous().start, oct_.previous().end), (date(2026, 9, 1), date(2026, 9, 30)))
        rng = ind.Period(date(2026, 11, 3), date(2026, 11, 12))
        self.assertEqual((rng.previous().start, rng.previous().end), (date(2026, 10, 24), date(2026, 11, 2)))
        day = ind.buckets(ind.Period(self.T, self.T), self.T)
        self.assertEqual((len(day), day[-1][0], day[0][0]), (8, self.T, self.T - timedelta(days=7)))
        wk = ind.buckets(ind.Period(date(2026, 11, 16), date(2026, 11, 22)), self.T)
        self.assertEqual((len(wk), wk[-1][0], wk[0][0]), (8, date(2026, 11, 16), date(2026, 9, 28)))
        self.assertEqual(len(ind.buckets(rng, self.T)), 10)  # día a día
        weekly = ind.buckets(ind.Period(date(2026, 9, 1), date(2026, 11, 18)), self.T)
        self.assertEqual((weekly[0], weekly[1][0]), ((date(2026, 9, 1), date(2026, 9, 6), "sem. 01/09"), date(2026, 9, 7)))
        monthly = ind.buckets(ind.Period(date(2026, 1, 1), date(2026, 11, 18)), self.T)
        self.assertEqual((len(monthly), monthly[-1]), (11, (date(2026, 11, 1), date(2026, 11, 18), "nov 26")))
        # el mes en curso no pasa de hoy
        self.assertEqual(ind.buckets(ind.preset_period("mes", self.T), self.T)[-1][0], self.T)

    def test_page_live_and_history(self):
        site = Site.objects.create(code="B1", name="Bodega 1")
        planner = get_user_model().objects.create_user("plan", password="x")
        planner.groups.add(Group.objects.get(name="Planeación"))
        self.client.force_login(planner)
        url = reverse("mes_core:indicators")
        r = self.client.get(url, {"site": site.pk, "periodo": "hoy"})
        self.assertContains(r, "data-live")
        self.assertContains(r, "últimos 8 días")
        today = timezone.localdate()
        r = self.client.get(url, {"site": "todas", "desde": (today - timedelta(days=40)).isoformat(),
                                  "hasta": (today - timedelta(days=20)).isoformat()})
        self.assertNotContains(r, "data-live")
        self.assertContains(r, "Histórico · 21 días")
        self.assertContains(r, "día a día en el periodo")
        self.assertContains(r, 'name="desde"')
