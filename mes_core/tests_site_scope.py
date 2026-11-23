"""«Todas las bodegas»: botones de bodega con memoria por sesión y vistas que suman todas las plantas."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from . import site_scope
from .models import Site, WorkCenter


class _Req:
    def __init__(self, get=None, session=None):
        self.GET, self.POST, self.session = get or {}, {}, session if session is not None else {}


class SiteScopeTests(TestCase):
    def setUp(self):
        self.a = Site.objects.create(code="A1", name="Bodega A")
        self.b = Site.objects.create(code="B2", name="Bodega B")
        WorkCenter.objects.create(site=self.b, code="L1", name="Línea 1")

    def test_defaults_and_memory(self):
        s = {}
        self.assertTrue(site_scope.pick(_Req(session=s)).all)                       # sin elegir: Todas
        self.assertEqual(site_scope.pick(_Req(session=s), allow_all=False).site, self.b)  # una sola: la más usada
        self.assertEqual(site_scope.pick(_Req({"site": str(self.a.pk)}, s)).site, self.a)
        self.assertEqual(site_scope.pick(_Req(session=s)).site, self.a)              # se recuerda
        self.assertEqual(site_scope.pick(_Req(session=s), allow_all=False).site, self.a)
        site_scope.pick(_Req({"site": "todas"}, s))
        self.assertTrue(site_scope.pick(_Req(session=s)).all)
        # elegir bodega en una pantalla de una sola no le quita «Todas» a las generales
        site_scope.pick(_Req({"site": str(self.b.pk)}, s), allow_all=False)
        self.assertTrue(site_scope.pick(_Req(session=s)).all)
        self.assertEqual(site_scope.pick(_Req({"site": "todas"}, s), allow_all=False).site, self.b)

    def test_pages_with_all_and_chips(self):
        u = get_user_model().objects.create_superuser("jefe", password="x")
        self.client.force_login(u)
        page = self.client.get(reverse("mes_core:dashboard") + "?site=todas").content.decode()
        self.assertIn("Capacidad por bodega", page)
        self.assertIn('class="site-chips"', page)
        self.assertIn("Bodega B", page)
        for name in ("mes_core:order_list", "mes_core:supervision_home", "mes_core:supervisor_assign",
                     "mes_core:at_risk", "mes_core:goods_queue", "mes_core:supplies_queue"):
            r = self.client.get(reverse(name) + "?site=todas")
            self.assertEqual(r.status_code, 200, name)
            self.assertContains(r, ">Todas<", msg_prefix=name)
        r = self.client.get(reverse("mes_core:shift_settings"))
        self.assertEqual(r.context["site"], self.b)
        self.assertNotContains(r, ">Todas<")
