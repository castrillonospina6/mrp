"""Patinador de la operación: se elige al armar la cuadrilla, su app pasa a ser de patinador mientras dure la
operación, y el supernumerario (o la línea, o el supervisor en Traslados) le pide material."""

from django.urls import reverse

from mes_core.models import Employee, Operation, WorkCenter

from .models import FloorAccess, LineCode, Location, MoverAssignment, Transfer
from .tests import FloorBase


class MoverAssignmentTests(FloorBase):
    def setUp(self):
        super().setUp()
        self.carlos = Employee.objects.create(document="3001", full_name="carlos patiño", shift=self.shift)
        acc = FloorAccess(employee=self.carlos)  # acceso de operario: lo vuelve patinador la asignación
        acc.set_pin("1234")
        acc.save()
        self.carla = Employee.objects.create(document="2001", full_name="carla diaz", shift=self.shift, supervisor=self.sup)
        acc = FloorAccess(employee=self.carla, role=FloorAccess.Role.SUPERNUMERARY)
        acc.set_pin("1234")
        acc.save()
        self.bodega = Location.objects.create(site=self.site, name="Bodega", distance=2)
        Location.objects.create(site=self.site, name="Frente L1", work_center=self.wc)

    def crew_post(self, **extra):
        self.client.force_login(self.sup)
        data = {"emp": [str(self.ana.pk), str(self.beto.pk)], "mover_panel": "1", **extra}
        return self.client.post(reverse("mes_core:crew_assign", args=[self.op1.pk]), data, follow=True)

    def test_crew_page_asks_and_assigns_the_mover(self):
        self.client.force_login(self.sup)
        page = self.client.get(reverse("mes_core:crew_assign", args=[self.op1.pk]))
        self.assertContains(page, "¿Requiere patinador?")
        r = self.crew_post(mover_need="1")  # dijo que sí pero no eligió a nadie
        self.assertContains(r, "Elige quién será el patinador")
        self.assertFalse(MoverAssignment.objects.exists())
        r = self.crew_post(mover_need="1", mover=str(self.ana.pk))  # está en la cuadrilla
        self.assertContains(r, "está en la cuadrilla")
        r = self.crew_post(mover_need="1", mover=str(self.carlos.pk))
        self.assertContains(r, "queda como patinador")
        self.assertEqual(MoverAssignment.objects.get().employee, self.carlos)
        self.assertEqual(self.op1.crew_assignments.count(), 2)  # no entra en la cuadrilla
        self.crew_post(mover_need="0")
        self.assertFalse(MoverAssignment.objects.exists())

    def test_assigned_person_sees_the_mover_app_until_the_operation_ends(self):
        self.login("3001")
        self.assertEqual(self.client.get(reverse("piso:api_state")).json()["kind"], "operario")
        MoverAssignment.objects.create(operation=self.op1, employee=self.carlos)
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual(s["kind"], "patinador")
        self.assertEqual(s["assigned"][0]["line"], "L1")
        Operation.objects.filter(pk=self.op1.pk).update(status=Operation.Status.DONE)
        self.assertEqual(self.client.get(reverse("piso:api_state")).json()["kind"], "operario")

    def test_supernumerary_requests_material_to_the_line_mover(self):
        MoverAssignment.objects.create(operation=self.op1, employee=self.carlos)
        LineCode.for_line(self.wc)
        self.login("2001")
        s = self.client.get(reverse("piso:api_state")).json()
        m = s["material"]
        self.assertEqual(m["lines"][0]["default_mover"], self.carlos.pk)
        self.assertIn(self.carlos.pk, [x["id"] for x in m["movers"]])
        r = self.post("piso:api_transfer", {"wc": self.wc.pk, "o": self.bodega.pk, "est": 2, "nota": "cajas"},
                      action="pedir")
        self.assertEqual(r.status_code, 200, r.content)
        t = Transfer.objects.get(pk=r.json()["id"])
        self.assertEqual((t.mover, t.requested_by, t.from_line, t.order), (self.carlos, self.carla, True, self.order))
        r = self.post("piso:api_transfer", {"wc": self.wc.pk, "m": 0}, action="pedir")  # abierto
        self.assertIsNone(Transfer.objects.get(pk=r.json()["id"]).mover)
        r = self.post("piso:api_transfer", {"wc": self.wc.pk, "m": self.ana.pk}, action="pedir")  # no es patinador
        self.assertEqual(r.status_code, 409)
        other = WorkCenter.objects.create(site=self.site, code="L9", name="Otra", kind="empaque")
        self.assertEqual(self.post("piso:api_transfer", {"wc": other.pk}, action="pedir").status_code, 403)
        # el patinador ve el pedido en su cola, con quién lo pidió
        self.client.post(reverse("piso:api_logout"), "{}", content_type="application/json")
        self.login("3001")
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual((s["current"]["by"], s["current"]["pallets"]), ("Carla", 2))

    def test_line_tablet_and_office_use_the_assigned_mover(self):
        MoverAssignment.objects.create(operation=self.op1, employee=self.carlos)
        lc = LineCode.for_line(self.wc)
        self.post("piso:api_pair", {"code": lc.pair_code})
        s = self.client.get(reverse("piso:api_state")).json()
        self.assertEqual(s["default_mover"], self.carlos.pk)
        r = self.post("piso:api_transfer", {"est": 1}, action="pedir")
        self.assertEqual(Transfer.objects.get(pk=r.json()["id"]).mover, self.carlos)
        # en Traslados (MES) el supervisor también se lo puede asignar
        t = Transfer.objects.create(site=self.site, origin=self.bodega, pallets=1)
        self.client.force_login(self.sup)
        page = self.client.get(reverse("piso:transfers"), {"site": self.site.pk})
        self.assertContains(page, "patinador de L1")
        self.client.post(reverse("piso:transfers") + f"?site={self.site.pk}",
                         {"a": "asignar", "id": t.pk, "pat": self.carlos.pk})
        t.refresh_from_db()
        self.assertEqual(t.mover, self.carlos)
