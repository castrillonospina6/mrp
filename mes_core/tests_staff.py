"""Personal: normalización, carga masiva desde Excel, supervisores, asistencia y cuadrillas."""

import io
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from . import staffing
from .models import (
    Attendance, CrewAssignment, Employee, EmployeeSkill, Operation, Product, ScheduleChange, Shift, ShiftAttendance, Site,
    Skill, WorkCenter, WorkOrder,
)
from .normalize import LevelError, display_name, normalize_document, normalize_key, normalize_text, parse_level


def xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Operarios"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class NormalizeTests(TestCase):
    def test_text(self):
        self.assertEqual(normalize_text("  JOSÉ   Pérez Muñoz "), "jose perez muñoz")
        self.assertEqual(normalize_text("ÁÉÍÓÚ ü Ñ"), "aeiou u ñ")
        self.assertEqual(normalize_text("línea 1\n\n  línea   2", keep_newlines=True), "linea 1\nlinea 2")
        self.assertEqual(normalize_text(None), "")

    def test_key_and_document(self):
        self.assertEqual(normalize_key("Control_de  CALIDAD."), "control de calidad")
        self.assertEqual(normalize_document("1.020.345.678"), "1020345678")
        self.assertEqual(normalize_document(79123456.0), "79123456")
        self.assertEqual(normalize_document("pa-12 34"), "PA1234")

    def test_level(self):
        self.assertEqual([parse_level(v) for v in ("EXPERTO", " Avanzado ", "medio", "básico", 1, 3.0)], [3, 3, 2, 1, 1, 3])
        self.assertIsNone(parse_level("Ninguno"))
        with self.assertRaises(LevelError):
            parse_level("muy bueno")

    def test_display_name(self):
        self.assertEqual(display_name("maria de la cruz"), "Maria de la Cruz")

    def test_models_normalize_on_save(self):
        site = Site.objects.create(code="P1", name="Planta")
        e = Employee.objects.create(site=site, document="1.234.567", full_name="  ANDRÉS   Peña ", position="OPERARIO")
        self.assertEqual((e.document, e.full_name, e.position), ("1234567", "andres peña", "operario"))
        self.assertEqual(e.display_name, "Andres Peña")
        self.assertEqual(Skill.objects.create(name=" Montacargas ").name, "montacargas")


class StaffBase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.site = Site.objects.create(code="CEDI", name="Centro", operators_total=30)
        self.morning = Shift.objects.create(site=self.site, name="Mañana", start_time=time(6), end_time=time(14))
        self.afternoon = Shift.objects.create(site=self.site, name="Tarde", start_time=time(14), end_time=time(22))
        self.wc = WorkCenter.objects.create(site=self.site, code="ETQ-1", name="Etiquetado 1", kind="etiquetado", capacity=2)
        self.product = Product.objects.create(sku="CAM-1", name="Camiseta")
        self.order = WorkOrder.objects.create(site=self.site, product=self.product, process_type="x", quantity=Decimal("100"),
                                              status=WorkOrder.Status.PLANNED)
        self.day = timezone.localdate() + timedelta(days=1)
        tz = timezone.get_current_timezone()
        self.op = Operation.objects.create(
            order=self.order, sequence=1, name="Etiquetado", work_center=self.wc, work_center_kind="etiquetado", crew=3,
            planned_start=datetime.combine(self.day, time(8), tzinfo=tz), planned_end=datetime.combine(self.day, time(12), tzinfo=tz))
        self.planner = User.objects.create_user("plan", password="x", first_name="Laura")
        self.planner.groups.add(Group.objects.get(name="Planeación"))
        self.julian = User.objects.create_user("julian", password="x", first_name="Julián", last_name="Ríos")
        self.julian.groups.add(Group.objects.get(name="Supervisor"))
        self.other_sup = User.objects.create_user("marta", password="x", first_name="Marta")
        self.other_sup.groups.add(Group.objects.get(name="Supervisor"))
        self.skill = Skill.objects.create(name="Etiquetado")
        self.emps = []
        for i, (name, level) in enumerate([("ana", 3), ("beto", 1), ("carla", 2), ("dario", None), ("elena", 3)]):
            e = Employee.objects.create(site=self.site, document=str(1000 + i), full_name=name, shift=self.morning,
                                        supervisor=self.julian)
            if level:
                EmployeeSkill.objects.create(employee=e, skill=self.skill, level=level)
            self.emps.append(e)


