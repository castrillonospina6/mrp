from decimal import Decimal

from django.test import TestCase

from mes_core import services
from mes_core.models import Product, Site

from .models import PackagingMaterial, PackagingSpec, material_requirements


class MaterialRequirementsTests(TestCase):
    def test_requirements_include_waste_and_boxes(self):
        site = Site.objects.create(code="S", name="S")
        shirt = Product.objects.create(sku="CAM", name="Camiseta")
        label = Product.objects.create(sku="ETQ", name="Etiqueta", kind="insumo")
        box = Product.objects.create(sku="CJA", name="Caja", kind="insumo")
        spec = PackagingSpec.objects.create(product=shirt, units_per_box=24)
        PackagingMaterial.objects.create(spec=spec, kind="etiqueta", material=label, waste_percent=Decimal("2"))
        PackagingMaterial.objects.create(spec=spec, kind="caja", material=box)

        order = services.create_order(site=site, product=shirt, process_type="acondicionado", quantity=100)
        req = {r.material.sku: r.quantity for r in material_requirements(order)}
        self.assertEqual(req["ETQ"], Decimal("102"))
        self.assertEqual(req["CJA"], Decimal("5"))  # 100 / 24 = 4.17 -> 5 cajas
