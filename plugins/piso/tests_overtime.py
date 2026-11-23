"""Horas extra: convocatoria desde el MES, bloqueo en la tablet hasta responder, SÍ/NO y resumen para planeación."""

from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from . import overtime
from .models import OvertimeCall, OvertimeRequest
from .tests import FloorBase


class OvertimeTests(FloorBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        today = timezone.localdate()
        self.sat = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
        self.sun = self.sat + timedelta(days=1)
        self.call = overtime.save_call(None, site=self.site, title="Extras fin de semana", note="Pedido urgente",
                                       shifts=[], respond_by=None, days=[
                                           overtime.DaySpec(self.sat, time(6), time(14), needed=5),
                                           overtime.DaySpec(self.sun, time(6), time(12))])

    def answer(self, body):
        return self.post("piso:api_overtime", body)

    def test_send_blocks_the_tablet_until_answering(self):
        self.assertEqual(overtime.send(self.call), 2)  # ana y beto (turno de la planta, con tablet)
        self.login()
        s = self.client.get(reverse("piso:api_state")).json()
        pend = s["overtime"]["pending"]
        self.assertEqual(pend["title"], "Extras fin de semana")
        self.assertEqual([d["kind"] for d in pend["days"]], ["Sábado", "Domingo"])
        r = self.post("piso:api_mark", {"k": "p"})
        self.assertEqual((r.status_code, r.json().get("overtime")), (409, True))
        self.assertEqual(self.answer({"req": pend["req"], "yes": False, "reason": ""}).status_code, 400)
        r = self.answer({"req": pend["req"], "yes": False, "reason": "Estudio"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["state"]["overtime"]["pending"])
        self.assertEqual(self.post("piso:api_mark", {"k": "p"}).status_code, 200)

    def test_yes_with_days_and_until_and_summary(self):
        overtime.send(self.call)
        self.login("1002")
        req = OvertimeRequest.objects.get(employee=self.beto)
        sat, sun = self.call.days.order_by("date")
        bad = self.answer({"req": req.pk, "yes": True, "picks": [{"d": sat.pk, "h": "15:00"}]})
        self.assertEqual(bad.status_code, 400)  # fuera de la franja
        self.assertEqual(self.answer({"req": req.pk, "yes": True, "picks": []}).status_code, 400)
        ok = self.answer({"req": req.pk, "yes": True, "picks": [{"d": sat.pk, "h": "12:00"}]})
        self.assertEqual(ok.status_code, 200)
        calls = ok.json()["state"]["overtime"]["calls"]
        self.assertEqual(calls[0]["days"][0]["going"], [{"name": self.beto.display_name, "until": "12:00"}])
        self.assertEqual(calls[0]["days"][0]["mine"], "12:00")
        overtime.answer(OvertimeRequest.objects.get(employee=self.ana), yes=False, reason="Salud / cita médica")
        s = overtime.summary(self.call)
        self.assertEqual((s["total"], s["answered"], len(s["yes"]), len(s["no"]), len(s["pending"])), (2, 2, 1, 1, 0))
        self.assertEqual((s["days"][0]["count"], s["days"][0]["hours"], s["days"][0]["missing"]), (1, 6.0, 4))
        self.assertEqual(s["days"][1]["count"], 0)

    def test_closed_or_expired_calls_do_not_block(self):
        overtime.send(self.call)
        self.assertIsNotNone(overtime.pending_for(self.ana))
        overtime.close(self.call)
        self.assertIsNone(overtime.pending_for(self.ana))
        with self.assertRaises(ValidationError):
            overtime.answer(OvertimeRequest.objects.get(employee=self.ana), yes=False, reason="tarde")

    def test_validation_of_days(self):
        with self.assertRaises(ValidationError):
            overtime.save_call(None, site=self.site, title="x", note="", shifts=[], respond_by=None, days=[])
        with self.assertRaises(ValidationError):
            overtime.save_call(None, site=self.site, title="x", note="", shifts=[], respond_by=None,
                               days=[overtime.DaySpec(timezone.localdate() - timedelta(days=1), time(6), time(8))])

    def test_all_sites_mass_send_or_by_site(self):
        from mes_core.models import Employee, Site

        from .models import FloorAccess
        other = Site.objects.create(code="P2", name="Planta 2")
        zoe = Employee.objects.create(document="3003", full_name="zoe paz", site=other)
        acc = FloorAccess(employee=zoe)
        acc.set_pin("1234")
        acc.save()
        days = [overtime.DaySpec(self.sat, time(6), time(14))]
        everyone = overtime.save_call(None, sites=[], title="Todos", note="", shifts=[], respond_by=None, days=days)
        self.assertIsNone(everyone.site)
        self.assertEqual(overtime.send(everyone), 3)  # todas las bodegas: ana, beto y zoe
        only_p2 = overtime.save_call(None, sites=[other], title="Solo P2", note="", shifts=[], respond_by=None, days=days)
        self.assertEqual([e.pk for e in overtime.audience(only_p2)], [zoe.pk])
        self.assertEqual(overtime.target_label(only_p2), "Planta 2")
        u = get_user_model().objects.create_superuser("plan2", password="x")
        self.client.force_login(u)
        page = self.client.get(reverse("piso:overtime") + "?site=todas").content.decode()
        self.assertIn("Todas las bodegas", page)
        self.assertIn("Solo P2", page)
        page = self.client.get(reverse("piso:overtime") + f"?site={self.site.pk}").content.decode()
        self.assertIn("Todos", page)          # la de todas las bodegas también se ve en cada bodega
        self.assertNotIn("Solo P2", page)

    def test_mes_pages(self):
        u = get_user_model().objects.create_superuser("plan", password="x")
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse("piso:overtime")).status_code, 200)
        self.assertEqual(self.client.get(reverse("piso:overtime_new") + f"?site={self.site.pk}").status_code, 200)
        r = self.client.post(reverse("piso:overtime_new") + f"?site={self.site.pk}", {
            "site": self.site.pk, "title": "Extras festivo", "note": "", "send": "1",
            "d_date": [self.sun.isoformat()], "d_start": ["07:00"], "d_end": ["13:00"], "d_needed": ["3"]})
        self.assertEqual(r.status_code, 302)
        call = OvertimeCall.objects.get(title="Extras festivo")
        self.assertEqual((call.status, call.requests.count()), (OvertimeCall.Status.SENT, 2))
        page = self.client.get(reverse("piso:overtime_detail", args=[call.pk])).content.decode()
        self.assertIn("Faltan por responder", page)
        x = self.client.get(reverse("piso:overtime_xlsx", args=[call.pk]))
        self.assertEqual(x.status_code, 200)
        self.assertTrue(x.content[:2] == b"PK")