class ImportTests(StaffBase):
    def test_import_creates_updates_and_levels(self):
        Employee.objects.filter(document="1000").update(position="")
        raw = xlsx([
            ["LISTADO DE PERSONAL"],  # título encima de los encabezados
            [],
            ["N°", "Cédula", "NOMBRE COMPLETO", "Cargo", "Turno", "Supervisor", "Montacargas", "ETIQUETADO"],
            [1, "1.000", "ANA GÓMEZ", "Líder", "mañana", "JULIAN", "Experto", "novato"],
            [2, 2000123.0, "pedro   pérez", "", "TARDE", "Marta", "", "3"],
        ])
        res = staffing.import_employees(raw, "personal.xlsx", self.site, user=self.planner)
        self.assertTrue(res.ok, res.errors)
        self.assertEqual((res.created, res.updated), (1, 1))
        ana = Employee.objects.get(document="1000")
        self.assertEqual((ana.full_name, ana.position, ana.shift, ana.supervisor), ("ana gomez", "lider", self.morning, self.julian))
        self.assertEqual(ana.skills.get(skill__name="montacargas").level, 3)
        self.assertEqual(ana.skills.get(skill=self.skill).level, 1)  # «ETIQUETADO» = la competencia existente
        pedro = Employee.objects.get(document="2000123")
        self.assertEqual((pedro.full_name, pedro.shift, pedro.supervisor), ("pedro perez", self.afternoon, self.other_sup))
        self.assertIn("montacargas", res.new_skills)
        self.assertTrue(ScheduleChange.objects.filter(action=ScheduleChange.Action.STAFF).exists())

    def test_import_is_all_or_nothing(self):
        raw = xlsx([
            ["documento", "nombre", "turno", "picking"],
            ["555", "Nuevo Uno", "Mañana", "experto"],
            ["556", "", "Noche", "muy bueno"],
            ["555", "Repetido", "", ""],
        ])
        res = staffing.import_employees(raw, "x.xlsx", self.site)
        self.assertFalse(res.ok)
        text = " ".join(res.errors)
        self.assertIn("Fila 3", text)
        self.assertIn("falta el nombre", text)
        self.assertIn("turno «noche» no existe", text)
        self.assertIn("no reconocido", text)
        self.assertIn("repetido", text)
        self.assertFalse(Employee.objects.filter(document="555").exists())
        self.assertFalse(Skill.objects.filter(name="picking").exists())

    def test_import_csv_and_missing_headers(self):
        raw = "Documento;Nombres;Apellidos;Activo\n777;LUZ;MARÍA TORO;no\n".encode("cp1252")
        res = staffing.import_employees(raw, "lista.csv", self.site)
        self.assertTrue(res.ok, res.errors)
        luz = Employee.objects.get(document="777")
        self.assertEqual(luz.full_name, "luz maria toro")
        self.assertFalse(luz.active)
        bad = staffing.import_employees(xlsx([["a", "b"], [1, 2]]), "x.xlsx", self.site)
        self.assertIn("encabezados", bad.errors[0])

    def test_template_roundtrip(self):
        data = staffing.template_xlsx(self.site)
        wb = load_workbook(io.BytesIO(data))
        self.assertEqual(wb.sheetnames, ["Operarios", "Instrucciones"])
        res = staffing.import_employees(data, "plantilla.xlsx", self.site)
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(res.created, 2)  # las dos filas de ejemplo
        self.assertEqual(Employee.objects.get(document="79123456").full_name, "julian perez")

    def test_admin_import_and_template_download(self):
        root = get_user_model().objects.create_superuser("root", password="x")
        self.client.force_login(root)
        r = self.client.get(reverse("admin:mes_core_employee_template"), {"site": self.site.pk})
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])
        url = reverse("admin:mes_core_employee_import")
        self.assertContains(self.client.get(url), "Descarga la plantilla")
        self.assertContains(self.client.get(reverse("admin:mes_core_employee_changelist")), "Cargar desde Excel")
        up = SimpleUploadedFile("p.xlsx", xlsx([["documento", "nombre"], ["9", "X"], ["", "sin doc"]]))
        r = self.client.post(url, {"site": self.site.pk, "file": up})
        self.assertContains(r, "falta el documento")
        self.assertFalse(Employee.objects.filter(document="9").exists())
        up = SimpleUploadedFile("p.xlsx", xlsx([["documento", "nombre"], ["9", "X"]]))
        r = self.client.post(url, {"site": self.site.pk, "file": up})
        self.assertRedirects(r, reverse("admin:mes_core_employee_changelist"), fetch_redirect_response=False)
        self.assertTrue(Employee.objects.filter(document="9").exists())
        # En la aplicación ya no se carga personal: lleva al panel de administración
        r = self.client.get(reverse("mes_core:employee_create"), {"site": self.site.pk})
        self.assertRedirects(r, f"{reverse('admin:mes_core_employee_add')}?site={self.site.pk}", fetch_redirect_response=False)
        self.client.force_login(self.planner)  # sin acceso al panel: no carga
        self.assertEqual(self.client.get(reverse("mes_core:employee_create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("mes_core:employee_import"), {"site": self.site.pk}).status_code, 403)
        self.assertNotContains(self.client.get(reverse("mes_core:employee_list"), {"site": self.site.pk}), "Cargar personal")


class SupervisorFlowTests(StaffBase):
    def test_planner_assigns_supervisor(self):
        self.client.force_login(self.planner)
        url = reverse("mes_core:supervisor_assign")
        r = self.client.get(url, {"site": self.site.pk, "desde": self.day.isoformat()})
        self.assertContains(r, "Etiquetado")
        self.assertContains(r, "Julián Ríos")
        r = self.client.post(url, {"site": self.site.pk, "op": [self.op.pk], f"sup_{self.op.pk}": self.julian.pk})
        self.assertEqual(r.status_code, 302)
        self.op.refresh_from_db()
        self.assertEqual((self.op.supervisor, self.op.supervisor_assigned_by), (self.julian, self.planner))
        with self.assertRaises(ValidationError):
            staffing.assign_supervisor([self.op], self.planner)  # no es supervisor

    def test_attendance_syncs_capacity_absences(self):
        entries = [staffing.AttendanceEntry(self.emps[0], "presente"), staffing.AttendanceEntry(self.emps[1], "ausente"),
                   staffing.AttendanceEntry(self.emps[2], "incapacidad"), staffing.AttendanceEntry(self.emps[3], "tarde")]
        counts = staffing.save_attendance(self.day, entries, user=self.julian)
        self.assertEqual(counts["saved"], 4)
        self.assertEqual(ShiftAttendance.objects.get(shift=self.morning, day=self.day).absent, 2)
        staffing.save_attendance(self.day, [staffing.AttendanceEntry(self.emps[1], "")], user=self.julian)
        self.assertEqual(ShiftAttendance.objects.get(shift=self.morning, day=self.day).absent, 1)

    def test_supervisor_home_and_attendance_post(self):
        self.op.supervisor = self.julian
        self.op.save()
        self.client.force_login(self.julian)
        url = reverse("mes_core:supervision_home")
        r = self.client.get(url, {"site": self.site.pk, "day": self.day.isoformat()})
        self.assertContains(r, "Mi turno")
        self.assertContains(r, "Ana")
        self.assertContains(r, "Armar cuadrilla")
        r = self.client.post(url, {"site": self.site.pk, "day": self.day.isoformat(), "emp": [e.pk for e in self.emps],
                                   f"st_{self.emps[0].pk}": "presente", f"st_{self.emps[1].pk}": "ausente",
                                   f"nt_{self.emps[1].pk}": "  cita   médica "})
        self.assertEqual(r.status_code, 302)
        a = Attendance.objects.get(employee=self.emps[1], day=self.day)
        self.assertEqual((a.status, a.notes, a.confirmed_by, a.shift), ("ausente", "cita médica", self.julian, self.morning))
        self.assertEqual(Attendance.objects.filter(day=self.day).count(), 2)  # los demás quedan sin confirmar

    def test_crew_rules(self):
        self.op.supervisor = self.julian
        self.op.save()
        ana, beto, carla, dario, elena = self.emps
        staffing.save_attendance(self.day, [staffing.AttendanceEntry(beto, "ausente"),
                                            staffing.AttendanceEntry(ana, "presente"),
                                            staffing.AttendanceEntry(elena, "presente")], user=self.julian)
        with self.assertRaisesMessage(ValidationError, "Beto está marcado «Ausente»"):
            staffing.set_crew(self.op, [ana.pk, beto.pk], user=self.julian)
        self.assertEqual(staffing.set_crew(self.op, [ana.pk, carla.pk], user=self.julian), (2, 0))
        # Otra operación a la misma hora no puede usar a Ana
        tz = timezone.get_current_timezone()
        op2 = Operation.objects.create(order=self.order, sequence=2, name="Empaque", crew=2,
                                       planned_start=datetime.combine(self.day, time(10), tzinfo=tz),
                                       planned_end=datetime.combine(self.day, time(13), tzinfo=tz))
        with self.assertRaisesMessage(ValidationError, "ya está en"):
            staffing.set_crew(op2, [ana.pk], user=self.julian)
        staffing.set_crew(op2, [elena.pk], user=self.julian)
        # Retirar y cambiar
        self.assertEqual(staffing.set_crew(self.op, [carla.pk, dario.pk], user=self.julian), (1, 1))
        self.assertEqual(set(self.op.crew_assignments.values_list("employee_id", flat=True)), {carla.pk, dario.pk})
        # Personal general: alguien de otra planta habitual (o sin planta) también se puede asignar
        other = Site.objects.create(code="P2", name="Otra")
        foreign = Employee.objects.create(site=other, document="X1", full_name="zoe")
        loose = Employee.objects.create(document="X2", full_name="yuri")
        self.assertEqual(staffing.set_crew(self.op, [carla.pk, dario.pk, foreign.pk, loose.pk], user=self.julian), (2, 0))

    def test_crew_gives_people_to_the_supervisor_until_the_operation_ends(self):
        self.op.supervisor = self.other_sup  # Marta arma la cuadrilla de su operación
        self.op.save()
        felipe = Employee.objects.create(document="F1", full_name="felipe")       # libre (sin supervisor)
        ana = self.emps[0]                                                         # equipo habitual de Julián
        staffing.set_crew(self.op, [felipe.pk, ana.pk], user=self.other_sup)
        felipe.refresh_from_db()
        ana.refresh_from_db()
        self.assertEqual((felipe.supervisor, felipe.crew_borrowed, felipe.crew_operation), (self.other_sup, True, self.op))
        self.assertEqual((ana.supervisor, ana.home_supervisor), (self.other_sup, self.julian))
        # si planeación le cambia el supervisor a la operación, la cuadrilla pasa con el nuevo
        staffing.assign_supervisor([self.op], self.julian, user=self.planner)
        felipe.refresh_from_db()
        self.assertEqual(felipe.supervisor, self.julian)
        staffing.assign_supervisor([self.op], self.other_sup, user=self.planner)
        # sacarlo de la cuadrilla lo deja libre otra vez
        staffing.set_crew(self.op, [ana.pk], user=self.other_sup)
        felipe.refresh_from_db()
        self.assertEqual((felipe.supervisor, felipe.crew_borrowed, felipe.crew_operation), (None, False, None))
        # al terminar la operación, Ana vuelve con su supervisor de siempre
        self.op.status = Operation.Status.DONE
        self.op.save(update_fields=["status"])
        ana.refresh_from_db()
        self.assertEqual((ana.supervisor, ana.crew_borrowed, ana.home_supervisor), (self.julian, False, None))

    def test_freed_person_goes_to_the_next_supervisor_that_picks_them(self):
        tz = timezone.get_current_timezone()
        self.op.supervisor = self.other_sup
        self.op.save()
        op_b = Operation.objects.create(order=self.order, sequence=2, name="Empaque", crew=1, supervisor=self.julian,
                                        planned_start=datetime.combine(self.day, time(13), tzinfo=tz),
                                        planned_end=datetime.combine(self.day, time(15), tzinfo=tz))
        felipe = Employee.objects.create(document="F1", full_name="felipe")
        staffing.set_crew(self.op, [felipe.pk], user=self.other_sup)       # Marta (8-12)
        felipe.refresh_from_db()
        self.assertEqual(felipe.supervisor, self.other_sup)
        self.op.status = Operation.Status.DONE
        self.op.save(update_fields=["status"])
        felipe.refresh_from_db()
        self.assertEqual((felipe.supervisor, felipe.crew_borrowed), (None, False))   # libre
        staffing.set_crew(op_b, [felipe.pk], user=self.julian)              # ahora lo elige Julián
        felipe.refresh_from_db()
        self.assertEqual((felipe.supervisor, felipe.crew_operation), (self.julian, op_b))
        op_b.status = Operation.Status.DONE
        op_b.save(update_fields=["status"])
        felipe.refresh_from_db()
        self.assertEqual(felipe.supervisor, None)

    def test_crew_release_sweep_and_manual_change(self):
        self.op.supervisor = self.other_sup
        self.op.save()
        felipe = Employee.objects.create(document="F1", full_name="felipe")
        gina = Employee.objects.create(document="G1", full_name="gina")
        staffing.set_crew(self.op, [felipe.pk, gina.pk], user=self.other_sup)
        # cambio a mano: Gina se queda con Marta aunque termine la operación
        self.client.force_login(self.planner)
        self.client.post(reverse("mes_core:employee_list"), {"ids": [gina.pk], "set_supervisor": self.other_sup.pk})
        # el pedido se cierra sin pasar por save() (p. ej. una actualización masiva): la revisión automática libera
        Operation.objects.filter(pk=self.op.pk).update(status=Operation.Status.DONE)
        self.assertEqual(staffing.sync_crew_supervisors(), 1)
        felipe.refresh_from_db()
        gina.refresh_from_db()
        self.assertEqual((felipe.supervisor, felipe.crew_borrowed), (None, False))
        self.assertEqual((gina.supervisor, gina.crew_borrowed), (self.other_sup, False))

    def test_candidates_ranked_by_level_and_skill_match(self):
        self.assertEqual(staffing.match_skill(self.op), self.skill)
        staffing.save_attendance(self.day, [staffing.AttendanceEntry(e, "presente") for e in self.emps[:3]] +
                                 [staffing.AttendanceEntry(self.emps[4], "vacaciones")])
        rows = staffing.crew_candidates(self.op, self.skill, self.julian)
        names = [c.employee.full_name for c in rows]
        # presentes por nivel (ana E, carla I, beto N), luego sin confirmar (dario) y al final el bloqueado (elena)
        self.assertEqual(names, ["ana", "carla", "beto", "dario", "elena"])
        self.assertEqual(rows[-1].blocked_reason, "Vacaciones")

    def test_crew_page_permissions_and_save(self):
        self.op.supervisor = self.other_sup
        self.op.save()
        url = reverse("mes_core:crew_assign", args=[self.op.pk])
        self.client.force_login(self.julian)
        self.assertEqual(self.client.get(url).status_code, 403)  # es de Marta
        self.op.supervisor = self.julian
        self.op.save()
        r = self.client.get(url)
        self.assertContains(r, "Planeación pide <b>3 operarios</b>")
        self.assertContains(r, "Nivel en etiquetado")
        r = self.client.post(url, {"emp": [self.emps[0].pk, self.emps[2].pk]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(CrewAssignment.objects.filter(operation=self.op).count(), 2)

    def test_matrix_and_api(self):
        self.client.force_login(self.julian)
        r = self.client.get(reverse("mes_core:skills_matrix"), {"site": self.site.pk})
        self.assertContains(r, "Matriz de polivalencia")
        self.assertContains(r, "E 2")  # dos expertos en etiquetado
        api = reverse("mes_core:api_skill_level")
        r = self.client.post(api, {"employee": self.emps[3].pk, "skill": self.skill.pk, "level": 2}, content_type="application/json")
        self.assertEqual(r.json()["label"], "Intermedio")
        self.client.post(api, {"employee": self.emps[3].pk, "skill": self.skill.pk, "level": ""}, content_type="application/json")
        self.assertFalse(EmployeeSkill.objects.filter(employee=self.emps[3]).exists())
        r = self.client.post(reverse("mes_core:skills_matrix"), {"site": self.site.pk, "name": "  MONTACARGAS "})
        self.assertTrue(Skill.objects.filter(name="montacargas").exists())

    def test_menu_by_role(self):
        self.client.force_login(self.julian)
        r = self.client.get(reverse("mes_core:order_list"))
        self.assertContains(r, reverse("mes_core:supervision_home"))
        self.assertNotContains(r, reverse("mes_core:supervisor_assign"))
        self.assertEqual(self.client.get(reverse("mes_core:supervisor_assign")).status_code, 403)
        self.assertEqual(self.client.get(reverse("mes_core:employee_create")).status_code, 403)  # no carga personal
        self.client.force_login(self.planner)
        r = self.client.get(reverse("mes_core:order_list"))
        self.assertContains(r, reverse("mes_core:supervisor_assign"))
        self.assertContains(r, reverse("mes_core:employee_list"))

    def test_plant_view_with_unsupervised_operation(self):
        Attendance.objects.create(employee=self.emps[0], day=self.day, status="presente")  # sin confirmado_por
        self.client.force_login(self.planner)
        r = self.client.get(reverse("mes_core:supervision_home"), {"site": self.site.pk, "day": self.day.isoformat(),
                                                                   "ops": "todas"})
        self.assertContains(r, "sin supervisor")
        self.assertContains(r, "Con —")  # presente sin supervisor que lo haya marcado (dato antiguo)

    def test_general_roster_each_supervisor_marks_their_own(self):
        """Lista general: Marta ve a todo el personal (también al equipo habitual de Julián) y marca a los que tiene."""
        other = Site.objects.create(code="P2", name="Otra")
        far = Employee.objects.create(site=other, document="7000", full_name="xiomara")  # otra planta habitual
        self.client.force_login(self.other_sup)
        url = reverse("mes_core:supervision_home")
        r = self.client.get(url, {"site": self.site.pk, "day": self.day.isoformat()})
        self.assertContains(r, "Ana")       # equipo habitual de Julián
        self.assertContains(r, "Xiomara")   # otra planta habitual
        self.assertContains(r, "lista general")
        ids = [e.pk for e in self.emps] + [far.pk]
        r = self.client.post(url, {"site": self.site.pk, "day": self.day.isoformat(), "emp": ids,
                                   f"st_{self.emps[0].pk}": "presente", f"st_{far.pk}": "presente"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(set(Attendance.objects.filter(day=self.day, confirmed_by=self.other_sup)
                             .values_list("employee_id", flat=True)), {self.emps[0].pk, far.pk})
        r = self.client.get(url, {"site": self.site.pk, "day": self.day.isoformat(), "lista": "mios"})
        self.assertContains(r, "Xiomara")
        self.assertNotContains(r, "Beto")
        self.assertContains(r, "Contigo")
        # Julián guarda su lista sin tocar a Ana: no se la quita a Marta
        self.client.force_login(self.julian)
        r = self.client.get(url, {"site": self.site.pk, "day": self.day.isoformat()})
        self.assertContains(r, "Con Marta")
        self.client.post(url, {"site": self.site.pk, "day": self.day.isoformat(), "emp": ids,
                               f"st_{self.emps[0].pk}": "presente", f"st_{self.emps[1].pk}": "presente"})
        self.assertEqual(Attendance.objects.get(employee=self.emps[0], day=self.day).confirmed_by, self.other_sup)
        self.assertEqual(Attendance.objects.get(employee=self.emps[1], day=self.day).confirmed_by, self.julian)
        r = self.client.get(url, {"site": self.site.pk, "day": self.day.isoformat(), "lista": "pendientes"})
        self.assertContains(r, "Carla")
        self.assertNotContains(r, "Xiomara")
        # En la cuadrilla, los que Marta tiene presentes con ella salen primero para ella
        rows = staffing.crew_candidates(self.op, self.skill, self.other_sup)
        self.assertTrue(next(c for c in rows if c.employee == far).mine)

    def test_replan_keeps_supervisor_per_stage(self):
        from . import stage_planning

        tz = timezone.get_current_timezone()
        stage = {"name": "Etiquetado", "line": self.wc.pk, "quantity": 100, "std_value": 0.5, "std_unit": "min_unidad",
                 "setup_minutes": 0, "mode": "turno", "qty_mode": "todo"}
        start = datetime.combine(self.day, time(6), tzinfo=tz)
        stage_planning.save_plan(self.order, [stage], start)
        op = self.order.operations.get()
        staffing.assign_supervisor([op], self.julian)
        staffing.set_crew(op, [self.emps[0].pk])
        stage_planning.save_plan(self.order, [{**stage, "quantity": 80}], start)
        new = self.order.operations.get()
        self.assertNotEqual(new.pk, op.pk)
        self.assertEqual(new.supervisor, self.julian)
        self.assertFalse(new.crew_assignments.exists())
        last = ScheduleChange.objects.filter(action=ScheduleChange.Action.PLAN).order_by("-pk").first()  # misma hora en Windows
        self.assertIn("cuadrillas por rearmar", last.description)

    def test_supervisor_cannot_load_or_edit_staff(self):
        marta_emp = Employee.objects.create(site=self.site, document="9000", full_name="ofelia", supervisor=self.other_sup)
        julian = get_user_model().objects.get(pk=self.julian.pk)
        self.assertFalse(julian.has_perm("mes_core.add_employee") or julian.has_perm("mes_core.change_employee"))
        self.client.force_login(self.julian)
        self.assertEqual(self.client.get(reverse("mes_core:employee_create")).status_code, 403)
        up = SimpleUploadedFile("p.xlsx", xlsx([["documento", "nombre"], ["555", "Uno"]]))
        self.assertEqual(self.client.post(reverse("mes_core:employee_import"), {"site": self.site.pk, "file": up}).status_code, 403)
        self.assertFalse(Employee.objects.filter(document="555").exists())
        self.assertEqual(self.client.get(reverse("mes_core:employee_edit", args=[self.emps[0].pk])).status_code, 403)
        self.assertNotEqual(self.client.get(reverse("admin:mes_core_employee_import")).status_code, 200)  # sin panel
        url = reverse("mes_core:employee_list")
        r = self.client.post(url, {"site": self.site.pk, "ids": [self.emps[0].pk], "set_shift": self.afternoon.pk})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Employee.objects.get(pk=self.emps[0].pk).shift, self.morning)
        # Ve su equipo (sin acciones de carga ni edición) y mantiene la polivalencia de los suyos
        r = self.client.get(url, {"site": self.site.pk})
        self.assertContains(r, "Mi equipo")
        self.assertNotContains(r, "Cargar personal")
        self.assertNotContains(r, 'name="ids"')
        self.assertNotContains(r, "Ofelia")
        self.assertContains(self.client.get(url, {"site": self.site.pk, "sup": "all"}), "Ofelia")
        api = reverse("mes_core:api_skill_level")
        r = self.client.post(api, {"employee": self.emps[3].pk, "skill": self.skill.pk, "level": 2}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        r = self.client.post(api, {"employee": marta_emp.pk, "skill": self.skill.pk, "level": 3}, content_type="application/json")
        self.assertEqual(r.status_code, 403)

    def test_import_team_scope_service(self):
        Employee.objects.create(site=self.site, document="9000", full_name="ofelia", supervisor=self.other_sup)
        res = staffing.import_employees(xlsx([["documento", "nombre", "supervisor", "etiquetado"],
                                              ["555", "Uno", "", "experto"], ["556", "Dos", "julian", "novato"]]),
                                        "p.xlsx", self.site, team_of=self.julian)
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(set(Employee.objects.filter(document__in=["555", "556"]).values_list("supervisor", flat=True)),
                         {self.julian.pk})
        res = staffing.import_employees(xlsx([["documento", "nombre", "supervisor"],
                                              ["9000", "Ofelia", ""], ["557", "Tres", "marta"]]),
                                        "p.xlsx", self.site, team_of=self.julian)
        self.assertTrue(any("ya está en el equipo de Marta" in e for e in res.errors))
        self.assertTrue(any("solo puedes cargar operarios a tu equipo" in e for e in res.errors))
        self.assertFalse(Employee.objects.filter(document="557").exists())
        tpl = load_workbook(io.BytesIO(staffing.template_xlsx(self.site, team_of=self.julian)))
        self.assertEqual(tpl["Operarios"]["E2"].value, "julian")

    def test_employee_list_bulk_and_form(self):
        self.client.force_login(self.planner)
        url = reverse("mes_core:employee_list")
        r = self.client.get(url, {"site": self.site.pk, "q": "ANÁ"})
        self.assertContains(r, "Ana")
        self.assertNotContains(r, "Beto")
        r = self.client.post(url, {"site": self.site.pk, "ids": [self.emps[0].pk, self.emps[1].pk],
                                   "set_supervisor": self.other_sup.pk, "set_shift": self.afternoon.pk})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Employee.objects.filter(supervisor=self.other_sup, shift=self.afternoon).count(), 2)
        r = self.client.post(reverse("mes_core:employee_edit", args=[self.emps[3].pk]), {
            "site": self.site.pk, "document": "52.111.222", "full_name": "SOFÍA  LÓPEZ", "position": "Auxiliar",
            "shift": self.morning.pk, "supervisor": self.julian.pk, "active": "on",
            "new_skill_1": "Control de CALIDAD", "new_level_1": "3"})
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        sofia = Employee.objects.get(document="52111222")
        self.assertEqual(sofia.full_name, "sofia lopez")
        self.assertEqual(sofia.skills.get().skill.name, "control de calidad")
        r = self.client.post(reverse("mes_core:employee_edit", args=[self.emps[4].pk]),
                             {"site": self.site.pk, "document": "52111222", "full_name": "otra", "active": "on"})
        self.assertContains(r, "Ya existe un operario")


class EmployeeAdminTests(StaffBase):
    """Panel de administración: desplegables en la lista y asignaciones masivas."""

    def setUp(self):
        super().setUp()
        self.root = get_user_model().objects.create_superuser("root", password="x")
        self.client.force_login(self.root)
        self.url = reverse("admin:mes_core_employee_changelist")

    def _act(self, action, ids, **data):
        payload = {"action": action, "_selected_action": [str(i) for i in ids], **data}
        return self.client.post(self.url, payload)

    def test_inline_dropdowns(self):
        r = self.client.get(self.url, {"site__id__exact": self.site.pk})
        self.assertContains(r, 'name="form-0-supervisor"')
        self.assertContains(r, 'name="form-0-shift"')
        self.assertContains(r, ">Julián Ríos</option>")
        self.assertContains(r, ">CEDI · Tarde (14:00-22:00)</option>")
        self.assertNotContains(r, ">plan</option>")  # planeación no es supervisor
        # Guardar desde la lista sin entrar a cada operario
        emps = list(Employee.objects.filter(site=self.site).order_by("full_name"))
        data = {"form-TOTAL_FORMS": str(len(emps)), "form-INITIAL_FORMS": str(len(emps)), "_save": "Guardar"}
        for i, e in enumerate(emps):
            data.update({f"form-{i}-id": e.pk, f"form-{i}-shift": e.shift_id or "", f"form-{i}-supervisor": e.supervisor_id or "",
                         f"form-{i}-active": "on"})
        data["form-0-supervisor"] = self.other_sup.pk
        data["form-0-shift"] = self.afternoon.pk
        r = self.client.post(f"{self.url}?site__id__exact={self.site.pk}", data)
        self.assertEqual(r.status_code, 302)
        emps[0].refresh_from_db()
        self.assertEqual((emps[0].supervisor, emps[0].shift), (self.other_sup, self.afternoon))
        self.assertEqual(Employee.objects.filter(supervisor=self.julian).count(), 4)

    def test_bulk_supervisor_and_shift(self):
        ids = [e.pk for e in self.emps[:3]]
        r = self._act("bulk_supervisor", ids)
        self.assertContains(r, "Aplicar a 3 operarios")
        r = self._act("bulk_supervisor", ids, apply="1", supervisor=self.other_sup.pk)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Employee.objects.filter(supervisor=self.other_sup).count(), 3)
        self.assertTrue(ScheduleChange.objects.filter(description__contains="supervisor Marta").exists())
        self._act("bulk_supervisor", ids[:1], apply="1", supervisor="")
        self.assertIsNone(Employee.objects.get(pk=ids[0]).supervisor)
        # Turno: el personal es general, cualquier turno de cualquier planta aplica a todos
        other = Site.objects.create(code="P2", name="Otra")
        night = Shift.objects.create(site=other, name="Noche", start_time=time(22), end_time=time(6))
        loose = Employee.objects.create(document="X1", full_name="zoe")  # sin planta
        r = self._act("bulk_shift", ids + [loose.pk])
        self.assertContains(r, "P2 · Noche")
        self._act("bulk_shift", ids + [loose.pk], apply="1", shift=night.pk)
        self.assertEqual(Employee.objects.filter(shift=night).count(), 4)

    def test_bulk_skill_and_select_across(self):
        ids = [e.pk for e in self.emps]
        self._act("bulk_skill", ids, apply="1", skill="", new_skill="  MONTACARGAS ", level="2")
        mont = Skill.objects.get(name="montacargas")
        self.assertEqual(EmployeeSkill.objects.filter(skill=mont, level=2).count(), 5)
        self._act("bulk_skill", ids[:2], apply="1", skill=self.skill.pk, level="3")
        self.assertEqual(list(EmployeeSkill.objects.filter(skill=self.skill, employee_id__in=ids[:2])
                              .values_list("level", flat=True)), [3, 3])
        self._act("bulk_skill", ids[:1], apply="1", skill=mont.pk, level="0")
        self.assertFalse(EmployeeSkill.objects.filter(skill=mont, employee_id=ids[0]).exists())
        r = self._act("bulk_skill", ids, apply="1", skill="", new_skill="", level="2")
        self.assertContains(r, "Elige una competencia")
        # «Seleccionar todos» (todas las páginas): aplica a todo lo filtrado
        r = self.client.post(f"{self.url}?site__id__exact={self.site.pk}",
                             {"action": "bulk_supervisor", "_selected_action": [str(ids[0])], "select_across": "1",
                              "apply": "1", "supervisor": self.other_sup.pk})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Employee.objects.filter(site=self.site, supervisor=self.other_sup).count(), 5)

    def test_roles_after_migrate(self):
        sup = Group.objects.get(name="Supervisor")
        codes = set(sup.permissions.values_list("codename", flat=True))
        self.assertFalse({"add_employee", "change_employee"} & codes)
        self.assertTrue({"supervise_crew", "add_crewassignment", "change_employeeskill"} <= codes)
