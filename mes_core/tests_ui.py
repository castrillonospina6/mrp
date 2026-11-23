"""Pruebas de la interfaz: acceso, menú lateral, Gantt y lista de pedidos."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from .models import Customer, Product, Site, WorkOrder


class AccessTests(TestCase):
    def test_anonymous_goes_to_login_page(self):
        r = self.client.get("/")
        self.assertRedirects(r, f"{reverse('login')}?next=/", fetch_redirect_response=False)
        page = self.client.get(reverse("login"))
        self.assertContains(page, "Iniciar sesión")
        self.assertContains(page, 'name="username"')

    def test_login_and_logout(self):
        get_user_model().objects.create_user("ana", password="clave-segura-123")
        bad = self.client.post(reverse("login"), {"username": "ana", "password": "mala"})
        self.assertContains(bad, "Usuario o contraseña incorrectos")
        r = self.client.post(reverse("login"), {"username": "ana", "password": "clave-segura-123", "next": "/"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        r = self.client.post(reverse("logout"))
        self.assertRedirects(r, reverse("login"), fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 302)


class ShellTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="CEDI", name="Centro", operators_total=10)
        User = get_user_model()
        self.viewer = User.objects.create_user("ver", password="x")
        self.planner = User.objects.create_user("plan", password="x", first_name="Laura")
        self.planner.groups.add(Group.objects.get(name="Planeación"))

    def test_sidebar_depends_on_permissions(self):
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("mes_core:order_list"))
        self.assertContains(r, 'class="sidebar"')
        self.assertNotContains(r, reverse("mes_core:scheduler"))  # sin permisos no ve lo que no usa
        self.assertNotContains(r, reverse("mes_core:planning_list"))
        self.assertNotContains(r, "Administración")
        self.client.force_login(self.planner)
        r = self.client.get(reverse("mes_core:order_list"))
        self.assertContains(r, reverse("mes_core:planning_list"))
        self.assertContains(r, "Laura")
        self.assertContains(r, "Planeación")  # rol mostrado bajo el nombre
        self.assertContains(r, reverse("mes_core:scheduler"))

    def test_logistics_menu_is_minimal(self):
        log = get_user_model().objects.create_user("log", password="x")
        log.groups.add(Group.objects.get(name="Logística"))
        self.client.force_login(log)
        r = self.client.get(reverse("mes_core:goods_queue"))
        for name in ("plant_board", "order_list", "goods_queue"):
            self.assertContains(r, reverse(f"mes_core:{name}"))
        for name in ("dashboard", "scheduler", "plan", "at_risk", "plugins", "work_calendar", "supplies_queue"):
            self.assertNotContains(r, f'href="{reverse(f"mes_core:{name}")}"')
        for hidden in ("Fichas de acondicionado", "Lotes", "Módulos instalados", "Configuración", "Planeación<"):
            self.assertNotContains(r, hidden)

    def test_each_role_lands_on_its_screen(self):
        for group, home in (("Supervisor", "supervision_home"), ("Insumos", "supplies_queue"),
                            ("Logística", "goods_queue"), ("Ventas", "order_list")):
            u = get_user_model().objects.create_user(f"u-{home}", password="x")
            u.groups.add(Group.objects.get(name=group))
            self.client.force_login(u)
            self.assertRedirects(self.client.get("/"), reverse(f"mes_core:{home}"), fetch_redirect_response=False)
        self.client.force_login(self.planner)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_gantt_page(self):
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("mes_core:plan"), {"site": self.site.pk})
        self.assertContains(r, "Diagrama de Gantt")
        self.assertContains(r, 'data-can-plan="0"')
        self.assertNotContains(r, 'id="gt-auto"')
        self.client.force_login(self.planner)
        r = self.client.get(reverse("mes_core:plan"), {"site": self.site.pk})
        self.assertContains(r, 'data-can-plan="1"')
        self.assertContains(r, 'id="gt-auto"')

    def test_order_search_and_status_tabs(self):
        product = Product.objects.create(sku="CAM-1", name="Camiseta")
        cust = Customer.objects.create(name="Tiendas Norte")
        a = WorkOrder.objects.create(site=self.site, product=product, process_type="x", quantity=Decimal("10"),
                                     customer=cust, purchase_order="OC-777")
        b = WorkOrder.objects.create(site=self.site, product=product, process_type="x", quantity=Decimal("5"),
                                     status=WorkOrder.Status.TO_PLAN)
        self.client.force_login(self.viewer)
        r = self.client.get(reverse("mes_core:order_list"), {"q": "OC-777"})
        self.assertEqual(list(r.context["orders"]), [a])
        r = self.client.get(reverse("mes_core:order_list"), {"q": "norte"})
        self.assertEqual(list(r.context["orders"]), [a])
        r = self.client.get(reverse("mes_core:order_list"), {"status": WorkOrder.Status.TO_PLAN})
        self.assertEqual(list(r.context["orders"]), [b])
        self.assertEqual(r.context["total"], 2)
        tabs = {value: n for value, _, n in r.context["status_tabs"]}
        self.assertEqual((tabs[WorkOrder.Status.TO_PLAN], tabs[WorkOrder.Status.DRAFT]), (1, 1))


class AdminBrandingTests(TestCase):
    def test_admin_uses_plant_branding(self):
        self.client.force_login(get_user_model().objects.create_superuser("root", password="x"))
        r = self.client.get(reverse("admin:index"))
        self.assertContains(r, "MES Planta")
        self.assertContains(r, "admin_theme.css")
        self.assertContains(r, "Volver a la aplicación")
        self.assertContains(r, "Accesos rápidos")
        self.assertNotContains(r, "Django")
        login = self.client.get(reverse("admin:login"))  # ya autenticado: redirige al panel
        self.assertIn(login.status_code, (200, 302))
