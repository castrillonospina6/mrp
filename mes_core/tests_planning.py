"""Pruebas de: configuración de turnos (descansos), recepción de pedidos y planeación por etapas."""

import json
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from . import scheduling, services, stage_planning
from .models import Operation, PlanStage, Product, Shift, Site, WorkCenter, WorkOrder
from .shift_config import apply_shift_config, build_shift_specs

TZ = ZoneInfo("America/Bogota")


def day_shift(site, **kw):
    """06:00-14:00 con desayuno 09:00-09:15 y almuerzo 12:00-12:30 → 7,25 h hábiles."""
    return Shift.objects.create(site=site, name=kw.pop("name", "Mañana"), start_time=time(6), end_time=time(14),
                                breakfast_start=time(9), breakfast_end=time(9, 15),
                                lunch_start=time(12), lunch_end=time(12, 30), **kw)


class BreaksAndShiftConfigTests(TestCase):
    def test_breaks_are_not_working_time(self):
        sh = Shift(name="M", start_time=time(6), end_time=time(14), breakfast_start=time(9), breakfast_end=time(9, 15),
                   lunch_start=time(12), lunch_end=time(12, 30))
        self.assertEqual(sh.effective_minutes, 435)
        wins = scheduling.shift_work_windows(sh, date(2026, 1, 5), TZ)
        self.assertEqual([(a.hour, a.minute, b.hour, b.minute) for a, b in wins],
                         [(6, 0, 9, 0), (9, 15, 12, 0), (12, 30, 14, 0)])
        # 4 h de trabajo desde las 08:00 saltan el desayuno y el almuerzo
        _, e = scheduling.fit(datetime(2026, 1, 5, 8, 0, tzinfo=TZ), 240, [sh])
        self.assertEqual(e, datetime(2026, 1, 5, 12, 45, tzinfo=TZ))

    def test_build_specs_three_shifts(self):
        specs = build_shift_specs(first_start=time(6), duration_minutes=480, count=3,
                                  breakfast=(time(9), time(9, 15)), lunch=(time(12), time(12, 30)), operators=[20, 15, 8])
        self.assertEqual([s.name for s in specs], ["Mañana", "Tarde", "Noche"])
        self.assertEqual((specs[2].start, specs[2].end), (time(22), time(6)))
        self.assertEqual(specs[1].lunch, (time(20), time(20, 30)))
        self.assertEqual(specs[2].breakfast, (time(1), time(1, 15)))  # cruza medianoche
        self.assertEqual([s.operators for s in specs], [20, 15, 8])
        self.assertEqual(specs[0].effective_minutes, 435)

    def test_build_specs_validation(self):
        with self.assertRaises(ValidationError):  # 3 × 10 h > 24 h
            build_shift_specs(first_start=time(6), duration_minutes=600, count=3, breakfast=None, lunch=None, operators=[0])
        with self.assertRaises(ValidationError):  # desayuno fuera del turno
            build_shift_specs(first_start=time(6), duration_minutes=480, count=1,
                              breakfast=(time(15), time(15, 15)), lunch=None, operators=[0])

    def test_apply_keeps_ids_and_deactivates_extra(self):
        site = Site.objects.create(code="S", name="S")
        a = Shift.objects.create(site=site, name="Mañana", start_time=time(6), end_time=time(14))
        b = Shift.objects.create(site=site, name="Tarde", start_time=time(14), end_time=time(22))
        specs = build_shift_specs(first_start=time(7), duration_minutes=600, count=1, breakfast=None,
                                  lunch=(time(12), time(13)), operators=[12])
        apply_shift_config(site, specs)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.start_time, a.end_time, a.operators, a.effective_minutes), (time(7), time(17), 12, 540))
        self.assertFalse(b.active)

    def test_apply_keeps_line_assignments_by_name(self):
        site = Site.objects.create(code="S", name="S")
        night = Shift.objects.create(site=site, name="Noche", start_time=time(22), end_time=time(6))
        morning = Shift.objects.create(site=site, name="Mañana", start_time=time(6), end_time=time(14))
        line = WorkCenter.objects.create(site=site, code="L", name="L", kind="x")
        line.shifts.set([morning])
        specs = build_shift_specs(first_start=time(6), duration_minutes=480, count=3, breakfast=None, lunch=None,
                                  operators=[5])
        apply_shift_config(site, specs)
        morning.refresh_from_db()
        night.refresh_from_db()
        self.assertEqual((morning.name, morning.start_time), ("Mañana", time(6)))
        self.assertEqual((night.name, night.start_time), ("Noche", time(22)))
        self.assertEqual([s.name for s in line.shifts.all()], ["Mañana"])
        self.assertEqual(site.shifts.filter(active=True).count(), 3)

    def test_settings_page_saves(self):
        site = Site.objects.create(code="S", name="S")
        self.client.force_login(get_user_model().objects.create_superuser("adm", password="p"))
        url = reverse("mes_core:shift_settings") + f"?site={site.pk}"
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.post(url, {"first_start": "06:00", "duration_hours": "8", "count": "2",
                                   "breakfast_start": "09:00", "breakfast_end": "09:15", "lunch_start": "12:00",
                                   "lunch_end": "12:30", "operators_total": "150", "operators_2": "60"})
        self.assertEqual(r.status_code, 302, r.content[:500])
        site.refresh_from_db()
        self.assertEqual(site.operators_total, 150)
        # Mañana sin número propio (usa el total); Tarde con 60
        self.assertEqual(list(site.shifts.order_by("start_time").values_list("name", "operators")),
                         [("Mañana", 0), ("Tarde", 60)])


class SalesAndStagePlanningTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="P", name="Planta")
        self.shift = day_shift(self.site, operators=10)
        self.line = WorkCenter.objects.create(site=self.site, code="ACO", name="Acondicionado", kind="acondicionado")
        self.line2 = WorkCenter.objects.create(site=self.site, code="ENC", name="Encajado", kind="encajado")
        self.product = Product.objects.create(sku="CAM", name="Camiseta")
        self.start = datetime(2026, 11, 16, 6, 0, tzinfo=TZ)  # lunes

    def _order(self, qty=1000, delivery=date(2026, 11, 25), transit=2):
        return services.receive_order(site=self.site, product=self.product, process_type="acondicionado",
                                      quantity=qty, received_on=date(2026, 11, 14), delivery_date=delivery,
                                      transit_days=transit)

    def _stage(self, **kw):
        base = {"name": "Etiquetado", "line": self.line.pk, "quantity": 1000, "std_value": 0.5,
                "std_unit": "min_unidad", "setup_minutes": 0, "mode": "turno", "qty_mode": "todo"}
        return {**base, **kw}

    def test_receive_order_sets_dispatch_and_deadline(self):
        order = self._order()
        self.assertEqual(order.status, WorkOrder.Status.TO_PLAN)
        self.assertEqual(order.dispatch_date, date(2026, 11, 23))
        self.assertEqual(order.due_date, datetime(2026, 11, 23, 6, 0, tzinfo=TZ))
        self.assertFalse(order.operations.exists())
        with self.assertRaises(ValidationError):  # el vehículo tendría que haber salido antes del pedido
            self._order(delivery=date(2026, 11, 15), transit=3)

    def test_modes(self):
        order = self._order()

        def calc(**kw):
            return stage_planning.preview(order, [self._stage(**kw)], self.start)["stages"][0]

        r = calc()  # 1000 × 0,5 min = 8,33 h de trabajo; turno hábil 7,25 h → 2 operarios
        self.assertEqual((r["crew"], r["man_hours"], r["duration_hours"], r["shift_hours"]), (2, 8.33, 4.17, 7.25))
        self.assertEqual(calc(mode="dias", mode_param=2)["crew"], 1)               # 2 días × 7,25 h
        self.assertEqual(calc(mode="operarios", mode_param=4)["duration_hours"], 2.08)
        self.assertEqual(calc(std_value=120, std_unit="unid_hora")["crew"], 2)     # 120 u/h = 0,5 min/u
        self.assertEqual(calc(std_value=2, std_unit="unid_min")["crew"], 2)        # 2 u/min = 0,5 min/u
        r = calc(mode="operarios", mode_param=12)
        self.assertTrue(any("12 operarios" in w for w in r["warnings"]))           # el turno tiene 10

    def test_deadline_mode_meets_due_date(self):
        order = self._order(delivery=date(2026, 11, 18), transit=0)  # listo antes del mié 18 06:00 → 2 turnos
        data = stage_planning.preview(order, [self._stage(quantity=3000, mode="compromiso")], self.start)
        self.assertEqual(data["stages"][0]["crew"], 2)  # 25 h / (2 × 7,25 h) = 1,72 → 2
        self.assertGreaterEqual(data["summary"]["slack_hours"], 0)
        self.assertEqual(data["summary"]["available_hours"], 14.5)

    def test_lots_flow_between_stages(self):
        order = self._order()
        stages = [self._stage(qty_mode="lotes", qty_param=300, mode="operarios", mode_param=2),
                  self._stage(name="Encajado", line=self.line2.pk, std_value=0.1, qty_mode="lotes", qty_param=300,
                              mode="operarios", mode_param=1)]
        first, second = stage_planning.preview(order, stages, self.start)["stages"]
        self.assertEqual([lot["qty"] for lot in first["schedule"]], [300, 300, 300, 100])
        self.assertEqual(second["schedule"][0]["start"], first["schedule"][0]["end"])  # el lote 1 pasa enseguida

    def test_save_plan_creates_operations_partial_and_saldo(self):
        order = self._order()
        stages = [self._stage(qty_mode="parcial", qty_param=400),
                  self._stage(name="Encajado", line=self.line2.pk, std_value=0.1, quantity=400)]
        stage_planning.save_plan(order, stages, self.start)
        order.refresh_from_db()
        ops = list(order.operations.order_by("sequence"))
        self.assertEqual([(o.sequence, o.name, float(o.qty), o.planned_start is not None) for o in ops], [
            (101, "Etiquetado · parcial", 400.0, True), (199, "Etiquetado · saldo", 600.0, False),
            (201, "Encajado", 400.0, True)])
        self.assertEqual(order.plan_stages.count(), 2)
        self.assertEqual(order.status, WorkOrder.Status.DRAFT)  # el saldo queda en el backlog
        self.assertEqual(dict(scheduling.detect_conflicts(ops, [])), {})
        # Re-planear reemplaza todo
        stage_planning.save_plan(order, [self._stage()], self.start)
        order.refresh_from_db()
        self.assertEqual(order.operations.count(), 1)
        self.assertEqual(order.status, WorkOrder.Status.PLANNED)
        self.assertEqual(order.operations.get().crew, 2)

    def test_operator_overload_conflict(self):
        stage_planning.save_plan(self._order(), [self._stage(mode="operarios", mode_param=6)], self.start)
        second = [self._stage(line=self.line2.pk, mode="operarios", mode_param=6)]
        # Por defecto la segunda espera a que se liberen operarios: no hay sobreasignación
        stage_planning.save_plan(self._order(), second, self.start)
        self.assertEqual(scheduling.operator_overloads(list(Operation.objects.all()), [self.shift]), set())
        # Si se pide no esperar, 6 + 6 > 10 disponibles → conflicto en ambas
        stage_planning.save_plan(WorkOrder.objects.latest("pk"), second, self.start, respect_operators=False)
        ops = list(Operation.objects.all())
        self.assertEqual(scheduling.operator_overloads(ops, [self.shift]), {o.pk for o in ops})

    def test_roles_and_views(self):
        User = get_user_model()
        seller = User.objects.create_user("ventas", password="p")
        seller.groups.add(Group.objects.get(name="Ventas"))
        planner = User.objects.create_user("plan", password="p")
        planner.groups.add(Group.objects.get(name="Planeación"))

        self.client.force_login(seller)
        r = self.client.post(reverse("mes_core:order_create"), {
            "site": self.site.pk, "new_customer": "Cliente Nuevo", "product": "cam",  # SKU escrito, sin mayúsculas
            "process_type": "acondicionado", "quantity": "500", "received_on": "2026-11-14",
            "delivery_date": "2026-11-26", "transit_days": "1", "priority": "2"})
        self.assertEqual(r.status_code, 302, r.content[:800])
        order = WorkOrder.objects.get(customer__name="Cliente Nuevo")
        self.assertEqual((order.status, order.created_by, order.product), (WorkOrder.Status.TO_PLAN, seller, self.product))
        self.assertEqual(self.client.get(reverse("mes_core:planning_list")).status_code, 403)

        self.client.force_login(planner)
        self.assertEqual(self.client.get(reverse("mes_core:order_create")).status_code, 403)
        self.assertEqual(self.client.get(reverse("mes_core:planning_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("mes_core:planning_detail", args=[order.pk])).status_code, 200)
        body = {"start": "2026-11-16T06:00", "stages": [self._stage(quantity=500)]}
        post = lambda name, b: self.client.post(reverse(name, args=[order.pk]), json.dumps(b),  # noqa: E731
                                                content_type="application/json")
        r = post("mes_core:api_plan_preview", body)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["stages"][0]["crew"], 1)
        r = post("mes_core:api_plan_save", body)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("/programador/", r.json()["redirect"])
        r = post("mes_core:api_plan_preview", {"start": "2026-11-16T06:00",
                                               "stages": [self._stage(std_value="", setup_minutes=0)]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Etapa 1", r.json()["error"])

    def test_intake_free_text_product_and_optional_fields(self):
        self.client.force_login(get_user_model().objects.create_superuser("adm", password="p"))
        url = reverse("mes_core:order_create")
        base = {"site": self.site.pk, "process_type": "acondicionado", "quantity": "120", "received_on": "2026-11-14",
                "delivery_date": "2026-11-26", "transit_days": "0", "priority": "3"}
        # Solo lo obligatorio: los campos nuevos pueden ir vacíos; el producto por nombre reutiliza el existente
        r = self.client.post(url, {**base, "product": "  camiseta  "})
        self.assertEqual(r.status_code, 302, r.content[:800])
        self.assertEqual(WorkOrder.objects.latest("pk").product, self.product)
        # Producto nuevo escrito a mano + campos nuevos
        r = self.client.post(url, {**base, "product": "Polo piqué azul Talla L", "reference": "REF-9",
                                   "purchase_order": "OC-5521", "external_op": "OP-ERP-77", "shipment": "EMB-03",
                                   "description": "Polo con bordado en pecho"})
        self.assertEqual(r.status_code, 302, r.content[:800])
        order = WorkOrder.objects.latest("pk")
        self.assertEqual((order.product.sku, order.product.name), ("POLO-PIQUE-AZUL-TALLA-L", "Polo piqué azul Talla L"))
        self.assertEqual((order.reference, order.purchase_order, order.external_op, order.shipment, order.description),
                         ("REF-9", "OC-5521", "OP-ERP-77", "EMB-03", "Polo con bordado en pecho"))
        self.assertContains(self.client.get(order.get_absolute_url()), "OC-5521")
        # Si el pedido no es válido no se crea el producto
        r = self.client.post(url, {**base, "product": "Producto fantasma", "delivery_date": "2026-11-10"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Product.objects.filter(name="Producto fantasma").exists())

    def test_resolve_product_unique_sku(self):
        Product.objects.create(sku="GORRA-ROJA", name="Otra cosa")
        product, created = services.resolve_product("Gorra roja")
        self.assertTrue(created)
        self.assertEqual(product.sku, "GORRA-ROJA-2")
        self.assertEqual(services.resolve_product("gorra-roja")[0].name, "Otra cosa")  # coincide por SKU

    def test_scheduler_actions_need_planning_permission(self):
        self.client.force_login(get_user_model().objects.create_user("x", password="p"))
        r = self.client.post(reverse("mes_core:api_auto"), json.dumps({"site": self.site.pk}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 403)
        self.assertIn("permiso", r.json()["error"])

    def test_initial_stages_from_standard_route(self):
        stages = stage_planning.initial_stages(self._order())
        self.assertEqual(stages[1]["name"], "Etiquetado")
        self.assertEqual(stages[0]["line"], self.line.pk)  # «Alistamiento de insumos» va a acondicionado
        self.assertEqual(PlanStage.objects.count(), 0)


class TunnelTests(TestCase):
    """Acceso por el túnel rápido de Cloudflare: host aleatorio *.trycloudflare.com servido por HTTPS."""

    HOST = "subsequent-jokes-coverage-strategy.trycloudflare.com"

    def test_tunnel_host_and_csrf_login(self):
        from django.test import Client

        get_user_model().objects.create_superuser("adm", password="clave-segura-123")
        client = Client(enforce_csrf_checks=True, HTTP_HOST=self.HOST)
        login_url = reverse("admin:login")
        r = client.get(login_url)
        self.assertEqual(r.status_code, 200)  # antes: DisallowedHost
        token = r.cookies["csrftoken"].value
        r = client.post(login_url, {"username": "adm", "password": "clave-segura-123", "csrfmiddlewaretoken": token,
                                    "next": "/"}, HTTP_ORIGIN=f"https://{self.HOST}")
        self.assertEqual(r.status_code, 302)  # login aceptado (sin error CSRF 403)
        self.assertEqual(client.get("/").status_code, 200)

    def test_other_hosts_still_rejected(self):
        self.assertEqual(self.client.get("/", HTTP_HOST="evil.example.com").status_code, 400)


class OperatorPoolTests(TestCase):
    """Bolsa de operarios: 150 en la planta; lo programado se descuenta y se libera al terminar."""

    def setUp(self):
        self.site = Site.objects.create(code="P", name="Planta", operators_total=150)
        self.shift = day_shift(self.site)  # sin número propio → usa los 150
        self.line = WorkCenter.objects.create(site=self.site, code="ACO", name="Acondicionado", kind="acondicionado")
        self.product = Product.objects.create(sku="CAM", name="Camiseta")
        self.start = datetime(2026, 11, 16, 6, 0, tzinfo=TZ)

    def _order(self):
        return services.receive_order(site=self.site, product=self.product, process_type="acondicionado",
                                      quantity=1000, received_on=date(2026, 11, 14), delivery_date=date(2026, 11, 30))

    def _stage(self, **kw):
        return {"name": "Etiquetado", "line": self.line.pk, "quantity": 1000, "std_value": 0.5, "std_unit": "min_unidad",
                "setup_minutes": 0, "mode": "operarios", "mode_param": 10, "qty_mode": "todo", **kw}

    def test_ledger_discounts_and_releases(self):
        stage_planning.save_plan(self._order(), [self._stage()], self.start)  # 10 operarios, 50 min
        ledger = scheduling.OperatorLedger(self.site)
        self.assertEqual(ledger.free_at(datetime(2026, 11, 16, 6, 30, tzinfo=TZ)), 140)
        self.assertEqual(ledger.free_at(datetime(2026, 11, 16, 7, 0, tzinfo=TZ)), 150)
        (rel,) = ledger.releases(self.start, datetime(2026, 11, 16, 14, 0, tzinfo=TZ))
        self.assertEqual((rel["at"], rel["released"], rel["free_after"]),
                         (datetime(2026, 11, 16, 6, 50, tzinfo=TZ), 10, 150))

    def test_by_operators_computes_time_and_reports_pool(self):
        stage_planning.save_plan(self._order(), [self._stage()], self.start)  # otra orden ocupa 10
        data = stage_planning.preview(self._order(), [self._stage(mode_param=25)], self.start)
        r = data["stages"][0]
        self.assertEqual((r["crew"], r["duration_hours"]), (25, 0.33))  # 8,33 h-hombre / 25 = 20 min
        self.assertEqual((r["free_at_start"], r["remaining"], r["free_after"]), (140, 115, 140))
        ops = data["summary"]["operators"]
        self.assertEqual((ops["available"], ops["used_by_others"], ops["free"]), (150, 10, 140))
        self.assertEqual([x["released"] for x in ops["releases"]], [25, 10])  # esta etapa (06:20) y la otra (06:50)

    def test_waits_until_operators_are_released(self):
        self.site.operators_total = 30
        self.site.save()
        stage_planning.save_plan(self._order(), [self._stage(mode_param=25)], self.start)  # 25 ocupados hasta 06:20
        order = self._order()
        r = stage_planning.preview(order, [self._stage(mode_param=10)], self.start)["stages"][0]
        self.assertEqual(r["start"], datetime(2026, 11, 16, 6, 20, tzinfo=TZ).isoformat())  # solo hay 5 libres
        self.assertTrue(any("Espera" in w for w in r["warnings"]))
        r = stage_planning.preview(order, [self._stage(mode_param=10)], self.start, respect_operators=False)["stages"][0]
        self.assertEqual(r["start"], self.start.isoformat())
        self.assertTrue(any("sobreasignada" in w for w in r["warnings"]))

    def test_shift_specific_number_overrides_total(self):
        self.shift.operators = 40
        self.shift.save()
        ledger = scheduling.OperatorLedger(self.site)
        self.assertEqual(ledger.available_at(datetime(2026, 11, 16, 8, 0, tzinfo=TZ)), 40)

    def test_board_operator_row(self):
        stage_planning.save_plan(self._order(), [self._stage()], self.start)
        self.client.force_login(get_user_model().objects.create_superuser("adm", password="p"))
        data = self.client.get(reverse("mes_core:api_board"),
                               {"site": self.site.pk, "view": "dia", "date": "2026-11-16"}).json()
        self.assertEqual(data["operators"]["total"], 150)
        (slot,) = data["operators"]["slots"]
        self.assertEqual((slot["available"], slot["used"], slot["free"]), (150, 10, 140))


class MidnightEndTests(TestCase):
    def test_shift_ending_at_midnight_belongs_to_its_start_day(self):
        tarde = Shift(name="Tarde", start_time=time(15), end_time=time(0))
        manana = Shift(name="Mañana", start_time=time(6), end_time=time(15))
        self.assertFalse(tarde.crosses_midnight)
        a, b = scheduling.shift_window(tarde, date(2026, 11, 17), TZ)
        self.assertEqual((a, b), (datetime(2026, 11, 17, 15, 0, tzinfo=TZ), datetime(2026, 11, 18, 0, 0, tzinfo=TZ)))
        slots = scheduling.build_slots(scheduling.sort_shifts([tarde, manana]), "semana",
                                       scheduling.view_days("semana", date(2026, 11, 17)))
        self.assertEqual([(s.day, s.label) for s in slots[:2]], [(date(2026, 11, 16), "Mañana"), (date(2026, 11, 16), "Tarde")])
        self.assertTrue(Shift(name="Noche", start_time=time(22), end_time=time(6)).crosses_midnight)
