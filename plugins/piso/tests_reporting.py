"""Formas de reportar en planta: contador, cada operario por tramos y supernumerario (total repartido o por persona)."""

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from mes_core.models import CrewAssignment, Employee, Operation, PlanStage, ProductionReport, WorkCenter

from . import reporting
from .models import FloorAccess, IntervalReport, LineCode, Mark, ProcessReporting, Stop, SupernumeraryLine
from .tests import FloorBase


class ReportingBase(FloorBase):
    def setUp(self):
        super().setUp()
        self.order.quantity = Decimal("100")
        self.order.save(update_fields=["quantity"])
        self.op1.refresh_from_db()
        self.carla = Employee.objects.create(document="2001", full_name="carla diaz", shift=self.shift, supervisor=self.sup)
        acc = FloorAccess(employee=self.carla, role=FloorAccess.Role.SUPERNUMERARY)
        acc.set_pin("1234")
        acc.save()

    def line_mode(self, mode, wc=None, interval=60):
        lc = LineCode.for_line(wc or self.wc)
        lc.report_mode, lc.interval_minutes = mode, interval
        lc.save()


class SplitTests(ReportingBase):
    def test_split_is_proportional_and_never_loses_units(self):
        self.assertEqual(set(reporting.split_quantity(Decimal(2400), {i: 1 for i in range(10)}).values()), {240})
        s = reporting.split_quantity(Decimal(10), {1: 60, 2: 30})
        self.assertEqual((s[1], s[2]), (7, 3))
        s = reporting.split_quantity(Decimal(10), {1: 1, 2: 1, 3: 1})
        self.assertEqual(sum(s.values()), 10)
        self.assertEqual(reporting.split_quantity(Decimal(4), {1: 0, 2: 0}), {1: 2, 2: 2})  # nadie trabajó: iguales

    def test_worked_minutes_discount_own_and_line_stops_and_absences(self):
        from mes_core.models import Attendance

        end = timezone.now()
        start = end - timedelta(minutes=60)
        Stop.objects.create(employee=self.beto, cause="Baño", start=end - timedelta(minutes=20), end=end - timedelta(minutes=5))
        Stop.objects.create(work_center=self.wc, cause="Falla mecánica", start=end - timedelta(minutes=10),
                            end=end - timedelta(minutes=0))
        w = reporting.worked_minutes([self.ana, self.beto], self.wc, start, end)
        self.assertAlmostEqual(w[self.ana.pk], 50, places=0)
        self.assertAlmostEqual(w[self.beto.pk], 35, places=0)
        Attendance.objects.create(employee=self.beto, day=timezone.localdate(end), status=Attendance.Status.ABSENT)
        self.assertEqual(reporting.worked_minutes([self.beto], self.wc, start, end)[self.beto.pk], 0)

    def test_workload_is_shared_among_the_crew(self):
        self.assertEqual(reporting.workload_share(self.op1, 2), 50)  # 100 unidades entre Ana y Beto


class ModeTests(ReportingBase):
    def test_stage_beats_process_beats_line_beats_counter(self):
        self.assertEqual(reporting.mode_of(self.op1)["mode"], "contador")
        self.line_mode("supernumerario", interval=30)
        mi = reporting.mode_of(self.op1)
        self.assertEqual((mi["mode"], mi["interval"]), ("supernumerario", 30))
        ProcessReporting.objects.create(name="  armar ", report_mode="intervalo", interval_minutes=45)
        mi = reporting.mode_of(self.op1)
        self.assertEqual((mi["mode"], mi["interval"]), ("intervalo", 45))
        stage = PlanStage.objects.create(order=self.order, sequence=1, name="Armar", work_center=self.wc, quantity=100,
                                         std_value=1, report_mode="contador", split_mode="iguales")
        Operation.objects.filter(pk=self.op1.pk).update(stage=stage)
        self.op1.refresh_from_db()
        mi = reporting.mode_of(self.op1)
        self.assertEqual((mi["mode"], mi["split"]), ("contador", "iguales"))

    def test_period_runs_from_last_report_to_the_next_cut(self):
        now = timezone.now()
        p = reporting.period(self.op1, interval=60, work_center=self.wc, now=now)
        self.assertLessEqual(p["start"], now)  # la cuadrilla se armó hoy: el primer tramo arranca ahí
        self.assertGreater(p["due"], p["start"])
        self.assertLessEqual(p["due"] - p["start"], timedelta(minutes=65))
        shift_start = timezone.localtime(now).replace(hour=0, minute=1, second=0, microsecond=0)
        self.assertEqual((p["due"] - shift_start).total_seconds() % 3600, 0)  # cortes: inicio del turno + N horas
        # sin cuadrilla de hoy, el tramo arranca con el turno y el corte ya pasó: está atrasado
        CrewAssignment.objects.filter(operation=self.op1).update(assigned_at=now - timedelta(days=1))
        p = reporting.period(self.op1, interval=60, work_center=self.wc, now=now)
        self.assertEqual(p["start"], shift_start)


