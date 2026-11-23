"""Control de piso: tablets conectadas al MES."""

import json
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mes_core.models import (
    CrewAssignment, Employee, Operation, Product, ProductionReport, Shift, Site, WorkCenter, WorkOrder,
)

from . import floor, transfers
from .models import FloorAccess, LineCode, Location, Mark, Message, Stop, Transfer


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class FloorBase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.site = Site.objects.create(code="P1", name="Planta 1")
        self.shift = Shift.objects.create(site=self.site, name="Día", start_time=time(0, 1), end_time=time(23, 59))
        self.wc = WorkCenter.objects.create(site=self.site, code="L1", name="Línea 1", kind="empaque")
        self.product = Product.objects.create(sku="P-1", name="Caja")
        self.order = WorkOrder.objects.create(site=self.site, product=self.product, process_type="x",
                                              quantity=Decimal("5"), status=WorkOrder.Status.RELEASED)
        tz = timezone.get_current_timezone()
        today = timezone.localdate()
        self.sup = User.objects.create_user("sup", password="x", first_name="Marta")
        self.sup.groups.add(Group.objects.get(name="Supervisor"))
        self.other_sup = User.objects.create_user("sup2", password="x", first_name="Julián")
        self.other_sup.groups.add(Group.objects.get(name="Supervisor"))
        self.op1 = Operation.objects.create(order=self.order, sequence=1, name="Armar", work_center=self.wc, crew=2,
                                            minutes_per_unit=Decimal("1"), supervisor=self.sup,
                                            planned_start=datetime.combine(today, time(7), tzinfo=tz))
        self.op2 = Operation.objects.create(order=self.order, sequence=2, name="Sellar", work_center=self.wc, crew=1,
                                            minutes_per_unit=Decimal("0.5"), supervisor=self.other_sup,
                                            planned_start=datetime.combine(today, time(9), tzinfo=tz))
        self.ana = Employee.objects.create(document="1001", full_name="ana gomez", shift=self.shift)
        self.beto = Employee.objects.create(document="1002", full_name="beto ruiz", shift=self.shift)
        for emp in (self.ana, self.beto):
            acc = FloorAccess(employee=emp)
            acc.set_pin("1234")
            acc.save()
        CrewAssignment.objects.create(operation=self.op1, employee=self.ana)
        CrewAssignment.objects.create(operation=self.op2, employee=self.ana)
        CrewAssignment.objects.create(operation=self.op1, employee=self.beto)

    def login(self, doc="1001", pin="1234", checkin=True):
        """Entra con documento + PIN y (por defecto) acepta la pantalla de llegada, como haría la persona."""
        r = self.client.post(reverse("piso:api_login"), json.dumps({"doc": doc, "pin": pin}),
                             content_type="application/json")
        if checkin and r.status_code == 200:
            self.client.post(reverse("piso:api_checkin"), "{}", content_type="application/json")
        return r

    def post(self, name, body=None, **kw):
        return self.client.post(reverse(name, kwargs=kw or None), json.dumps(body or {}), content_type="application/json")


