"""Carga datos de ejemplo: un centro logístico y una planta de alimentos.

Uso:
  python manage.py seed_demo           # maestros idempotentes; órdenes solo si no hay
  python manage.py seed_demo --reset   # borra órdenes, lotes, paros y bitácora y vuelve a crear la demo
Nunca toca usuarios.
"""

from datetime import time, timedelta
from decimal import Decimal

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from mes_core import planning, services
from mes_core.models import (Customer, Downtime, Product, ScheduleChange, Shift, ShiftAttendance, Site, WorkCenter,
                             WorkOrder)
from mes_core.plugins import registry

SHIFTS = [("Noche", time(22), time(6), "#5b5bd6"), ("Mañana", time(6), time(14), "#2f80ed"),
          ("Tarde", time(14), time(22), "#16a39a")]
# Desayuno a las 3 h y almuerzo a las 6 h de iniciar cada turno.
BREAKS = {"Noche": ((time(1), time(1, 15)), (time(4), time(4, 30))),
          "Mañana": ((time(9), time(9, 15)), (time(12), time(12, 30))),
          "Tarde": ((time(17), time(17, 15)), (time(20), time(20, 30)))}
OPERATORS_TOTAL = {"CEDI-01": 150, "PAL-01": 60}  # bolsa de operarios por planta


