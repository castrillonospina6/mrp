from decimal import Decimal

from django.test import TestCase

from mes_core import services
from mes_core.models import Product, Site

from .models import Lot, YieldStandard, yield_analysis


class YieldTests(TestCase):
    def test_yield_uses_input_lots_and_flags_deviation(self):
        site = Site.objects.create(code="P", name="Planta", kind=Site.Kind.FOOD)
        chicken = Product.objects.create(sku="POL", name="Pollo", unit="KG")
        breast = Product.objects.create(sku="PEC", name="Pechuga", unit="KG")
        YieldStandard.objects.create(input_product=chicken, output_product=breast,
                                     expected_percent=Decimal("24"), tolerance_percent=Decimal("2"))
        order = services.create_order(site=site, product=chicken, process_type="deshuese", quantity=1000)
        Lot.objects.create(code="E1", product=chicken, order=order, direction="entrada", quantity=Decimal("800"))
        Lot.objects.create(code="S1", product=breast, order=order, direction="salida", quantity=Decimal("160"))

        (line,) = yield_analysis(order)
        self.assertEqual(line.expected_kg, Decimal("192.000"))  # 24% de 800 kg reales, no de 1000 planeados
        self.assertEqual(line.actual_percent, Decimal("20.00"))
        self.assertTrue(line.out_of_tolerance)