class TabletTests(FloorBase):
    def test_login_needs_valid_pin_and_shows_assigned_task(self):
        self.assertEqual(self.login(pin="0000").status_code, 401)
        self.assertEqual(self.client.get(reverse("piso:api_state")).status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual(s["kind"], "operario")
        self.assertEqual(s["task"]["name"], "Armar")  # la primera de su cola, en el orden del programa
        self.assertEqual([t["name"] for t in s["queue"]], ["Sellar"])
        self.assertIn("e%d" % self.ana.pk, s["pulse"])

    def test_unreleased_orders_do_not_reach_the_tablet(self):
        WorkOrder.objects.filter(pk=self.order.pk).update(status=WorkOrder.Status.PLANNED)
        self.login()
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertIsNone(s["task"])
        r = self.post("piso:api_mark", {"k": "p"})
        self.assertEqual(r.status_code, 409)

    def test_marks_become_one_merged_production_report_in_the_mes(self):
        self.login()
        for _ in range(2):
            self.assertEqual(self.post("piso:api_mark", {"k": "p"}).status_code, 200)
        self.assertEqual(self.post("piso:api_mark", {"k": "x"}).status_code, 200)
        self.assertEqual(self.post("piso:api_mark", {"k": "w"}).status_code, 200)
        self.op1.refresh_from_db()
        self.assertEqual((self.op1.qty_good, self.op1.qty_scrap, self.op1.status), (2, 1, Operation.Status.IN_PROGRESS))
        reps = ProductionReport.objects.filter(operation=self.op1)
        self.assertEqual(reps.count(), 1)  # se acumulan en un solo reporte (ventana de 15 min)
        rep = reps.get()
        self.assertEqual((rep.qty_good, rep.qty_scrap, rep.employee, rep.source), (2, 1, self.ana, "piso"))
        self.assertEqual(Mark.objects.filter(employee=self.ana).count(), 4)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WorkOrder.Status.IN_PROGRESS)

    def test_fast_taps_arrive_together_with_their_own_times(self):
        """La tablet agrupa los toques seguidos: un solo envío, una marcación por toque con su hora real."""
        self.login()
        base = timezone.now().timestamp() * 1000
        times = [base - 3000, base - 2000, base - 1000]
        r = self.post("piso:api_mark", {"k": "p", "q": 3, "op": self.op1.pk, "t": times})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["task"]["done"], r.json()["task"]["pending"]), (3, 2))
        marks = list(Mark.objects.filter(employee=self.ana).order_by("at"))
        self.assertEqual([m.qty for m in marks], [1, 1, 1])
        gaps = [(b.at - a.at).total_seconds() for a, b in zip(marks, marks[1:])]
        self.assertEqual([round(g) for g in gaps], [1, 1])  # el ciclo entre toques se conserva
        self.assertEqual(ProductionReport.objects.get(operation=self.op1).qty_good, 3)
        # horas absurdas o que no cuadran con la cantidad: una sola marcación con la hora de llegada
        self.post("piso:api_mark", {"k": "x", "q": 2, "op": self.op1.pk, "t": [1]})
        self.assertEqual(Mark.objects.get(kind="x").qty, 2)

    def test_marks_do_not_wake_every_tablet(self):
        from django.core.cache import cache

        from .live import PREFIX

        cache.clear()
        with self.captureOnCommitCallbacks(execute=True):
            floor.add_mark(operation=self.op1, employee=self.ana)
        self.assertIsNone(cache.get(PREFIX + "g"))           # las demás tablets no recargan
        self.assertIsNotNone(cache.get(PREFIX + f"e{self.ana.pk}"))
        with self.captureOnCommitCallbacks(execute=True):
            floor.add_mark(operation=self.op1, employee=self.ana, qty=4)  # cierra la operación: ahí sí se avisa
        self.assertIsNotNone(cache.get(PREFIX + "g"))

    def test_reaching_the_goal_closes_the_operation_and_moves_to_the_next(self):
        self.login()
        r = self.post("piso:api_mark", {"k": "p", "q": 7})
        self.assertEqual(r.status_code, 409)
        self.assertIn("Solo faltan 5", r.json()["e"])
        r = self.post("piso:api_mark", {"k": "p", "q": "5"})
        self.assertTrue(r.json()["finished"])
        self.op1.refresh_from_db()
        self.assertEqual(self.op1.status, Operation.Status.DONE)
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual(s["task"]["name"], "Sellar")
        self.assertEqual(s["done_today"][0]["name"], "Armar")
        # El compañero de cuadrilla ya no puede marcar en la operación cerrada: queda disponible
        self.client.post(reverse("piso:api_logout"))
        self.login("1002")
        self.assertIsNone(self.client.get(reverse("piso:api_state")).json()["task"])

    def test_open_stop_blocks_marks_and_breaks_need_the_line_qr(self):
        self.login()
        self.assertEqual(self.post("piso:api_stop", {"cause": "Inventado"}).status_code, 400)
        self.assertEqual(self.post("piso:api_stop", {"cause": "Baño"}).status_code, 200)
        r = self.post("piso:api_mark", {"k": "p"})
        self.assertEqual(r.status_code, 409)
        st = Stop.objects.get(employee=self.ana, end__isnull=True)
        self.assertEqual(st.work_center, self.wc)
        self.assertEqual(self.post("piso:api_stop_close", {"id": st.pk}).status_code, 200)
        self.assertEqual(self.post("piso:api_mark", {"k": "p"}).status_code, 200)
        # Pausa: no se cierra con «Reanudar», solo con el QR de una línea
        self.assertEqual(self.post("piso:api_break", {"kind": "Desayuno"}).status_code, 200)
        brk = Stop.objects.get(employee=self.ana, end__isnull=True)
        self.assertTrue(brk.is_break)
        self.assertEqual(self.post("piso:api_stop_close", {"id": brk.pk}).status_code, 409)
        self.assertEqual(self.post("piso:api_resume", {"code": "MALO"}).status_code, 409)
        code = LineCode.for_line(self.wc).pause_code
        self.assertEqual(self.post("piso:api_resume", {"code": code.lower()}).status_code, 200)
        brk.refresh_from_db()
        self.assertIsNotNone(brk.end)
        self.assertEqual(self.post("piso:api_break", {"kind": "Desayuno"}).status_code, 409)  # una vez por turno

    def test_single_break_window_starts_by_itself(self):
        now = timezone.localtime()
        start = (now - timedelta(minutes=5)).time().replace(second=0, microsecond=0)
        end = (now + timedelta(minutes=10)).time().replace(second=0, microsecond=0)
        if not (time(0, 2) < start < end < time(23, 58)):
            self.skipTest("Cerca de medianoche")
        self.shift.breakfast_start, self.shift.breakfast_end = start, end
        self.shift.save()
        self.ana.refresh_from_db()
        floor.operator_state(self.ana)
        st = Stop.objects.get(employee=self.ana, end__isnull=True)
        self.assertEqual((st.cause, st.is_break), ("Desayuno", True))
        floor.operator_state(self.ana)
        self.assertEqual(Stop.objects.filter(employee=self.ana, is_break=True).count(), 1)

    def test_admin_pin_change_closes_the_session(self):
        self.login()
        acc = FloorAccess.objects.get(employee=self.ana)
        acc.set_pin("9999")
        acc.save()
        self.assertEqual(self.client.get(reverse("piso:api_state")).status_code, 401)

    def test_operator_changes_own_pin(self):
        self.login()
        self.assertEqual(self.post("piso:api_pin", {"pin": "1111", "new": "4321"}).status_code, 403)
        self.assertEqual(self.post("piso:api_pin", {"pin": "1234", "new": "12"}).status_code, 400)
        self.assertEqual(self.post("piso:api_pin", {"pin": "1234", "new": "4321"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("piso:api_state")).status_code, 200)  # sigue dentro
        self.client.post(reverse("piso:api_logout"))
        self.assertEqual(self.login(pin="4321").status_code, 200)

    def test_line_tablet_marks_for_the_line_and_stops_crew_members(self):
        lc = LineCode.for_line(self.wc)
        self.assertEqual(self.post("piso:api_pair", {"code": "XXXX"}).status_code, 401)
        self.assertEqual(self.post("piso:api_pair", {"code": lc.pair_code.lower()}).status_code, 200)
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual((s["kind"], s["task"]["name"]), ("linea", "Armar"))
        self.assertEqual({c["name"] for c in s["crew"]}, {"Ana Gomez", "Beto Ruiz"})
        self.assertEqual(self.post("piso:api_mark", {"k": "p"}).status_code, 200)
        rep = ProductionReport.objects.get(operation=self.op1)
        self.assertEqual((rep.employee, rep.source), (None, "piso"))
        self.assertEqual(self.post("piso:api_stop", {"cause": "Baño", "emp": self.beto.pk}).status_code, 200)
        other = Employee.objects.create(document="77", full_name="zoe")
        self.assertEqual(self.post("piso:api_stop", {"cause": "Baño", "emp": other.pk}).status_code, 403)
        self.assertEqual(self.post("piso:api_stop", {"cause": "Falla mecánica"}).status_code, 200)  # toda la línea
        self.assertEqual(self.post("piso:api_mark", {"k": "p"}).status_code, 409)
        # Un código nuevo desvincula la tablet
        lc.pair_code = "NUEVO123"
        lc.save()
        self.assertEqual(self.client.get(reverse("piso:api_state")).status_code, 401)

    def test_deleting_the_report_in_the_mes_removes_its_floor_marks(self):
        from mes_core import corrections

        floor.add_mark(operation=self.op1, employee=self.ana)
        floor.add_mark(operation=self.op1, employee=self.ana)
        rep = ProductionReport.objects.get(operation=self.op1)
        corrections.delete_report(rep)
        self.assertFalse(Mark.objects.filter(employee=self.ana).exists())
        self.op1.refresh_from_db()
        self.assertEqual((self.op1.qty_good, self.op1.status), (0, Operation.Status.PENDING))

    def test_pulse_is_public_and_cheap(self):
        r = self.client.get(reverse("piso:api_pulse") + "?k=g,e1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("g:", r.json()["v"])


class SupervisorTests(FloorBase):
    def test_board_shows_only_the_supervisor_operations(self):
        floor.add_mark(operation=self.op1, employee=self.beto)
        data = floor.board(self.sup)
        names = {c["name"] for c in data["cards"]}
        self.assertEqual(names, {"Ana Gomez", "Beto Ruiz"})
        beto = next(c for c in data["cards"] if c["name"] == "Beto Ruiz")
        self.assertEqual((beto["good"], beto["state"]), (1, "ok"))
        self.assertEqual(data["kpis"]["good"], 1)
        other = floor.board(self.other_sup)
        self.assertEqual({c["name"] for c in other["cards"]}, {"Ana Gomez"})  # solo Sellar es de Julián

    def test_message_reaches_the_tablet(self):
        self.client.force_login(self.sup)
        r = self.client.post(reverse("piso:live_message"), json.dumps({"to": "p", "ids": [self.ana.pk], "text": "Hola Ana"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.client.logout()
        self.login()
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual([m["text"] for m in s["messages"]], ["Hola Ana"])
        self.post("piso:api_read", {"id": s["messages"][0]["id"]})
        self.assertEqual(self.client.get(reverse("piso:api_state")).json()["messages"], [])

    def test_line_message_goes_to_its_crew(self):
        msg = None
        self.client.force_login(self.sup)
        r = self.client.post(reverse("piso:live_message"), json.dumps({"to": "l", "line": self.wc.pk, "text": "Cambio"}),
                             content_type="application/json")
        self.assertEqual(r.json()["n"], 2)
        msg = Message.objects.get()
        self.assertEqual(msg.work_center, self.wc)

    def test_supervisor_closes_a_break_without_qr(self):
        st = floor.start_break(self.ana, "Almuerzo", operation=self.op1)
        self.client.force_login(self.sup)
        self.assertEqual(self.client.post(reverse("piso:live_close_stop", args=[st.pk])).status_code, 200)
        st.refresh_from_db()
        self.assertEqual(st.closed_by, self.sup)

    def test_pages_render(self):
        admin = get_user_model().objects.create_superuser("root", password="x")
        self.client.force_login(admin)
        floor.add_mark(operation=self.op1, employee=self.ana)
        for name in ("live", "live_data", "history", "transfers", "codes", "tablet"):
            r = self.client.get(reverse(f"piso:{name}"))
            self.assertEqual(r.status_code, 200, name)
        self.assertContains(self.client.get(reverse("piso:history")), "Ana Gomez")
        self.assertContains(self.client.get(reverse("mes_core:order_detail", args=[self.order.pk])), "Piso")

    def test_operators_without_supervision_rights_cannot_open_the_board(self):
        viewer = get_user_model().objects.create_user("v", password="x")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("piso:live")).status_code, 403)
        self.assertEqual(self.client.get(reverse("piso:codes")).status_code, 403)

    def test_admin_action_grants_pins_as_excel(self):
        admin = get_user_model().objects.create_superuser("root", password="x")
        self.client.force_login(admin)
        new = Employee.objects.create(document="555", full_name="carla")
        r = self.client.post(reverse("admin:mes_core_employee_changelist"),
                             {"action": "grant_mover", "_selected_action": [new.pk]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])
        acc = FloorAccess.objects.get(employee=new)
        self.assertEqual(acc.role, FloorAccess.Role.MOVER)


class EmployeeFormPinTests(FloorBase):
    def _post(self, emp, extra):
        data = {"document": emp.document, "full_name": emp.full_name, "position": "", "shift": self.shift.pk,
                "supervisor": "", "phone": "", "notes": "", "active": "on",
                "skills-TOTAL_FORMS": "0", "skills-INITIAL_FORMS": "0", "skills-MIN_NUM_FORMS": "0", "skills-MAX_NUM_FORMS": "1000"}
        data.update(extra)
        return self.client.post(reverse("admin:mes_core_employee_change", args=[emp.pk]), data, follow=True)

    def test_pin_from_the_employee_form(self):
        admin = get_user_model().objects.create_superuser("root", password="x")
        self.client.force_login(admin)
        carla = Employee.objects.create(document="555", full_name="carla", shift=self.shift)
        page = self.client.get(reverse("admin:mes_core_employee_change", args=[carla.pk]))
        self.assertContains(page, "cceso a la tablet de piso (PIN)")
        self.assertContains(page, "Generar PIN al azar")
        prefix = next(k for k in page.context["inline_admin_formsets"] if k.formset.model is FloorAccess).formset.prefix
        mgmt = {f"{prefix}-TOTAL_FORMS": "1", f"{prefix}-INITIAL_FORMS": "0", f"{prefix}-MIN_NUM_FORMS": "0",
                f"{prefix}-MAX_NUM_FORMS": "1"}
        # PIN escrito a mano
        r = self._post(carla, {**mgmt, f"{prefix}-0-role": "operario", f"{prefix}-0-active": "on",
                               f"{prefix}-0-new_pin": "2468"})
        self.assertContains(r, "2468")
        self.assertTrue(FloorAccess.objects.get(employee=carla).check_pin("2468"))
        # Guardar de nuevo sin tocar el apartado no cambia el PIN
        acc = FloorAccess.objects.get(employee=carla)
        mgmt1 = {**mgmt, f"{prefix}-INITIAL_FORMS": "1", f"{prefix}-0-id": acc.pk, f"{prefix}-0-employee": carla.pk,
                 f"{prefix}-0-role": "operario", f"{prefix}-0-active": "on"}
        self._post(carla, mgmt1)
        self.assertTrue(FloorAccess.objects.get(employee=carla).check_pin("2468"))
        # Generar uno al azar
        r = self._post(carla, {**mgmt1, f"{prefix}-0-generate": "on"})
        self.assertContains(r, "no se vuelve a mostrar")
        self.assertFalse(FloorAccess.objects.get(employee=carla).check_pin("2468"))
        # La lista muestra quién tiene PIN
        self.assertContains(self.client.get(reverse("admin:mes_core_employee_changelist")), "Con PIN")


class TransferTests(FloorBase):
    def test_line_requests_material_and_mover_delivers_it(self):
        bodega = Location.objects.create(site=self.site, name="Bodega", distance=3)
        frente = Location.objects.create(site=self.site, name="Frente L1", work_center=self.wc)
        acc = FloorAccess.objects.get(employee=self.beto)
        acc.role = FloorAccess.Role.MOVER
        acc.save()
        lc = LineCode.for_line(self.wc)
        self.post("piso:api_pair", {"code": lc.pair_code})
        self.assertEqual(self.post("piso:api_transfer", {"o": bodega.pk, "est": 2, "nota": "cajas"}, action="pedir").status_code, 200)
        t = Transfer.objects.get()
        self.assertEqual((t.origin, t.destination, t.from_line, t.pallets), (bodega, frente, True, 2))
        self.assertEqual(self.post("piso:api_transfer", {"id": t.pk}, action="avanzar").status_code, 403)  # la línea no mueve
        self.client.post(reverse("piso:api_logout"))
        self.login("1002")
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual((s["kind"], len(s["pool"])), ("patinador", 1))
        for expected in ("a", "c", "e"):
            r = self.post("piso:api_transfer", {"id": t.pk}, action="avanzar")
            self.assertEqual(r.json()["status"], expected)
        t.refresh_from_db()
        self.assertEqual((t.mover, t.status), (self.beto, Transfer.Status.DELIVERED))
        k = transfers.kpis(self.beto, floor.day_start())
        self.assertEqual((k["trips"], k["pallets"], k["points"]), (1, 2, 2 * 3 * transfers.POINTS_PER_WEIGHT))

    def test_one_transfer_in_course_at_a_time(self):
        a = transfers.create(site=self.site, user=None, origin=Location.objects.create(site=self.site, name="A"),
                             destination=Location.objects.create(site=self.site, name="B"))
        b = Transfer.objects.create(site=self.site, origin=a.origin, destination=a.destination)
        transfers.advance(a, self.beto)
        with self.assertRaises(floor.FloorError):
            transfers.advance(b, self.beto)
        transfers.advance(a, self.beto)
        transfers.advance(a, self.beto)
        transfers.advance(b, self.beto)  # ya entregó el otro


class UnscheduledTests(FloorBase):
    """Solo se trabaja lo programado: un saldo sin fecha en el programador se ve en la cola, bloqueado."""

    def setUp(self):
        super().setUp()
        Operation.objects.filter(pk=self.op2.pk).update(planned_start=None, planned_end=None, name="Sellar · saldo")

    def test_unscheduled_shows_locked_and_cannot_be_reported(self):
        self.login()
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual(s["task"]["id"], self.op1.pk)
        self.assertTrue(s["task"]["scheduled"])
        locked = next(q for q in s["queue"] if q["id"] == self.op2.pk)
        self.assertFalse(locked["scheduled"])
        r = self.post("piso:api_mark", {"k": "p", "op": self.op2.pk})
        self.assertEqual(r.status_code, 409)
        self.assertIn("programador", r.json()["e"])
        self.assertFalse(ProductionReport.objects.filter(operation=self.op2).exists())

    def test_only_unscheduled_left_means_no_current_task(self):
        Operation.objects.filter(pk=self.op1.pk).update(planned_start=None)
        self.login()
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertIsNone(s["task"])
        self.assertEqual({q["id"] for q in s["queue"]}, {self.op1.pk, self.op2.pk})
        self.assertEqual(self.post("piso:api_mark", {"k": "p"}).status_code, 409)


class CheckInTests(FloorBase):
    """Lo primero del día: ver a dónde va y confirmar la asistencia. Antes no se habilita nada."""

    def setUp(self):
        super().setUp()
        from django.core.cache import cache

        cache.clear()

    def test_nothing_works_until_arrival_is_confirmed(self):
        from mes_core.models import Attendance

        from .models import CheckIn

        self.login(checkin=False)
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertFalse(s["checkin"]["done"])
        info = s["checkin"]["info"]
        self.assertEqual(info["operation"], "Armar")
        self.assertIn(self.wc.code, info["line"])
        self.assertTrue(info["site"])
        r = self.post("piso:api_mark", {"k": "p"})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["checkin"])
        self.assertFalse(ProductionReport.objects.exists())
        r = self.post("piso:api_checkin")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["state"]["checkin"]["done"])
        att = Attendance.objects.get(employee=self.ana)
        self.assertEqual(att.status, Attendance.Status.PRESENT)
        self.assertIn("tablet", att.notes)
        self.assertEqual(CheckIn.objects.get(employee=self.ana).operation_id, self.op1.pk)
        self.assertEqual(self.post("piso:api_mark", {"k": "p"}).status_code, 200)

    def test_supervisor_late_mark_is_kept(self):
        from django.utils import timezone

        from mes_core.models import Attendance

        Attendance.objects.create(employee=self.ana, day=timezone.localdate(), status=Attendance.Status.LATE)
        self.login()
        self.assertEqual(Attendance.objects.get(employee=self.ana).status, Attendance.Status.LATE)

    def test_page_boot_brings_the_arrival_screen(self):
        self.login(checkin=False)
        html = self.client.get(reverse("piso:tablet")).content.decode()
        self.assertIn('"checkin": {"done": false', html)


class LateArrivalTests(FloorBase):
    """«Llegada hasta» del turno: después de esa hora la llegada queda «Llegó tarde» sola."""

    def setUp(self):
        super().setUp()
        from django.core.cache import cache

        cache.clear()
        from datetime import time as _t

        from mes_core.models import Shift

        self.day_shift = Shift.objects.create(site=self.site, name="Día prueba", start_time=_t(6), end_time=_t(14),
                                              checkin_deadline=_t(6, 15))
        self.night = Shift.objects.create(site=self.site, name="Noche prueba", start_time=_t(22), end_time=_t(6),
                                          checkin_deadline=_t(22, 10))

    def at(self, day_offset, h, m):
        from datetime import datetime, timedelta

        from django.utils import timezone

        d = timezone.localdate() + timedelta(days=day_offset)
        return datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.get_current_timezone())

    def test_deadline_of_day_and_night_shifts(self):
        from . import checkin

        self.assertEqual(checkin.deadline(self.day_shift, self.at(0, 5, 50)), self.at(0, 6, 15))
        self.assertEqual(checkin.deadline(self.day_shift, self.at(0, 9, 0)), self.at(0, 6, 15))
        self.assertEqual(checkin.deadline(self.night, self.at(0, 21, 45)), self.at(0, 22, 10))
        self.assertEqual(checkin.deadline(self.night, self.at(1, 0, 30)), self.at(0, 22, 10))  # pasada la medianoche
        self.day_shift.checkin_deadline = None
        self.assertIsNone(checkin.deadline(self.day_shift, self.at(0, 9, 0)))

    def test_on_time_is_present_and_after_deadline_is_late(self):
        from mes_core.models import Attendance

        from . import checkin

        self.ana.shift = self.day_shift
        self.ana.save(update_fields=["shift"])
        self.beto.shift = self.day_shift
        self.beto.save(update_fields=["shift"])
        info = checkin.info(self.ana, self.at(0, 6, 10))
        self.assertFalse(info["late"])
        checkin.confirm(self.ana, now=self.at(0, 6, 10))
        checkin.confirm(self.beto, now=self.at(0, 6, 40))
        a = Attendance.objects.get(employee=self.ana, day=self.at(0, 6, 10).date())
        b = Attendance.objects.get(employee=self.beto, day=self.at(0, 6, 40).date())
        self.assertEqual(a.status, Attendance.Status.PRESENT)
        self.assertEqual(b.status, Attendance.Status.LATE)
        self.assertIn("hora máxima 06:15", b.notes)

    def test_shift_settings_saves_the_deadline(self):
        from django.contrib.auth import get_user_model

        u = get_user_model().objects.create_superuser("jefe", password="x")
        self.client.force_login(u)
        r = self.client.post(reverse("mes_core:shift_settings") + f"?site={self.site.pk}",
                             {"action": "checkin", f"ci_{self.day_shift.pk}": "06:20", f"ci_{self.night.pk}": ""})
        self.assertEqual(r.status_code, 302)
        self.day_shift.refresh_from_db()
        self.night.refresh_from_db()
        self.assertEqual(f"{self.day_shift.checkin_deadline:%H:%M}", "06:20")
        self.assertIsNone(self.night.checkin_deadline)
        page = self.client.get(reverse("mes_core:shift_settings") + f"?site={self.site.pk}").content.decode()
        self.assertIn("Llegada a la terminal", page)