class OperatorIntervalTests(ReportingBase):
    def test_operator_reports_own_units_each_period(self):
        self.line_mode("intervalo")
        self.login()
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual(s["report"]["mode"], "intervalo")
        self.assertIn("due", s["report"]["period"])
        self.assertEqual(s["report"]["share"], 50)
        r = self.post("piso:api_report", {"op": self.op1.pk, "g": "12", "x": "1"})
        self.assertEqual(r.status_code, 200, r.content)
        self.op1.refresh_from_db()
        self.assertEqual((self.op1.qty_good, self.op1.qty_scrap), (12, 1))
        rep = ProductionReport.objects.get(operation=self.op1)
        self.assertEqual((rep.employee, rep.source), (self.ana, "piso"))
        m = Mark.objects.get(operation=self.op1, kind=Mark.Kind.GOOD)
        self.assertEqual((m.employee, m.qty), (self.ana, 12))
        self.assertIsNotNone(m.since)
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual(s["mine"]["task_good"], 12)
        self.assertEqual(len(s["report"]["reports"]), 1)
        # el siguiente tramo arranca donde terminó este
        self.assertEqual(s["report"]["period"]["start"][:16], s["report"]["reports"][0]["end"][:16])

    def test_counter_lines_do_not_take_period_reports_from_operators(self):
        self.login()
        self.assertEqual(self.post("piso:api_report", {"op": self.op1.pk, "g": "1"}).status_code, 409)

    def test_cannot_report_more_than_pending(self):
        self.line_mode("intervalo")
        self.login()
        r = self.post("piso:api_report", {"op": self.op1.pk, "g": "500"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("Solo faltan", r.json()["e"])


class SupernumeraryTests(ReportingBase):
    def setUp(self):
        super().setUp()
        self.line_mode("supernumerario")
        self.wc2 = WorkCenter.objects.create(site=self.site, code="L2", name="Banda", kind="empaque")
        self.op3 = Operation.objects.create(order=self.order, sequence=3, name="Limpieza", work_center=self.wc2, crew=1,
                                            minutes_per_unit=Decimal("0.2"), supervisor=self.other_sup,
                                            planned_start=timezone.now(), planned_end=timezone.now() + timedelta(hours=8))
        CrewAssignment.objects.create(operation=self.op3, employee=self.beto)

    def test_sees_lines_of_their_supervisor_and_reports_the_line_total_split_evenly(self):
        self.login("2001")
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual(s["kind"], "supernumerario")
        self.assertEqual([ln["code"] for ln in s["lines"]], ["L1"])  # L2 es de otro supervisor
        self.assertIn("supervisor", s["lines"][0]["tags"])
        self.assertIn(f"l{self.wc.pk}", s["pulse"])
        op = next(o for o in s["lines"][0]["ops"] if o["id"] == self.op1.pk)
        self.assertEqual(len(op["crew_list"]), 2)
        PlanStage.objects.create(order=self.order, sequence=1, name="Armar", work_center=self.wc, quantity=100,
                                 std_value=1, split_mode="iguales")
        Operation.objects.filter(pk=self.op1.pk).update(stage=PlanStage.objects.get(name="Armar"))
        r = self.post("piso:api_report", {"op": self.op1.pk, "g": "10"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["people"], 2)
        self.op1.refresh_from_db()
        self.assertEqual(self.op1.qty_good, 10)
        rep = ProductionReport.objects.get(operation=self.op1)
        self.assertIsNone(rep.employee)  # un solo reporte en el pedido, repartido por dentro
        by = dict(Mark.objects.filter(operation=self.op1).values_list("employee__document", "qty"))
        self.assertEqual(by, {"1001": 5, "1002": 5})
        self.assertEqual(IntervalReport.objects.get().employee, self.carla)

    def test_reports_per_person(self):
        self.login("2001")
        r = self.post("piso:api_report", {"op": self.op1.pk, "per": [{"id": self.ana.pk, "g": 4},
                                                                      {"id": self.beto.pk, "g": "2", "x": 1}]})
        self.assertEqual(r.status_code, 200, r.content)
        self.op1.refresh_from_db()
        self.assertEqual((self.op1.qty_good, self.op1.qty_scrap), (6, 1))
        self.assertEqual(Mark.objects.get(employee=self.ana, operation=self.op1).qty, 4)
        r = self.post("piso:api_report", {"op": self.op1.pk, "per": [{"id": self.carla.pk, "g": 4}]})
        self.assertEqual(r.status_code, 409)  # no está en la cuadrilla

    def test_lines_chosen_on_the_tablet_or_assigned_in_the_mes(self):
        self.login("2001")
        self.assertEqual(self.post("piso:api_report", {"op": self.op3.pk, "g": "1"}).status_code, 403)
        self.assertEqual(self.post("piso:api_sup_lines", {"wc": self.wc2.pk, "on": True}).status_code, 200)
        self.assertEqual(self.post("piso:api_report", {"op": self.op3.pk, "g": "1"}).status_code, 200)
        self.post("piso:api_sup_lines", {"wc": self.wc2.pk, "on": False})
        self.assertFalse(SupernumeraryLine.objects.exists())
        SupernumeraryLine.objects.create(employee=self.carla, work_center=self.wc2)
        self.assertEqual(self.post("piso:api_report", {"op": self.op3.pk, "g": "1"}).status_code, 200)
        # el supernumerario reporta aunque la línea sea de contador (siempre puede escribir a mano)
        self.assertEqual(reporting.mode_of(self.op3)["mode"], "contador")

    def test_undo_and_deleting_the_report_in_the_mes(self):
        self.login("2001")
        ir = self.post("piso:api_report", {"op": self.op1.pk, "g": "8"}).json()["id"]
        self.assertEqual(self.post("piso:api_report_undo", {"id": ir}).status_code, 200)
        self.op1.refresh_from_db()
        self.assertEqual(self.op1.qty_good, 0)
        self.assertFalse(Mark.objects.exists() or IntervalReport.objects.exists())
        self.post("piso:api_report", {"op": self.op1.pk, "g": "8"})
        ProductionReport.objects.get(operation=self.op1).delete()
        self.assertFalse(Mark.objects.exists() or IntervalReport.objects.exists())

    def test_operators_of_a_supernumerary_line_see_their_share_and_no_counter(self):
        self.login()
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual(s["report"]["mode"], "supernumerario")
        self.assertEqual(s["report"]["share"], 50)
        self.assertIn("due", s["report"]["period"])

    def test_line_tablet_reports_by_period(self):
        lc = LineCode.for_line(self.wc)
        self.post("piso:api_pair", {"code": lc.pair_code})
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual(s["panel"]["ops"][0]["mode"], "supernumerario")
        r = self.post("piso:api_report", {"op": self.op1.pk, "g": "6"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(IntervalReport.objects.get().employee)
        self.assertEqual(self.post("piso:api_report", {"op": self.op3.pk, "g": "1"}).status_code, 403)


class SupernumeraryStaffTests(ReportingBase):
    """Pestaña «Personal»: asistencia, indicadores, paros y mensajes a su personal."""

    def setUp(self):
        super().setUp()
        self.outsider = Employee.objects.create(document="3001", full_name="otro operario", shift=self.shift)
        self.login("2001")

    def test_sees_attendance_and_indicators_of_their_people(self):
        from mes_core.models import Attendance

        Attendance.objects.create(employee=self.ana, day=timezone.localdate(), status=Attendance.Status.PRESENT,
                                  confirmed_by=self.sup)
        Mark.objects.create(operation=self.op1, employee=self.ana, work_center=self.wc, qty=3,
                            minutes_per_unit=Decimal("1"), at=timezone.now() - timedelta(minutes=5))
        s = self.client.get(reverse("piso:api_state")).json()
        cards = {c["name"]: c for c in s["people"]["cards"]}
        self.assertEqual(cards["Ana Gomez"]["att"]["label"], "Presente")
        self.assertEqual(cards["Ana Gomez"]["att"]["by"], "Marta")
        self.assertEqual(cards["Ana Gomez"]["good"], 3)
        self.assertIsNone(cards["Beto Ruiz"]["att"])
        self.assertEqual(s["people"]["attendance"]["pending"], 1)
        self.assertNotIn("Otro Operario", cards)
        self.assertNotIn("Carla Diaz", cards)  # el supernumerario no se ve a sí mismo

    def test_puts_and_closes_stops_of_people_and_of_the_whole_line(self):
        r = self.post("piso:api_stop", {"cause": "Falla mecánica", "emp": self.ana.pk})
        self.assertEqual(r.status_code, 200, r.content)
        st = Stop.objects.get(employee=self.ana, end__isnull=True)
        self.assertEqual(st.work_center, self.wc)
        self.assertEqual(self.post("piso:api_stop_close", {"id": st.pk}).status_code, 200)
        st.refresh_from_db()
        self.assertIsNotNone(st.end)
        self.assertEqual(self.post("piso:api_stop", {"cause": "Baño", "emp": self.outsider.pk}).status_code, 403)
        self.assertEqual(self.post("piso:api_stop", {"cause": "Falta de material", "wc": self.wc.pk}).status_code, 200)
        self.assertTrue(Stop.objects.filter(work_center=self.wc, employee__isnull=True, end__isnull=True).exists())

    def test_sends_messages_that_reach_the_operator(self):
        r = self.post("piso:api_sup_message", {"to": "p", "ids": [self.ana.pk], "text": "Reporten la producción"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.post("piso:api_sup_message", {"to": "a", "text": "Hola a todos"}).json()["n"], 2)
        self.assertEqual(self.post("piso:api_sup_message", {"to": "p", "ids": [self.outsider.pk], "text": "x"}).status_code,
                         403)
        self.assertEqual(self.post("piso:api_sup_message", {"to": "l", "line": self.wc.pk, "text": "Línea"}).status_code, 200)
        self.client.post(reverse("piso:api_logout"))
        self.login()
        msgs = self.client.get(reverse("piso:api_state")).json()["messages"]
        self.assertEqual([m["text"] for m in msgs], ["Reporten la producción", "Hola a todos", "Línea"])
        self.assertEqual(msgs[0]["from"], "Carla Diaz (supernumerario)")
        self.assertTrue(all(m["alarm"] for m in msgs))

    def test_message_with_or_without_alarm_and_buzz_only(self):
        self.post("piso:api_sup_message", {"to": "p", "ids": [self.ana.pk], "text": "Sin ruido", "alarm": False})
        self.assertEqual(self.post("piso:api_sup_message", {"to": "p", "ids": [self.ana.pk], "text": ""}).status_code, 200)
        r = self.post("piso:api_sup_message", {"to": "p", "ids": [self.ana.pk], "text": "", "alarm": False})
        self.assertEqual(r.status_code, 409)  # sin alarma y sin texto no hay nada que enviar
        self.client.post(reverse("piso:api_logout"))
        self.login()
        msgs = self.client.get(reverse("piso:api_state")).json()["messages"]
        self.assertEqual([(m["text"], m["alarm"]) for m in msgs],
                         [("Sin ruido", False), ("🔔 ¡Atención! Mira la pantalla.", True)])


class PresenceAndNightlyLogoutTests(ReportingBase):
    def age_session(self, **delta):
        s = self.client.session
        data = s["piso"]
        data["t"] = (timezone.now() - timedelta(**delta)).timestamp()
        s["piso"] = data
        s.save()

    def test_board_shows_who_has_the_app_open_even_without_an_operation(self):
        loose = Employee.objects.create(document="4001", full_name="dora sin tarea", shift=self.shift)
        acc = FloorAccess(employee=loose)
        acc.set_pin("1234")
        acc.save()
        self.login("4001")
        self.client.get(reverse("piso:api_state"))
        self.client.force_login(self.sup)
        d = self.client.get(reverse("piso:live_data")).json()
        cards = {c["name"]: c for c in d["cards"]}
        self.assertTrue(cards["Dora Sin Tarea"]["online"])  # aparece aunque no esté en ninguna operación
        self.assertIsNone(cards["Dora Sin Tarea"]["task"])
        self.assertFalse(cards["Beto Ruiz"]["online"])
        self.assertEqual(d["kpis"]["online"], 1)
        r = self.post("piso:live_message", {"to": "p", "ids": [loose.pk], "text": "Pase por la oficina"})
        self.assertEqual(r.status_code, 200, r.content)
        FloorAccess.objects.filter(employee=loose).update(last_seen=timezone.now() - timedelta(minutes=5))
        self.assertNotIn("Dora Sin Tarea", [c["name"] for c in self.client.get(reverse("piso:live_data")).json()["cards"]])

    def test_logout_marks_offline(self):
        self.login()
        self.client.get(reverse("piso:api_state"))
        self.assertIsNotNone(FloorAccess.objects.get(employee=self.ana).last_seen)
        self.post("piso:api_logout")
        self.assertIsNone(FloorAccess.objects.get(employee=self.ana).last_seen)

    def test_people_sessions_close_every_night_but_line_tablets_stay(self):
        from . import views

        cut = views.session_cutoff()
        self.login()
        self.assertEqual(self.client.get(reverse("piso:api_state")).status_code, 200)
        # entró antes del último corte de las 22:00 → la sesión se cierra sola
        self.age_session(seconds=(timezone.now() - cut).total_seconds() + 60)
        r = self.client.get(reverse("piso:api_state"))
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.json()["closed"])
        self.assertEqual(self.client.get(reverse("piso:api_state")).status_code, 401)  # ya no hay sesión
        self.login()
        self.assertEqual(self.client.get(reverse("piso:api_state")).status_code, 200)  # entra de nuevo
        # la tablet fija de la línea no se desvincula
        lc = LineCode.for_line(self.wc)
        self.post("piso:api_pair", {"code": lc.pair_code})
        s = self.client.session
        s["piso"] = {**s["piso"], "t": 0}
        s.save()
        self.assertEqual(self.client.get(reverse("piso:api_state")).status_code, 200)

    def test_cutoff_is_the_last_22h(self):
        from datetime import datetime as dt

        from . import views

        tz = timezone.get_current_timezone()
        self.assertEqual(views.session_cutoff(dt(2026, 11, 20, 23, 0, tzinfo=tz)).hour, 22)
        self.assertEqual(views.session_cutoff(dt(2026, 11, 20, 23, 0, tzinfo=tz)).day, 20)
        self.assertEqual(views.session_cutoff(dt(2026, 11, 20, 7, 0, tzinfo=tz)).day, 19)
        with self.settings(PISO_CIERRE_SESIONES=""):
            self.assertIsNone(views.session_cutoff())


class InstallableAppAndSoundTests(ReportingBase):
    def test_manifest_and_service_worker_make_the_app_installable(self):
        m = self.client.get(reverse("piso:manifest"))
        self.assertEqual(m["Content-Type"], "application/manifest+json")
        data = m.json()
        self.assertEqual((data["start_url"], data["scope"], data["display"]), ("/p/piso/", "/p/piso/", "standalone"))
        self.assertEqual({i["sizes"] for i in data["icons"]}, {"192x192", "512x512"})
        sw = self.client.get(reverse("piso:sw"))
        self.assertEqual(sw["Service-Worker-Allowed"], "/p/piso/")
        self.assertIn("notificationclick", sw.content.decode())
        self.assertContains(self.client.get(reverse("piso:tablet")), 'rel="manifest"')

    def test_each_device_reports_if_it_can_ring_and_the_board_shows_it(self):
        self.login()
        self.client.get(reverse("piso:api_state") + "?snd=0")
        self.assertIs(FloorAccess.objects.get(employee=self.ana).sound_ok, False)
        self.client.force_login(self.sup)
        card = next(c for c in self.client.get(reverse("piso:live_data")).json()["cards"] if c["id"] == self.ana.pk)
        self.assertEqual((card["online"], card["sound"]), (True, False))
        lc = LineCode.for_line(self.wc)
        self.post("piso:api_pair", {"code": lc.pair_code})
        self.client.get(reverse("piso:api_state") + "?snd=1")
        lc.refresh_from_db()
        self.assertTrue(lc.tablet_sound)
        self.assertIsNotNone(lc.tablet_seen)


class MesPageTests(ReportingBase):
    def test_configure_lines_processes_and_supernumeraries(self):
        boss = self.sup
        boss.is_staff = True  # configura líneas y procesos (el supervisor solo asigna supernumerarios)
        boss.save()
        self.client.force_login(boss)
        url = reverse("piso:reporting")
        self.assertContains(self.client.get(url), "Forma de reporte")
        self.client.post(url, {"a": "lineas", f"m{self.wc.pk}": "intervalo", f"i{self.wc.pk}": "30"})
        lc = LineCode.objects.get(work_center=self.wc)
        self.assertEqual((lc.report_mode, lc.interval_minutes), ("intervalo", 30))
        self.client.post(url, {"a": "procesos", "pn": ["Limpieza"], "pm0": "supernumerario", "pi0": ""})
        self.assertEqual(ProcessReporting.objects.get().report_mode, "supernumerario")
        self.client.post(url, {"a": "procesos", "pn": ["Limpieza"], "pm0": ""})
        self.assertFalse(ProcessReporting.objects.exists())
        self.client.post(url, {"a": "sup", "emp": self.carla.pk, "wc": [str(self.wc.pk)]})
        self.assertTrue(SupernumeraryLine.objects.filter(employee=self.carla, work_center=self.wc, source="mes").exists())
        self.assertContains(self.client.get(url), "Carla")


class PlanningFieldsTests(ReportingBase):
    def test_stage_report_mode_is_validated_and_kept(self):
        from django.core.exceptions import ValidationError

        from mes_core import stage_planning

        base = {"name": "Armar", "line": self.wc.pk, "quantity": 100, "std_value": 1, "std_unit": "min_unidad"}
        st = stage_planning.parse_stages(self.order, [{**base, "report_mode": "supernumerario", "split_mode": "iguales"}])[0]
        self.assertEqual((st.report_mode, st.split_mode), ("supernumerario", "iguales"))
        with self.assertRaises(ValidationError):
            stage_planning.parse_stages(self.order, [{**base, "report_mode": "otro"}])


class SupEanControlTests(ReportingBase):
    """El supernumerario ordena las referencias (EAN) de la etapa y abre / cierra el envío de datos a cada una."""
    A, B, C = "4006381333931", "5901234123457", "9780306406157"

    def setUp(self):
        from django.apps import apps

        if not apps.is_installed("plugins.eanes"):
            self.skipTest("módulo de validación EAN no instalado")
        super().setUp()
        from plugins.eanes.models import EanLine, EanSheet

        self.line_mode("supernumerario")
        EanSheet.objects.create(order=self.order, status=EanSheet.Status.LOADED)
        for i, (code, qty) in enumerate(((self.A, 40), (self.B, 40), (self.C, 20))):
            EanLine.objects.create(order=self.order, ean=code, quantity=qty, position=i)
        self.login("2001")

    def items(self):
        s = self.client.get(reverse("piso:api_state")).json()
        op = next(o for o in s["lines"][0]["ops"] if o["id"] == self.op1.pk)
        return [(i["ean"], i["on"], i["pos"]) for i in op["ref"]["items"]]

    def ctl(self, ean, action, op=None):
        return self.post("piso:api_sup_ean", {"op": (op or self.op1).pk, "e": ean, "a": action})

    def report(self, ean, g):
        return self.post("piso:api_report", {"op": self.op1.pk, "g": str(g), "e": ean})

    def test_order_open_close_and_only(self):
        from plugins.eanes.models import EanLog

        self.assertEqual(self.items(), [(self.A, True, 1), (self.B, True, 2), (self.C, True, 3)])
        self.assertEqual(self.ctl(self.B, "primero").status_code, 200)
        self.assertEqual(self.ctl(self.C, "subir").status_code, 200)
        self.assertEqual([e for e, _, _ in self.items()], [self.B, self.C, self.A])
        self.assertEqual(self.ctl(self.B, "solo").status_code, 200)
        self.assertEqual(self.items(), [(self.B, True, 1), (self.C, False, 2), (self.A, False, 3)])
        r = self.report(self.A, 5)
        self.assertEqual(r.status_code, 409)
        self.assertIn("está cerrada", r.json()["e"])
        self.assertEqual(self.report(self.B, 5).status_code, 200, self.report)
        self.assertEqual(self.ctl(self.A, "abrir").status_code, 200)
        self.assertEqual(self.report(self.A, 5).status_code, 200)
        self.assertEqual(self.ctl(self.A, "cerrar").status_code, 200)
        self.assertEqual(self.ctl("", "todas").status_code, 200)
        self.assertTrue(all(on for _, on, _ in self.items()))
        self.assertEqual([e for e, _, _ in self.items()], [self.B, self.C, self.A])  # abrir no cambia el orden
        # al 100 % de la referencia no se le puede enviar más, aunque esté abierta
        r = self.report(self.B, 36)
        self.assertEqual(r.status_code, 409)
        self.assertIn("faltan 35", r.json()["e"])
        self.assertTrue(EanLog.objects.filter(order=self.order, detail__contains="carla").exists())

    def test_only_the_supernumerary_of_the_line(self):
        self.assertEqual(self.ctl("1234", "cerrar").status_code, 400)  # no es del pedido
        self.assertEqual(self.ctl(self.A, "volar").status_code, 400)
        wc2 = WorkCenter.objects.create(site=self.site, code="L2", name="Banda", kind="empaque")
        op3 = Operation.objects.create(order=self.order, sequence=3, name="Limpieza", work_center=wc2, crew=1,
                                       minutes_per_unit=Decimal("0.2"), supervisor=self.other_sup,
                                       planned_start=timezone.now(), planned_end=timezone.now() + timedelta(hours=8))
        self.assertEqual(self.ctl(self.A, "cerrar", op3).status_code, 403)
        self.client.post(reverse("piso:api_logout"), "{}", content_type="application/json")
        self.login("1001")  # un operario no maneja las referencias
        self.assertEqual(self.ctl(self.A, "cerrar").status_code, 403)

    def test_office_corrections_of_old_reports_are_not_blocked(self):
        from plugins.eanes import eans

        self.assertEqual(self.report(self.A, 5).status_code, 200)
        self.ctl(self.A, "cerrar")
        rep = ProductionReport.objects.get(operation=self.op1, ean=self.A)
        eans.check_report(self.op1, ean=self.A, qty_good=4, qty_leftover=0, exclude_report=rep.pk)  # no lanza
        with self.assertRaisesMessage(Exception, "está cerrada"):
            eans.check_report(self.op1, ean=self.A, qty_good=1, qty_leftover=0)
