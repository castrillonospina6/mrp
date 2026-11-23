"""Pruebas del tablero de planta en vivo."""

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Downtime, Operation, Product, Shift, Site, WorkCenter, WorkOrder
from .plant_board import plant_board_all, plant_board_data

TZ = ZoneInfo("America/Bogota")
NOW = datetime(2026, 11, 17, 10, 0, tzinfo=TZ)  # martes, turno Mañana 06:00–14:00


class PlantBoardTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="CEDI", name="Centro", operators_total=50)
        self.morning = Shift.objects.create(site=self.site, name="Mañana", start_time=time(6), end_time=time(14))
        self.night = Shift.objects.create(site=self.site, name="Noche", start_time=time(22), end_time=time(6))
        self.product = Product.objects.create(sku="CAM", name="Camiseta")

        def line(code, kind="etiquetado", shifts=()):
            wc = WorkCenter.objects.create(site=self.site, code=code, name=f"Línea {code}", kind=kind)
            if shifts:
                wc.shifts.set(shifts)
            return wc

        self.run, self.asg, self.free = line("A"), line("B"), line("C")
        self.maint, self.off = line("D"), line("E", shifts=[self.night])

    def _op(self, wc, start, end, *, status=Operation.Status.PENDING, seq=1, **kw):
        order = WorkOrder.objects.create(site=self.site, product=self.product, process_type="acondicionado",
                                         quantity=Decimal("1000"), status=WorkOrder.Status.PLANNED)
        return Operation.objects.create(order=order, sequence=seq, name="Etiquetado", work_center_kind=wc.kind if wc else "etiquetado",
                                        work_center=wc, planned_start=start, planned_end=end, status=status,
                                        minutes_per_unit=Decimal("0.6"), crew=kw.pop("crew", 2), **kw)

    def _lines(self, now=NOW):
        return {line["code"]: line for line in plant_board_data(self.site, now)["lines"]}

    def test_states(self):
        self._op(self.run, NOW - timedelta(hours=3), NOW + timedelta(hours=2), status=Operation.Status.IN_PROGRESS,
                 actual_start=NOW - timedelta(hours=2), qty_good=Decimal("180"), qty_scrap=Decimal("20"))
        self._op(self.asg, NOW - timedelta(minutes=30), NOW + timedelta(hours=1))
        self._op(self.free, NOW + timedelta(hours=2), NOW + timedelta(hours=3))  # siguiente, aún no
        self._op(None, None, None)  # en el backlog, sin programar
        Downtime.objects.create(work_center=self.maint, start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1),
                                reason="Limpieza")
        data = plant_board_data(self.site, NOW)
        lines = {line["code"]: line for line in data["lines"]}

        self.assertEqual({c: lines[c]["state"] for c in "ABCDE"}, {
            "A": "rodando", "B": "asignada", "C": "por_asignar", "D": "mantenimiento", "E": "fuera_turno"})
        self.assertEqual(data["counts"], {"rodando": 1, "asignada": 1, "mantenimiento": 1, "por_asignar": 1, "fuera_turno": 1})
        self.assertEqual(data["shift"]["name"], "Mañana")
        self.assertEqual(data["stations"], 5)
        self.assertEqual(data["downtime_minutes"], 120)  # paro dentro del turno
        # Por asignar: muestra la siguiente orden y lo que está en el backlog de su tipo
        self.assertIsNone(lines["C"]["main"])
        self.assertEqual(lines["C"]["next"]["start"], "12:00")
        self.assertEqual(lines["C"]["backlog"], 1)
        # Rodando: avance y calidad
        a = lines["A"]
        self.assertEqual(a["main"]["qty_pct"], 20)
        self.assertEqual(a["kpi"]["qual"], 90)
        # 120 min × 2 operarios ÷ 0,6 min/u = 400 esperadas; 200 reportadas → 50 %
        self.assertEqual(a["kpi"]["perf"], 50)
        self.assertEqual(a["kpi"]["avail"], 100)
        # Mantenimiento: 1 h de paro en 4 h transcurridas → 75 %
        self.assertEqual(lines["D"]["kpi"]["avail"], 75)
        self.assertIsNone(lines["C"]["kpi"]["perf"])
        # Operarios: 2 rodando + 2 asignados de 50
        self.assertEqual(data["operators"], {"available": 50, "used": 4, "free": 46})

    def test_outside_shifts(self):
        data = plant_board_data(self.site, datetime(2026, 11, 17, 18, 0, tzinfo=TZ))  # entre Mañana y Noche
        self.assertIsNone(data["shift"])
        self.assertEqual(data["next_shift"], {"name": "Noche", "start": "22:00"})
        self.assertTrue(all(line["state"] == "fuera_turno" for line in data["lines"]))

    def test_night_line_is_on_shift_at_night(self):
        lines = self._lines(datetime(2026, 11, 17, 23, 0, tzinfo=TZ))
        self.assertEqual(lines["E"]["state"], "por_asignar")
        self.assertEqual(lines["A"]["state"], "por_asignar")  # sin turnos propios = todos los de la planta

    def test_page_and_api(self):
        user = get_user_model().objects.create_user("op", password="x")
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("mes_core:plant_board")), "Tablero de planta")
        r = self.client.get(reverse("mes_core:api_plant_board"), {"site": self.site.pk})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["lines"]), 5)

    def test_all_sites(self):
        other = Site.objects.create(code="FRIO", name="Fríos", operators_total=10)
        Shift.objects.create(site=other, name="Tarde", start_time=time(14), end_time=time(22))
        WorkCenter.objects.create(site=other, code="F1", name="Cámara 1", kind="picking")
        Site.objects.create(code="VACIA", name="Sin líneas")
        self._op(self.run, NOW - timedelta(hours=3), NOW + timedelta(hours=2), status=Operation.Status.IN_PROGRESS)
        data = plant_board_all(list(Site.objects.order_by("code")), NOW)
        self.assertTrue(data["all"])
        self.assertEqual([g["site"]["code"] for g in data["groups"]], ["CEDI", "FRIO"])  # sin la bodega vacía
        self.assertEqual(data["groups"][0]["shift"]["name"], "Mañana")
        self.assertIsNone(data["groups"][1]["shift"])  # a las 10:00 Fríos está fuera de turno
        self.assertEqual(data["stations"], 6)
        self.assertEqual(sum(data["counts"].values()), 6)
        self.assertEqual(data["counts"]["rodando"], 1)
        self.assertEqual({line["site"] for line in data["lines"]}, {"CEDI", "FRIO"})
        user = get_user_model().objects.create_user("op", password="x")
        self.client.force_login(user)
        page = self.client.get(reverse("mes_core:plant_board"), {"site": "todas"})
        self.assertContains(page, ">Todas<")
        self.assertContains(page, 'data-site="todas"')
        r = self.client.get(reverse("mes_core:api_plant_board"), {"site": "todas"})
        self.assertEqual((r.status_code, len(r.json()["lines"]), r.json()["all"]), (200, 6, True))
