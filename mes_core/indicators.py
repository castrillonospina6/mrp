"""Indicadores de la empresa: OEE, OLE, paros, pausas excedidas, OTIF, adherencia al plan, puntualidad y horas extra.

Definiciones (todo en un periodo [a, b); lo que aún no pasa se corta en «ahora»):

OEE de una línea = Disponibilidad × Rendimiento × Calidad
  * Tiempo planeado: las horas hábiles de la línea (turnos sin desayuno ni almuerzo, calendario laboral, sin
    mantenimientos) en las que tenía una operación programada o trabajando.
  * Tiempo produciendo: lo que de verdad trabajó (inicio real → fin real) dentro de ese tiempo, menos los paros de
    toda la línea registrados en piso.
  * Disponibilidad = tiempo produciendo ÷ tiempo planeado.
  * Rendimiento = minutos estándar producidos ÷ minutos-persona produciendo (tiempo produciendo × cuadrilla).
  * Calidad = buenas ÷ (buenas + rechazos + reprocesos).

OLE (eficacia de la mano de obra) de una bodega o de la empresa: la misma fórmula medida sobre la gente.
  * Tiempo planeado de la gente: personas del reparto × horas hábiles de cada turno.
  * Tiempo productivo de la gente: minutos-persona produciendo − paros individuales − pausas excedidas.
  * Disponibilidad = productivo ÷ planeado (ahí caen ausencias, llegadas tarde, gente sin tarea, paros y pausas).

TEEP de una línea = OEE × tiempo planeado ÷ tiempo del calendario (24 × 7)
                  = OEE × carga del turno (planeado ÷ horas de turno) × utilización (horas de turno ÷ calendario).

Alistamiento y cambios: paros de toda la línea con motivo de alistamiento / cambio / esperando patinador, frente al
tiempo planeado y al alistamiento que traían las operaciones. MTBF / MTTR: los demás paros de toda la línea
(cada cuánto, produciendo, para la línea y cuánto tarda en volver).

Los paros, pausas, marcaciones de reproceso, llegadas y convocatorias de horas extra vienen del módulo de control de
piso; si no está instalado, esos datos quedan en cero y el resto se calcula igual.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.apps import apps
from django.core.cache import cache
from django.db.models import Max, Q
from django.utils import timezone

from . import workcal
from .models import Downtime, Operation, OrderClosure, ProductionReport, Site, WorkCenter, WorkOrder
from .scheduling import center_shifts, shift_work_windows, site_shifts, working_windows
from .workcal import merge, subtract

ADHERENCE_GRACE = timedelta(minutes=30)   # una operación terminada hasta 30 min después de lo planeado está a tiempo
PUNCTUAL_GRACE = 5                         # minutos de tolerancia para la llegada
SETUP_WORDS = ("alistamiento", "patinador", "cambio")   # motivos de paro que son alistamiento o cambio de referencia


def is_setup(cause: str) -> bool:
    c = (cause or "").lower()
    return any(w in c for w in SETUP_WORDS)


def _piso(model: str):
    if not apps.is_installed("plugins.piso"):
        return None
    return apps.get_model("piso", model)


def _minutes(wins) -> float:
    return sum((b - a).total_seconds() for a, b in wins) / 60


def _intersect(wins, base):
    out = []
    for a, b in wins:
        for c, d in base:
            s, e = max(a, c), min(b, d)
            if e > s:
                out.append((s, e))
    return merge(out)


def _pct(x: float | None) -> int | None:
    return None if x is None else round(x * 100)


def level(pct: int | None, good: int = 85, warn: int = 60) -> str:
    """Estado de un indicador de 0–100 %: bueno, a vigilar o crítico."""
    if pct is None:
        return "na"
    return "good" if pct >= good else "warn" if pct >= warn else "bad"


LEVEL_LABEL = {"good": "Bien", "warn": "A vigilar", "bad": "Crítico", "na": "Sin datos"}


def _std_mpu(op: Operation) -> float:
    from .stage_planning import minutes_per_unit

    if op.stage_id and op.stage and op.stage.std_value:
        return minutes_per_unit(float(op.stage.std_value), op.stage.std_unit)
    return float(op.minutes_per_unit or 0)


# ---------------------------------------------------------------- OEE / OLE
@dataclass
class Effect:
    """Acumulado para OEE (línea) u OLE (gente)."""
    planned: float = 0.0        # minutos planeados (línea) o minutos-persona planeados (gente)
    run: float = 0.0            # minutos produciendo (línea) o minutos-persona productivos (gente)
    labor_run: float = 0.0      # minutos-persona produciendo (para el rendimiento)
    earned: float = 0.0         # minutos estándar producidos
    good: float = 0.0
    scrap: float = 0.0
    rework: float = 0.0
    stops: float = 0.0          # minutos de paro de línea
    shift: float = 0.0          # minutos de turno de la línea (horas hábiles)
    calendar: float = 0.0       # minutos del calendario (24 × 7)
    setup: float = 0.0          # minutos de alistamiento / cambio (paros de línea) dentro de lo planeado
    n_setup: int = 0
    setup_plan: float = 0.0     # minutos de alistamiento que traían las operaciones
    n_setup_plan: int = 0
    fail: float = 0.0           # minutos de los demás paros de línea
    n_fail: int = 0
    extra: dict = field(default_factory=dict)

    COUNTERS = ("planned", "run", "labor_run", "earned", "good", "scrap", "rework", "stops", "setup", "n_setup",
                "setup_plan", "n_setup_plan", "fail", "n_fail")

    def add(self, o: "Effect") -> None:
        for k in self.COUNTERS:
            setattr(self, k, getattr(self, k) + getattr(o, k))
        if o.planned > 0:  # el calendario solo cuenta las líneas que tuvieron trabajo
            self.shift += o.shift
            self.calendar += o.calendar

    @property
    def availability(self):
        return min(1.0, self.run / self.planned) if self.planned > 0 else None

    @property
    def performance(self):
        return self.earned / self.labor_run if self.labor_run > 0 and self.earned > 0 else None

    @property
    def quality(self):
        total = self.good + self.scrap + self.rework
        return self.good / total if total > 0 else None

    @property
    def value(self):
        a, p, q = self.availability, self.performance, self.quality
        if a is None or p is None:
            return None
        return a * min(1.0, p) * (q if q is not None else 1.0)

    @property
    def load(self):
        return min(1.0, self.planned / self.shift) if self.shift > 0 else None

    @property
    def utilization(self):
        return self.shift / self.calendar if self.calendar > 0 else None

    @property
    def teep(self):
        v = self.value
        return v * self.planned / self.calendar if v is not None and self.calendar > 0 else None

    def as_dict(self) -> dict:
        v = _pct(self.value)
        return {
            "value": v, "level": level(v), "level_label": LEVEL_LABEL[level(v)],
            "availability": _pct(self.availability), "performance": _pct(self.performance), "quality": _pct(self.quality),
            "planned_h": self.planned / 60, "run_h": self.run / 60, "labor_h": self.labor_run / 60,
            "earned_h": self.earned / 60, "good": self.good, "scrap": self.scrap, "rework": self.rework,
            "stops_h": self.stops / 60, "over_std": self.performance is not None and self.performance > 1.05,
            "teep": _pct(self.teep), "load": _pct(self.load), "utilization": _pct(self.utilization),
            "shift_h": self.shift / 60, "calendar_h": self.calendar / 60,
            "setup_min": self.setup, "n_setup": self.n_setup,
            "setup_pct": round(self.setup / self.planned * 100, 1) if self.planned > 0 else None,
            "setup_avg": self.setup / self.n_setup if self.n_setup else None,
            "setup_plan_min": self.setup_plan, "n_setup_plan": self.n_setup_plan,
            "setup_plan_avg": self.setup_plan / self.n_setup_plan if self.n_setup_plan else None,
            "n_fail": self.n_fail, "fail_min": self.fail,
            "mtbf_h": self.run / self.n_fail / 60 if self.n_fail and self.run > 0 else None,
            "mttr_min": self.fail / self.n_fail if self.n_fail else None,
        }


def _period_ops(lines, a, b):
    return list(Operation.objects.filter(work_center__in=lines)
                .filter(Q(planned_start__lt=b, planned_end__gt=a)
                        | Q(actual_start__lt=b) & (Q(actual_end__isnull=True) | Q(actual_end__gt=a)))
                .exclude(order__status=WorkOrder.Status.CANCELLED)
                .select_related("stage", "order", "work_center"))


def line_effectiveness(lines: list[WorkCenter], a: datetime, b: datetime) -> dict[int, Effect]:
    """OEE de cada línea en [a, b)."""
    now = timezone.now()
    b = min(b, now)
    if b <= a or not lines:
        return {wc.pk: Effect() for wc in lines}
    ops = _period_ops(lines, a, b)
    reports = defaultdict(lambda: [0.0, 0.0])
    for r in ProductionReport.objects.filter(operation__in=ops, reported_at__gte=a, reported_at__lt=b):
        reports[r.operation_id][0] += float(r.qty_good)
        reports[r.operation_id][1] += float(r.qty_scrap)
    Stop, Mark = _piso("Stop"), _piso("Mark")
    stops = defaultdict(list)
    setups = defaultdict(list)
    fails = defaultdict(lambda: [0, 0.0])
    if Stop is not None:
        for s in Stop.objects.filter(work_center__in=lines, employee__isnull=True, is_break=False, start__lt=b).filter(
                Q(end__isnull=True) | Q(end__gt=a)):
            win = (max(s.start, a), min(s.end or now, b))
            stops[s.work_center_id].append(win)
            if is_setup(s.cause):
                setups[s.work_center_id].append((win, s.start >= a))
            elif s.start >= a:
                fails[s.work_center_id][0] += 1
                fails[s.work_center_id][1] += s.minutes(a, b)
    rework = defaultdict(float)
    if Mark is not None:
        for m in Mark.objects.filter(work_center__in=lines, kind="w", at__gte=a, at__lt=b):
            rework[m.work_center_id] += float(m.qty)
    downs = defaultdict(list)
    for d in Downtime.objects.filter(work_center__in=lines, start__lt=b, end__gt=a):
        downs[d.work_center_id].append((d.start, d.end))
    by_line = defaultdict(list)
    for op in ops:
        by_line[op.work_center_id].append(op)
    out = {}
    site_sh = {}
    for wc in lines:
        if wc.site_id not in site_sh:
            site_sh[wc.site_id] = site_shifts(wc.site)
        base = subtract(working_windows(center_shifts(wc, site_sh[wc.site_id]), a, b), downs[wc.pk])
        line_stops = merge(stops[wc.pk])
        e = Effect(rework=rework[wc.pk], shift=_minutes(base), calendar=(b - a).total_seconds() / 60,
                   n_fail=fails[wc.pk][0], fail=fails[wc.pk][1])
        planned, actual = [], []
        for op in by_line[wc.pk]:
            if op.planned_start and op.planned_end:
                planned.append((op.planned_start, op.planned_end))
            setup = float((op.setup_minutes or 0) + (op.changeover_minutes or 0))
            began = op.actual_start or op.planned_start
            if setup > 0 and began and a <= began < b:
                e.setup_plan += setup
                e.n_setup_plan += 1
            g, sc = reports.get(op.pk, (0.0, 0.0))
            if op.actual_start:
                win = (op.actual_start, op.actual_end or (now if op.status == Operation.Status.IN_PROGRESS
                                                          else op.planned_end or now))
            elif g or sc:  # reportó sin marcar inicio: se toma lo planeado
                win = (op.planned_start, op.planned_end) if op.planned_start and op.planned_end else None
            else:
                win = None
            if win and win[1] > win[0]:
                actual.append(win)
                op_run = subtract(_intersect([win], base), line_stops)
                e.labor_run += _minutes(op_run) * max(1, op.crew)
            e.good += g
            e.scrap += sc
            e.earned += (g + sc) * _std_mpu(op)
        sched = _intersect(merge(planned + actual), base)
        run = subtract(_intersect(merge(actual), base), line_stops)
        e.planned = _minutes(sched)
        e.run = _minutes(run)
        e.stops = _minutes(_intersect(line_stops, sched))
        e.setup = _minutes(_intersect(merge([w for w, _ in setups[wc.pk]]), sched))
        e.n_setup = sum(1 for _, new in setups[wc.pk] if new)
        out[wc.pk] = e
    return out


def people_effectiveness(site: Site, lines_eff: dict[int, Effect], lines: list[WorkCenter], a: datetime,
                         b: datetime) -> Effect:
    """OLE de una bodega en [a, b): lo producido frente al tiempo de la gente que se planeó (reparto × turnos)."""
    from .workforce import shift_people

    now = timezone.now()
    b = min(b, now)
    e = Effect()
    for wc in lines:
        le = lines_eff.get(wc.pk)
        if le:
            e.labor_run += le.labor_run
            e.earned += le.earned
            e.good += le.good
            e.scrap += le.scrap
            e.rework += le.rework
    if b <= a:
        return e
    cal = workcal.get_calendar(site.pk)
    tz = timezone.get_current_timezone()
    day = timezone.localtime(a, tz).date() - timedelta(days=1)
    last = timezone.localtime(b, tz).date()
    while day <= last:
        for sh in site_shifts(site):
            if cal.shift_off_reason(sh, day):
                continue
            mins = _minutes(_intersect(shift_work_windows(sh, day, tz), [(a, b)]))
            if mins:
                e.planned += shift_people(site, sh, day)[0] * mins
        day += timedelta(days=1)
    lost = 0.0
    Stop = _piso("Stop")
    if Stop is not None:
        for s in Stop.objects.filter(work_center__in=lines, employee__isnull=False, start__lt=b).filter(
                Q(end__isnull=True) | Q(end__gt=a)):
            if s.is_break:
                if s.planned_end and (s.end or now) > s.planned_end:
                    lost += (min(s.end or now, b) - max(s.planned_end, a)).total_seconds() / 60
            else:
                lost += s.minutes(a, b)
    e.run = max(0.0, e.labor_run - lost)
    e.extra["lost_min"] = lost
    return e


class _OLE(Effect):
    """Para el OLE el rendimiento se mide sobre el tiempo productivo de la gente."""

    @property
    def performance(self):
        return self.earned / self.run if self.run > 0 and self.earned > 0 else None


LOAD_FULL = 90   # % del turno con trabajo a partir del cual el turno está lleno
LOAD_SLACK = 60  # por debajo, el turno tiene holgura


def capacity_reading(d: dict) -> dict:
    """Lectura del TEEP de una línea para decidir entre horas extra, otro turno o atacar las pérdidas."""
    load, oee = d.get("load"), d.get("value")
    if load is None or not d.get("planned_h"):
        return {"kind": "na", "label": "Sin turno o sin trabajo", "text": ""}
    free_h = max(0.0, d["shift_h"] - d["planned_h"])
    if load >= LOAD_FULL:
        if oee is not None and oee < 60:
            return {"kind": "warn", "label": "Turno lleno, OEE bajo",
                    "text": "Antes de pagar horas extra u otro turno, recupera lo que se pierde dentro del turno."}
        return {"kind": "full", "label": "Turno lleno",
                "text": "Si faltan horas varios días a la semana, otro turno sale mejor que horas extra; "
                        "si es algo puntual, horas extra."}
    if load < LOAD_SLACK:
        return {"kind": "slack", "label": "Turno con holgura",
                "text": f"Quedaron {free_h:.0f} h de turno sin trabajo programado: no hacen falta horas extra."}
    return {"kind": "ok", "label": "Carga normal", "text": f"{free_h:.0f} h de turno libres."}


def effectiveness(sites: list[Site], a: datetime, b: datetime) -> dict:
    """OEE por línea y bodega, OLE por bodega, y ambos para todo el conjunto."""
    lines = list(WorkCenter.objects.filter(site__in=sites, active=True).select_related("site").order_by("site__code", "code"))
    eff = line_effectiveness(lines, a, b)
    rows, site_rows = [], []
    tot_oee, tot_ole = Effect(), _OLE()
    for site in sites:
        site_lines = [wc for wc in lines if wc.site_id == site.pk]
        s_oee = Effect()
        for wc in site_lines:
            le = eff[wc.pk]
            s_oee.add(le)
            if le.planned or le.good or le.scrap:
                d = le.as_dict()
                rows.append({"wc": wc, "site": site, **d, "reading": capacity_reading(d)})
        people = people_effectiveness(site, eff, site_lines, a, b)
        s_ole = _OLE(planned=people.planned, run=people.run, labor_run=people.labor_run, earned=people.earned,
                     good=people.good, scrap=people.scrap, rework=people.rework)
        tot_oee.add(s_oee)
        tot_ole.add(s_ole)
        if s_oee.planned or s_ole.planned or s_oee.good:
            site_rows.append({"site": site, "oee": s_oee.as_dict(), "ole": s_ole.as_dict(),
                              "lost_h": people.extra.get("lost_min", 0) / 60})
    rows.sort(key=lambda r: (r["value"] is None, r["value"] if r["value"] is not None else 0))
    oee = tot_oee.as_dict()
    return {"lines": rows, "sites": site_rows, "oee": oee, "ole": tot_ole.as_dict(), "reading": capacity_reading(oee)}


# ---------------------------------------------------------------- paros y pausas
def stops_pareto(sites: list[Site], a: datetime, b: datetime) -> dict:
    """Pareto de paros de piso por motivo (sin desayuno ni almuerzo)."""
    Stop = _piso("Stop")
    if Stop is None:
        return {"rows": [], "total_min": 0, "installed": False}
    now = timezone.now()
    b = min(b, now)
    agg: dict[str, dict] = {}
    for s in (Stop.objects.filter(work_center__site__in=sites, is_break=False, start__lt=b)
              .filter(Q(end__isnull=True) | Q(end__gt=a)).select_related("work_center")):
        m = s.minutes(a, b)
        if m <= 0:
            continue
        key = " ".join(s.cause.split()).capitalize() or "Sin motivo"
        r = agg.setdefault(key, {"cause": key, "minutes": 0.0, "count": 0, "lines": defaultdict(float), "people": 0})
        r["minutes"] += m
        r["count"] += 1
        r["lines"][s.work_center.code if s.work_center_id else "—"] += m
        if s.employee_id:
            r["people"] += 1
    rows = sorted(agg.values(), key=lambda r: -r["minutes"])
    total = sum(r["minutes"] for r in rows)
    cum = 0.0
    top = rows[0]["minutes"] if rows else 1
    for r in rows:
        cum += r["minutes"]
        r["pct"] = round(r["minutes"] / total * 100) if total else 0
        r["cum_pct"] = round(cum / total * 100) if total else 0
        r["bar"] = round(r["minutes"] / top * 100) if top else 0
        r["top_lines"] = sorted(r["lines"].items(), key=lambda x: -x[1])[:3]
        r["vital"] = r["cum_pct"] <= 80 or r is rows[0]
        del r["lines"]
    return {"rows": rows, "total_min": total, "installed": True}


def exceeded_breaks(sites: list[Site], a: datetime, b: datetime) -> dict:
    """Desayunos y almuerzos que pasaron de su fin previsto: minutos de más por línea y por supervisor."""
    Stop = _piso("Stop")
    if Stop is None:
        return {"installed": False, "n": 0, "over": 0, "minutes": 0.0, "by_line": [], "by_supervisor": []}
    now = timezone.now()
    b = min(b, now)
    n = over = 0
    minutes = 0.0
    by_line: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])
    by_sup: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])
    for s in (Stop.objects.filter(work_center__site__in=sites, is_break=True, start__gte=a, start__lt=b)
              .select_related("work_center", "operation__supervisor")):
        n += 1
        line = s.work_center.code if s.work_center_id else "—"
        sup_u = s.operation.supervisor if s.operation_id and s.operation and s.operation.supervisor_id else None
        sup = (sup_u.get_full_name() or sup_u.username) if sup_u else "Sin supervisor"
        by_line[line][0] += 1
        by_sup[sup][0] += 1
        if s.planned_end and (s.end or now) > s.planned_end:
            extra = ((s.end or now) - s.planned_end).total_seconds() / 60
            if extra >= 1:
                over += 1
                minutes += extra
                for d in (by_line[line], by_sup[sup]):
                    d[1] += 1
                    d[2] += extra

    def rows(d):
        out = [{"name": k, "n": v[0], "over": v[1], "minutes": v[2], "pct": round(v[1] / v[0] * 100) if v[0] else 0,
                "avg": v[2] / v[1] if v[1] else 0} for k, v in d.items()]
        return sorted(out, key=lambda r: -r["minutes"])

    return {"installed": True, "n": n, "over": over, "minutes": minutes,
            "pct": round(over / n * 100) if n else None, "by_line": rows(by_line)[:10], "by_supervisor": rows(by_sup)[:10]}


# ---------------------------------------------------------------- entrega y plan
def otif(sites: list[Site], a: datetime, b: datetime) -> dict:
    """Pedidos terminados en el periodo: a tiempo (antes del compromiso) y completos (sin cierre con faltante)."""
    done = (WorkOrder.objects.filter(site__in=sites, status=WorkOrder.Status.DONE, due_date__isnull=False)
            .annotate(done_at=Max("operations__actual_end")).filter(done_at__gte=a, done_at__lt=b)
            .select_related("customer"))
    closures = {c.order_id: c for c in OrderClosure.objects.filter(order__in=done)}
    rows, n_time = [], 0
    n_full = n_both = 0
    for o in done:
        on_time = o.done_at <= o.due_date
        short = closures.get(o.pk)
        in_full = not (short and short.total > 0)
        n_time += on_time
        n_full += in_full
        n_both += on_time and in_full
        if not (on_time and in_full):
            rows.append({"order": o, "on_time": on_time, "in_full": in_full, "done_at": o.done_at,
                         "late_h": max(0.0, (o.done_at - o.due_date).total_seconds() / 3600),
                         "short": float(short.total) if short else 0.0, "reason": short.reason if short else ""})
    n = len(done)
    rows.sort(key=lambda r: -r["late_h"])
    pct = lambda k: round(k / n * 100) if n else None  # noqa: E731
    return {"n": n, "otif": pct(n_both), "on_time": pct(n_time), "in_full": pct(n_full),
            "level": level(pct(n_both), good=95, warn=85), "misses": rows[:15],
            "level_label": LEVEL_LABEL[level(pct(n_both), good=95, warn=85)]}


def plan_adherence(sites: list[Site], a: datetime, b: datetime) -> dict:
    """Operaciones que debían terminar en el periodo y terminaron a tiempo (hasta 30 min después de lo planeado)."""
    now = timezone.now()
    b = min(b, now)
    ops = (Operation.objects.filter(order__site__in=sites, planned_end__gte=a, planned_end__lt=b)
           .exclude(order__status=WorkOrder.Status.CANCELLED).select_related("order__site", "supervisor"))
    by_site: dict[str, list] = defaultdict(lambda: [0, 0])
    by_sup: dict[str, list] = defaultdict(lambda: [0, 0])
    n = hit = 0
    for op in ops:
        ok = op.status == Operation.Status.DONE and op.actual_end and op.actual_end <= op.planned_end + ADHERENCE_GRACE
        n += 1
        hit += bool(ok)
        sup = (op.supervisor.get_full_name() or op.supervisor.username) if op.supervisor_id else "Sin supervisor"
        for d, k in ((by_site, op.order.site.code), (by_sup, sup)):
            d[k][0] += 1
            d[k][1] += bool(ok)

    def rows(d):
        return sorted(({"name": k, "n": v[0], "hit": v[1], "pct": round(v[1] / v[0] * 100) if v[0] else None}
                       for k, v in d.items()), key=lambda r: (r["pct"] if r["pct"] is not None else 101))

    p = round(hit / n * 100) if n else None
    return {"n": n, "hit": hit, "pct": p, "level": level(p, good=90, warn=75),
            "level_label": LEVEL_LABEL[level(p, good=90, warn=75)],
            "by_site": rows(by_site), "by_supervisor": rows(by_sup)[:10]}


# ---------------------------------------------------------------- gente
def punctuality(sites: list[Site], a: datetime, b: datetime) -> dict:
    """Llegadas confirmadas en la tablet frente a la hora de entrada del turno (5 min de tolerancia)."""
    CheckIn = _piso("CheckIn")
    if CheckIn is None:
        return {"installed": False, "n": 0}
    tz = timezone.get_current_timezone()
    shifts_by_site = {s.pk: list(site_shifts(s)) for s in sites}
    n = late = 0
    late_min = 0.0
    by_site: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])
    for c in (CheckIn.objects.filter(at__gte=a, at__lt=b)
              .select_related("employee__shift", "employee__site", "work_center__site")):
        site_id = c.work_center.site_id if c.work_center_id else c.employee.site_id
        if site_id not in shifts_by_site:
            continue
        cands = []
        if c.employee.shift_id and c.employee.shift.site_id == site_id:
            cands = [c.employee.shift]
        cands = cands or shifts_by_site[site_id]
        best = None
        for sh in cands:
            for d in (c.day - timedelta(days=1), c.day, c.day + timedelta(days=1)):
                start = shift_work_windows(sh, d, tz)[0][0] if shift_work_windows(sh, d, tz) else None
                if start is None:
                    continue
                diff = (c.at - start).total_seconds() / 60
                if -180 <= diff <= 300 and (best is None or abs(diff) < abs(best)):
                    best = diff
        if best is None:
            continue
        n += 1
        code = next((s.code for s in sites if s.pk == site_id), "—")
        by_site[code][0] += 1
        if best > PUNCTUAL_GRACE:
            late += 1
            late_min += best
            by_site[code][1] += 1
            by_site[code][2] += best
    p = round((n - late) / n * 100) if n else None
    return {"installed": True, "n": n, "late": late, "pct": p, "level": level(p, good=95, warn=85),
            "level_label": LEVEL_LABEL[level(p, good=95, warn=85)],
            "avg_late": late_min / late if late else 0, "late_h": late_min / 60,
            "by_site": sorted(({"name": k, "n": v[0], "late": v[1], "pct": round((v[0] - v[1]) / v[0] * 100),
                                "avg_late": v[2] / v[1] if v[1] else 0} for k, v in by_site.items()),
                              key=lambda r: r["pct"])}


def overtime_response(sites: list[Site], a: datetime, b: datetime) -> dict:
    """Convocatorias de horas extra enviadas en el periodo: quién responde, cuántos aceptan y si se cubre lo pedido."""
    Call, Request = _piso("OvertimeCall"), _piso("OvertimeRequest")
    if Call is None:
        return {"installed": False, "calls": 0}
    site_ids = {s.pk for s in sites}
    calls = [c for c in Call.objects.filter(sent_at__gte=a, sent_at__lt=b).prefetch_related("sites", "days__answers")
             if (c.site_id in site_ids) or not c.site_id or any(s.pk in site_ids for s in c.sites.all())]
    reqs = list(Request.objects.filter(call__in=calls))
    yes = sum(1 for r in reqs if r.answer == "si")
    no = sum(1 for r in reqs if r.answer == "no")
    pending = len(reqs) - yes - no
    needed = covered = 0
    for c in calls:
        for d in c.days.all():
            if d.needed:
                needed += d.needed
                covered += min(d.needed, d.answers.count())
    reasons: dict[str, int] = defaultdict(int)
    for r in reqs:
        if r.answer == "no":
            reasons[" ".join((r.reason or "Sin motivo").split()).capitalize()] += 1
    by_site: dict[str, int] = defaultdict(int)
    for c in calls:
        names = [s.code for s in c.sites.all()] or ([c.site.code] if c.site_id else ["Todas"])
        for nm in names:
            by_site[nm] += 1
    answered = yes + no
    acc = round(yes / answered * 100) if answered else None
    return {"installed": True, "calls": len(calls), "requests": len(reqs), "yes": yes, "no": no, "pending": pending,
            "response_pct": round(answered / len(reqs) * 100) if reqs else None, "accept_pct": acc,
            "coverage_pct": round(covered / needed * 100) if needed else None, "needed": needed, "covered": covered,
            "level": level(acc, good=70, warn=40), "level_label": LEVEL_LABEL[level(acc, good=70, warn=40)],
            "reasons": sorted(reasons.items(), key=lambda x: -x[1])[:5],
            "by_site": sorted(by_site.items(), key=lambda x: -x[1])}


# ---------------------------------------------------------------- conjunto
def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _bounds(d0: date, d1: date):
    tz = timezone.get_current_timezone()
    return (datetime.combine(d0, datetime.min.time(), tzinfo=tz), datetime.combine(d1, datetime.min.time(), tzinfo=tz))


def point(sites: list[Site], d0: date, d1: date, label: str = "") -> dict:
    """Indicadores principales de los días [d0, d1] (inclusive). Los periodos ya cerrados se guardan 10 min en caché."""
    today = timezone.localdate()
    key = f"ind:pt:{','.join(str(s.pk) for s in sites)}:{d0.isoformat()}:{d1.isoformat()}"
    closed = d1 < today
    hit = cache.get(key) if closed else None
    if hit is None:
        e = effectiveness(sites, *_bounds(d0, d1 + timedelta(days=1)))
        o = e["oee"]
        hit = {"start": d0, "end": d1, "week": d0, "oee": o["value"], "ole": e["ole"]["value"], "teep": o["teep"],
               "a": o["availability"], "p": o["performance"], "q": o["quality"],
               "setup_pct": o["setup_pct"], "mtbf_h": o["mtbf_h"], "mttr_min": o["mttr_min"]}
        if closed:
            cache.set(key, hit, 600)
    return {**hit, "label": label or hit.get("label", "")}


def trend(sites: list[Site], last_week: date, weeks: int = 8) -> list[dict]:
    """OEE y OLE de las últimas semanas."""
    return [point(sites, w, w + timedelta(days=6), f"sem. {w:%d/%m}")
            for w in (last_week - timedelta(weeks=i) for i in range(weeks - 1, -1, -1))]


# ---------------------------------------------------------------- periodos (calendario e histórico)
MAX_DAYS = 400
MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
DAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
PRESETS = (("hoy", "Hoy"), ("ayer", "Ayer"), ("semana", "Esta semana"), ("semana-pasada", "Semana pasada"),
           ("mes", "Este mes"), ("mes-pasado", "Mes pasado"), ("30d", "Últimos 30 días"), ("90d", "Últimos 90 días"))


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


@dataclass
class Period:
    """Días [start, end] (inclusive) que se miran en «Indicadores»."""
    start: date
    end: date
    preset: str = ""          # uno de PRESETS, o "" si es un rango a mano

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def bounds(self):
        return _bounds(self.start, self.end + timedelta(days=1))

    @property
    def live(self) -> bool:
        """Incluye hoy: lo que pasa ahora se ve al actualizar."""
        return self.start <= timezone.localdate() <= self.end

    @property
    def is_week(self) -> bool:
        return self.days == 7 and self.start.weekday() == 0

    @property
    def is_month(self) -> bool:
        return self.start.day == 1 and self.end == _month_end(self.start)

    @property
    def label(self) -> str:
        if self.days == 1:
            today = timezone.localdate()
            name = "hoy" if self.start == today else "ayer" if self.start == today - timedelta(days=1) else ""
            return f"{name + ', ' if name else ''}{DAYS[self.start.weekday()]} {self.start:%d/%m/%Y}"
        if self.is_month:
            return f"{MONTHS[self.start.month - 1]} {self.start.year}"
        what = "semana del" if self.is_week else "del"
        return f"{what} {self.start:%d/%m} al {self.end:%d/%m/%Y}"

    @property
    def noun(self) -> str:
        """Cómo nombrar el periodo en los textos: «este día», «esta semana», «este periodo»."""
        return "este día" if self.days == 1 else "esta semana" if self.is_week else "este mes" if self.is_month \
            else "este periodo"

    def previous(self) -> "Period":
        """El periodo inmediatamente anterior, del mismo largo (para el mes: el mes anterior)."""
        if self.is_month:
            e = self.start - timedelta(days=1)
            return Period(_month_start(e), e)
        return Period(self.start - timedelta(days=self.days), self.start - timedelta(days=1))

    def next(self) -> "Period":
        if self.is_month:
            s = self.end + timedelta(days=1)
            return Period(s, _month_end(s))
        return Period(self.end + timedelta(days=1), self.end + timedelta(days=self.days))

    def query(self) -> str:
        return f"desde={self.start.isoformat()}&hasta={self.end.isoformat()}"


def preset_period(key: str, today: date | None = None) -> Period | None:
    t = today or timezone.localdate()
    ws = week_start(t)
    ms = _month_start(t)
    span = {
        "hoy": (t, t), "ayer": (t - timedelta(days=1), t - timedelta(days=1)),
        "semana": (ws, ws + timedelta(days=6)), "semana-pasada": (ws - timedelta(days=7), ws - timedelta(days=1)),
        "mes": (ms, _month_end(t)), "mes-pasado": (_month_start(ms - timedelta(days=1)), ms - timedelta(days=1)),
        "30d": (t - timedelta(days=29), t), "90d": (t - timedelta(days=89), t),
    }.get(key)
    return Period(*span, preset=key) if span else None


def parse_period(params, today: date | None = None) -> tuple[Period, str]:
    """Lee el periodo de la dirección: ?periodo=hoy|semana|…, ?desde=&hasta= o ?semana= (lunes de una semana).
    Devuelve el periodo y un aviso si hubo que corregir algo."""
    from django.utils.dateparse import parse_date

    t = today or timezone.localdate()

    def day(v):
        try:
            return parse_date(v or "")
        except ValueError:
            return None

    warn = ""
    d0, d1 = day(params.get("desde")), day(params.get("hasta"))
    if d0 or d1:
        d0, d1 = d0 or d1, d1 or d0
        if d1 < d0:
            d0, d1 = d1, d0
        if d0 > t:
            d0 = d1 = t
            warn = "Las fechas eran futuras: se muestra hoy."
        if (d1 - d0).days + 1 > MAX_DAYS:
            d0 = d1 - timedelta(days=MAX_DAYS - 1)
            warn = f"El rango máximo es de {MAX_DAYS} días: se tomaron los últimos {MAX_DAYS}."
        per = Period(d0, d1)
        for key, _ in PRESETS:  # si coincide con un atajo, se marca
            pp = preset_period(key, t)
            if pp.start == d0 and pp.end == d1:
                per.preset = key
                break
        return per, warn
    if params.get("semana"):
        w = day(params.get("semana"))
        if w:
            w = week_start(min(w, t))
            per = Period(w, w + timedelta(days=6))
            per.preset = "semana" if w == week_start(t) else "semana-pasada" if w == week_start(t) - timedelta(days=7) else ""
            return per, ""
    return preset_period(params.get("periodo") or "semana", t) or preset_period("semana", t), ""


def buckets(per: Period, today: date | None = None) -> list[tuple[date, date, str]]:
    """Cortes para la tendencia, sin pasar de hoy:
    * un día o un rango corto (≤ 7 días que no es una semana): los 8 días que terminan en el último día;
    * una semana de lunes a domingo: las 8 semanas que terminan en ella;
    * hasta 31 días: día a día; hasta 26 semanas: semana a semana; más: mes a mes."""
    t = today or timezone.localdate()
    last = min(per.end, t)
    out = []
    if per.is_week:
        for i in range(7, -1, -1):
            w = per.start - timedelta(weeks=i)
            out.append((w, w + timedelta(days=6), f"sem. {w:%d/%m}"))
        return out
    if per.days <= 7:
        return [(d, d, f"{DAYS[d.weekday()]} {d:%d/%m}") for d in (last - timedelta(days=i) for i in range(7, -1, -1))]
    if per.days <= 31:
        d, out = per.start, []
        while d <= last:
            out.append((d, d, f"{DAYS[d.weekday()]} {d:%d/%m}"))
            d += timedelta(days=1)
        return out
    if per.days <= 26 * 7:
        d = per.start
        while d <= last:
            e = min(week_start(d) + timedelta(days=6), per.end)
            out.append((d, e, f"sem. {d:%d/%m}"))
            d = e + timedelta(days=1)
        return out
    d = per.start
    while d <= last:
        e = min(_month_end(d), per.end)
        out.append((d, e, f"{MONTHS[d.month - 1]} {d:%y}"))
        d = e + timedelta(days=1)
    return out


def series(sites: list[Site], per: Period, today: date | None = None) -> list[dict]:
    return [point(sites, a, b, lbl) for a, b, lbl in buckets(per, today)]


def summary(sites: list[Site], a: datetime, b: datetime) -> dict:
    """Los indicadores del tablero de la empresa en un periodo (se guardan 5 min en caché)."""
    key = f"ind:sum:{','.join(str(s.pk) for s in sites)}:{a.isoformat()}:{b.date().isoformat()}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    e = effectiveness(sites, a, b)
    data = {"oee": e["oee"], "ole": e["ole"], "otif": otif(sites, a, b), "adherence": plan_adherence(sites, a, b),
            "punctuality": punctuality(sites, a, b), "overtime": overtime_response(sites, a, b),
            "stops": stops_pareto(sites, a, b), "breaks": exceeded_breaks(sites, a, b)}
    cache.set(key, data, 300)
    return data


def spark(points: list[dict], key: str, width: int = 260, height: int = 64) -> dict:
    """Coordenadas de una línea de tendencia 0–100 % (un solo indicador: sin leyenda)."""
    pad = 6
    n = len(points)
    step = (width - 2 * pad) / max(1, n - 1)
    pts = []
    for i, p in enumerate(points):
        v = p.get(key)
        x = pad + i * step
        y = None if v is None else pad + (height - 2 * pad) * (1 - min(120, v) / 120)
        pts.append({"x": f"{x:.1f}", "y": None if y is None else f"{y:.1f}", "v": v, "week": p["week"], "label": p.get("label", ""), "start": p.get("start"), "end": p.get("end")})
    segs, cur = [], []
    for p in pts:
        if p["y"] is None:
            if len(cur) > 1:
                segs.append(cur)
            cur = []
        else:
            cur.append(p)
    if len(cur) > 1:
        segs.append(cur)
    y100 = f"{pad + (height - 2 * pad) * (1 - 100 / 120):.1f}"  # texto: el SVG no acepta coma decimal
    y85 = f"{pad + (height - 2 * pad) * (1 - 85 / 120):.1f}"
    return {"w": width, "h": height, "points": pts, "y100": y100, "y85": y85,
            "paths": [" ".join(f"{p['x']},{p['y']}" for p in s) for s in segs],
            "last": next((p for p in reversed(pts) if p["v"] is not None), None)}