class Command(BaseCommand):
    help = "Carga datos de demostración (plantas, turnos, líneas, clientes, productos, órdenes)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Borra órdenes y datos operativos antes de cargar.")

    def _seed_employees(self, site, count, skill_names, shifts):
        """Operarios de ejemplo con su matriz de polivalencia (solo si la planta aún no tiene operarios).
        No crea supervisores: se asignan después a usuarios con el rol Supervisor."""
        from mes_core.models import Employee, EmployeeSkill, Skill

        if Employee.objects.filter(site=site).exists():
            return
        first = ["Ana", "Luis", "Carolina", "Jorge", "Paola", "Andrés", "Diana", "Camilo", "Natalia", "Felipe", "Sandra",
                 "Julián", "Valentina", "Óscar", "Lina", "Mauricio", "Erika", "Sebastián"]
        last = ["Gómez", "Rodríguez", "Martínez", "López", "Ramírez", "Torres", "Muñoz", "Restrepo", "Castaño", "Peña",
                "Ríos", "Cárdenas"]
        skills = [Skill.objects.get_or_create(name=n)[0] for n in skill_names]
        shift_cycle = [shifts[(site.code, "Mañana")], shifts[(site.code, "Tarde")]]
        base_doc = 1_010_000_000 if site.code == "CEDI-01" else 1_020_000_000
        for i in range(count):
            emp = Employee.objects.create(
                site=site, document=str(base_doc + i * 7919), position="operario" if i % 6 else "lider de linea",
                full_name=f"{first[i % len(first)]} {last[(i * 5) % len(last)]} {last[(i * 7 + 3) % len(last)]}",
                shift=shift_cycle[i % 2])
            for j, skill in enumerate(skills):
                level = (i * 3 + j * 2) % 5  # 0 = no la tiene; 1..3 = novato..experto (4 → experto)
                if level:
                    EmployeeSkill.objects.create(employee=emp, skill=skill, level=min(level, 3))
        self.stdout.write(f"{count} operarios de ejemplo en {site.code} (asígnales supervisor en Personal → Operarios).")

    @transaction.atomic
    def handle(self, *args, reset=False, **options):
        if reset:
            if apps.is_installed("plugins.food"):
                from plugins.food.models import Lot
                Lot.objects.all().delete()
            WorkOrder.objects.all().delete()
            Downtime.objects.all().delete()
            ScheduleChange.objects.all().delete()
            self.stdout.write("Datos operativos borrados.")

        cedi, _ = Site.objects.get_or_create(code="CEDI-01", defaults={"name": "Centro logístico", "kind": Site.Kind.LOGISTICS})
        planta, _ = Site.objects.get_or_create(code="PAL-01", defaults={"name": "Planta de alimentos", "kind": Site.Kind.FOOD})
        shifts = {}
        for site in (cedi, planta):
            if not site.operators_total:
                site.operators_total = OPERATORS_TOTAL[site.code]
                site.save(update_fields=["operators_total"])
            for name, start, end, color in SHIFTS:
                sh, _ = Shift.objects.get_or_create(
                    site=site, name=name, defaults={"start_time": start, "end_time": end, "color": color,
                                                    "weekdays": "012345"})  # lunes a sábado
                if sh.breakfast_start is None:  # solo si nadie lo ha configurado aún
                    (sh.breakfast_start, sh.breakfast_end), (sh.lunch_start, sh.lunch_end) = BREAKS[name]
                    sh.save()
                shifts[(site.code, name)] = sh

        def wc(site, code, name, kind, capacity, shift_names=()):
            obj, created = WorkCenter.objects.get_or_create(site=site, code=code,
                                                            defaults=dict(name=name, kind=kind, capacity=capacity))
            if created and shift_names:
                obj.shifts.set([shifts[(site.code, n)] for n in shift_names])

        wc(cedi, "PICK", "Zona de picking", "picking", 6)
        wc(cedi, "VER", "Mesa de verificación", "verificacion", 2, ("Mañana", "Tarde"))
        wc(cedi, "MUE", "Muelles de despacho", "despacho", 2)
        wc(cedi, "ACO", "Mesas de acondicionado", "acondicionado", 4, ("Mañana", "Tarde"))
        wc(cedi, "ETQ", "Etiquetadoras", "etiquetado", 2, ("Mañana", "Tarde"))
        wc(cedi, "ENC", "Línea de encajado", "encajado", 1, ("Mañana", "Tarde"))
        wc(planta, "REC", "Recepción y báscula", "recepcion", 1, ("Noche", "Mañana"))
        wc(planta, "DSH", "Mesas de deshuese", "deshuese", 8, ("Mañana", "Tarde"))
        wc(planta, "POR", "Porcionadoras", "porcionado", 2, ("Mañana", "Tarde"))
        wc(planta, "EMP", "Empacadora al vacío", "empaque", 2, ("Mañana", "Tarde"))

        customers = {}
        for name, tier in [("Almacenes Éxito", "vip"), ("Tiendas D1", "oro"), ("Textiles Andinos", "plata"),
                           ("Restaurantes La Brasa", "oro"), ("Minimercados del Valle", "bronce")]:
            customers[name], _ = Customer.objects.get_or_create(name=name, defaults={"tier": tier})

        def prod(sku, name, unit=Product.Unit.UNIT, kind=Product.Kind.PRODUCT):
            return Product.objects.get_or_create(sku=sku, defaults={"name": name, "unit": unit, "kind": kind})[0]

        def insumo(sku, name):
            return prod(sku, name, kind=Product.Kind.SUPPLY)

        camiseta = prod("CAM-001", "Camiseta básica talla M")
        jean = prod("JEA-032", "Jean clásico talla 32")
        etiqueta = insumo("ETQ-001", "Etiqueta de precio")
        gancho = insumo("GCH-001", "Gancho plástico")
        bolsa = insumo("BLS-001", "Bolsa individual")
        caja = insumo("CJA-024", "Caja corrugada x24")
        pedido = prod("PED-MIX", "Pedido mixto e-commerce")
        pollo = prod("POL-ENT", "Pollo entero en canal", Product.Unit.KG)
        cortes = {"PEC-001": ("Pechuga deshuesada", "24.0"), "CMU-001": ("Contramuslo deshuesado", "20.0"),
                  "ALA-001": ("Alas", "10.0"), "CAR-001": ("Carcasa", "30.0")}

        if apps.is_installed("plugins.copacking"):
            from plugins.copacking.models import PackagingMaterial as PM, PackagingSpec

            for product in (camiseta, jean):
                spec, created = PackagingSpec.objects.get_or_create(product=product, defaults={"units_per_box": 24})
                if created:
                    for kind, mat, waste in [(PM.Kind.LABEL, etiqueta, "2"), (PM.Kind.HOOK, gancho, "1"),
                                             (PM.Kind.BAG, bolsa, "1.5"), (PM.Kind.BOX, caja, "0")]:
                        PM.objects.create(spec=spec, kind=kind, material=mat, qty_per_unit=Decimal("1"),
                                          waste_percent=Decimal(waste))

        if apps.is_installed("plugins.food"):
            from plugins.food.models import YieldStandard

            for sku, (name, pct) in cortes.items():
                out = prod(sku, name, Product.Unit.KG)
                YieldStandard.objects.get_or_create(input_product=pollo, output_product=out,
                                                    defaults={"expected_percent": Decimal(pct)})

        self._seed_employees(cedi, 36, ("picking", "verificacion", "despacho", "acondicionado", "etiquetado", "encajado",
                                        "montacargas"), shifts)
        self._seed_employees(planta, 18, ("recepcion", "deshuese", "porcionado", "empaque", "bpm"), shifts)

        if WorkOrder.objects.exists():
            self.stdout.write("Ya hay órdenes; no se crean nuevas (usa --reset para recrear la demo).")
            return

        now = timezone.now()
        c = customers

        def order(site, product, ptype, qty, prio, due_days, ref, customer):
            if registry.get_process_type(ptype) is None:
                return None  # el plugin de ese proceso está deshabilitado
            return services.create_order(site=site, product=product, process_type=ptype, quantity=qty, priority=prio,
                                         due_date=now + timedelta(days=due_days), reference=ref, customer=customer)

        planned = [
            order(cedi, pedido, "picking", 800, 1, 1, "Ola 1 e-commerce", c["Almacenes Éxito"]),
            order(cedi, pedido, "picking", 1500, 2, 2, "Ola 2 tiendas", c["Tiendas D1"]),
            order(cedi, pedido, "picking", 600, 3, 3, "Ola 3 tiendas", c["Minimercados del Valle"]),
            order(cedi, camiseta, "acondicionado", 2400, 2, 2, "Temporada", c["Textiles Andinos"]),
            order(cedi, jean, "acondicionado", 3600, 3, 4, "Temporada", c["Textiles Andinos"]),
            order(cedi, camiseta, "acondicionado", 1200, 1, 1, "Promoción", c["Almacenes Éxito"]),
            order(planta, pollo, "deshuese", 1200, 1, 1, "Proveedor A", c["Restaurantes La Brasa"]),
            order(planta, pollo, "deshuese", 900, 2, 2, "Proveedor B", c["Almacenes Éxito"]),
            order(planta, pollo, "deshuese", 700, 3, 3, "Proveedor A", c["Minimercados del Valle"]),
        ]
        planned = [o for o in planned if o]

        week_start = timezone.localtime(now).replace(hour=6, minute=0, second=0, microsecond=0)
        if (cedi_ret := WorkCenter.objects.filter(site=cedi, kind="encajado").first()):
            Downtime.objects.create(work_center=cedi_ret, reason="Mantenimiento preventivo",
                                    start=week_start + timedelta(days=1), end=week_start + timedelta(days=1, hours=4))
        if (dsh := WorkCenter.objects.filter(site=planta, kind="deshuese").first()):
            Downtime.objects.create(work_center=dsh, reason="Sanitización profunda",
                                    start=week_start + timedelta(days=2, hours=8), end=week_start + timedelta(days=2, hours=12))

        for site in (cedi, planta):
            result = planning.generate_plan(site)
            self.stdout.write(f"Plan {site.code}: {result.scheduled_operations} operaciones, "
                              f"{len(result.late_orders)} órdenes atrasadas.")

        # Variedad de estados: liberar algunas y arrancar una en cada planta.
        for o in planned[:2] + planned[6:7]:
            services.release_order(o)
        for o in (planned[0], planned[6]):
            first = o.operations.order_by("sequence").first()
            services.report_production(first, qty_good=o.quantity / 3)

        # Órdenes nuevas que quedan en el backlog para programar arrastrando.
        order(cedi, jean, "acondicionado", 960, 2, 5, "Reposición", c["Tiendas D1"])
        order(cedi, pedido, "picking", 400, 1, 2, "Pedido urgente", c["Almacenes Éxito"])
        order(planta, pollo, "deshuese", 500, 2, 4, "Proveedor C", c["Restaurantes La Brasa"])

        # Flujo ventas → planeación: pedidos recibidos por ventas (sin etapas) …
        today = timezone.localdate()
        if registry.get_process_type("acondicionado"):
            services.receive_order(site=cedi, product=camiseta, process_type="acondicionado", quantity=6000,
                                   received_on=today, delivery_date=today + timedelta(days=9), transit_days=2,
                                   customer=c["Almacenes Éxito"], reference="PO-88213", priority=2,
                                   notes="Etiqueta de precio + gancho + bolsa individual; cajas x24.")
            sold = services.receive_order(site=cedi, product=jean, process_type="acondicionado", quantity=2400,
                                          received_on=today, delivery_date=today + timedelta(days=6), transit_days=1,
                                          customer=c["Tiendas D1"], reference="PO-4471")
            # … y uno ya planeado por etapas, con lotes que fluyen de una etapa a la siguiente.
            from mes_core import stage_planning

            aco = WorkCenter.objects.get(site=cedi, code="ACO")
            etq = WorkCenter.objects.get(site=cedi, code="ETQ")
            enc = WorkCenter.objects.get(site=cedi, code="ENC")
            start = (timezone.localtime(now) + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
            lots = {"qty_mode": "lotes", "qty_param": 600}
            stage_planning.save_plan(sold, [
                {"name": "Etiquetado", "line": etq.pk, "quantity": 2400, "std_value": 240, "std_unit": "unid_hora",
                 "setup_minutes": 10, "mode": "operarios", "mode_param": 3, **lots},
                {"name": "Gancho y embolsado", "line": aco.pk, "quantity": 2400, "std_value": 0.25,
                 "std_unit": "min_unidad", "setup_minutes": 5, "mode": "turno", **lots},
                {"name": "Encajado", "line": enc.pk, "quantity": 2400, "std_value": 6, "std_unit": "unid_min",
                 "setup_minutes": 5, "mode": "operarios", "mode_param": 1, **lots},
            ], start)

        # --- Planeación avanzada: familias, reglas, calendario, historial y versión aprobada ---
        from mes_core import versions, workcal
        from mes_core.models import CalendarException, ChangeoverTime, PlanningRules

        for prod_, fam in ((camiseta, "Camisetas"), (jean, "Jeans"), (pedido, "E-commerce"), (pollo, "Pollo")):
            if not prod_.family:
                prod_.family = fam
                prod_.save(update_fields=["family"])
        for site in (cedi, planta):
            PlanningRules.objects.get_or_create(site=site, defaults={"default_changeover_minutes": 15})
            for year in (today.year, today.year + 1):
                workcal.load_colombia_holidays(site, year)
        ChangeoverTime.objects.get_or_create(site=cedi, work_center_kind="etiquetado", from_family="Camisetas",
                                             to_family="Jeans", defaults={"minutes": 25})
        ChangeoverTime.objects.get_or_create(site=cedi, work_center_kind="etiquetado", from_family="Jeans",
                                             to_family="Camisetas", defaults={"minutes": 20})
        tarde = shifts[("CEDI-01", "Tarde")]
        friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
        sunday = friday + timedelta(days=2)
        CalendarException.objects.get_or_create(site=cedi, kind="horas_extra", date=friday, shift=tarde,
                                                defaults={"start_time": time(22), "end_time": time(0), "operators": 30})
        CalendarException.objects.get_or_create(site=cedi, kind="turno_adicional", date=sunday,
                                                defaults={"name": "Domingo de temporada", "start_time": time(6),
                                                          "end_time": time(14), "operators": 40})
        workcal.invalidate()
        # 60 semanas de historial (pedidos terminados) para el pronóstico, con temporada alta en diciembre
        import math as _m
        monday = today - timedelta(days=today.weekday())
        for w in range(1, 61):
            wk = monday - timedelta(weeks=w)
            season = 1.4 if wk.month in (11, 12) else 1.0
            for ptype, prod_, base, cust in (("picking", pedido, 1800, c["Tiendas D1"]),
                                             ("acondicionado", camiseta, 2600, c["Textiles Andinos"])):
                if registry.get_process_type(ptype) is None:
                    continue
                qty = round(base * season * (1 + 0.15 * _m.sin(w / 3)))
                WorkOrder.objects.create(site=cedi, product=prod_, process_type=ptype, quantity=qty, status=WorkOrder.Status.DONE,
                                         received_on=wk + timedelta(days=w % 5), customer=cust, reference="Histórico",
                                         quantity_done=qty)
        v = versions.snapshot(cedi, name="Plan de la semana", note="Versión de demostración", propose=True)
        versions.approve(v, note="Aprobado para la demo", release=False)

        # Estado de recursos y ausencias para el tablero de capacidad
        for i, o in enumerate(WorkOrder.objects.order_by("pk")):
            o.goods_pct, o.supplies_pct = [(100, 100), (100, 80), (90, 100), (100, 60), (70, 50)][i % 5]
            o.save(update_fields=["goods_pct", "supplies_pct"])
        for site, absent in ((cedi, 12), (planta, 5)):
            for name in ("Mañana", "Tarde"):
                ShiftAttendance.objects.get_or_create(shift=shifts[(site.code, name)], day=today,
                                                      defaults={"absent": absent, "notes": "Demo: incapacidades y vacaciones"})

        self.stdout.write(self.style.SUCCESS("Datos de demostración cargados."))
