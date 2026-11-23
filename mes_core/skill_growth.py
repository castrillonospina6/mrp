"""Sugerencias de nivel en la matriz de polivalencia.

Con las horas que cada operario ha trabajado en cada competencia (cuadrillas de operaciones que sí se produjeron,
según los reportes), el sistema propone subirle el nivel: «Ana lleva 120 h en etiquetado: ¿subir a experto?».
El supervisor solo aprueba o descarta; lo descartado no se vuelve a proponer para ese nivel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import CrewAssignment, Employee, EmployeeSkill, Operation, Skill, SkillReview, WorkOrder

LEVELS = EmployeeSkill.Level
# Horas trabajadas en la competencia para sugerir cada nivel (se puede cambiar con MES_SKILL_HOURS en settings).
DEFAULT_HOURS = {LEVELS.NOVICE: 8, LEVELS.INTERMEDIATE: 40, LEVELS.EXPERT: 120}
HISTORY = timedelta(days=365)


def thresholds() -> dict[int, float]:
    custom = getattr(settings, "MES_SKILL_HOURS", None) or {}
    return {int(k): float(v) for k, v in {**DEFAULT_HOURS, **custom}.items()}


@dataclass
class Suggestion:
    employee: Employee
    skill: Skill
    hours: float
    current: int  # 0 = sin nivel
    level: int

    @property
    def level_label(self) -> str:
        return LEVELS(self.level).label

    @property
    def current_label(self) -> str:
        return LEVELS(self.current).label if self.current else "sin nivel"


def worked_hours(site, now=None) -> dict[tuple[int, int], float]:
    """{(operario, competencia): horas hábiles} en operaciones con producción reportada (último año)."""
    from .risk import _Clock
    from .staffing import match_skill

    now = now or timezone.now()
    ops = (Operation.objects.filter(order__site=site, planned_start__isnull=False, planned_start__gte=now - HISTORY,
                                    planned_start__lt=now)
           .exclude(order__status=WorkOrder.Status.CANCELLED)
           .filter(Q(status=Operation.Status.DONE) | Q(qty_good__gt=0))
           .select_related("work_center", "stage"))
    skills = list(Skill.objects.all())
    clock = _Clock(site)
    op_info = {}
    for op in ops:
        skill = match_skill(op, skills)
        if skill is None:
            continue
        a = op.actual_start or op.planned_start
        b = op.actual_end or (op.planned_end if op.status == Operation.Status.DONE else min(now, op.planned_end or now))
        if not b or b <= a:
            continue
        op_info[op.pk] = (skill.pk, clock.minutes(op, a, b) / 60)
    out: dict[tuple[int, int], float] = {}
    for emp_id, op_id in CrewAssignment.objects.filter(operation_id__in=op_info).values_list("employee_id", "operation_id"):
        skill_id, hours = op_info[op_id]
        out[(emp_id, skill_id)] = out.get((emp_id, skill_id), 0.0) + hours
    return out


def suggestions(site, supervisor=None, now=None) -> list[Suggestion]:
    hours = worked_hours(site, now)
    if not hours:
        return []
    th = thresholds()
    emps = Employee.objects.filter(active=True, pk__in={e for e, _ in hours}).select_related("supervisor")
    if supervisor is not None:
        emps = emps.filter(supervisor=supervisor)
    emps = {e.pk: e for e in emps}
    skills = {s.pk: s for s in Skill.objects.filter(pk__in={s for _, s in hours})}
    levels = {(es.employee_id, es.skill_id): es.level
              for es in EmployeeSkill.objects.filter(employee_id__in=emps, skill_id__in=skills)}
    reviewed = set(SkillReview.objects.filter(employee_id__in=emps).values_list("employee_id", "skill_id", "level"))
    out = []
    for (emp_id, skill_id), h in hours.items():
        emp = emps.get(emp_id)
        if emp is None:
            continue
        current = levels.get((emp_id, skill_id), 0)
        target = max((lvl for lvl, need in th.items() if h >= need and lvl > current), default=None)
        if target is None or (emp_id, skill_id, target) in reviewed:
            continue
        out.append(Suggestion(emp, skills[skill_id], round(h, 1), current, target))
    out.sort(key=lambda s: (-s.level, -s.hours))
    return out


@transaction.atomic
def decide(employee: Employee, skill: Skill, level: int, approve: bool, hours, user=None) -> None:
    if level not in LEVELS.values:
        raise ValidationError("Nivel no válido.")
    who = user if user is not None and user.is_authenticated else None
    SkillReview.objects.update_or_create(
        employee=employee, skill=skill, level=level,
        defaults={"hours": Decimal(str(hours or 0)).quantize(Decimal("0.1")), "decided_by": who,
                  "decision": SkillReview.Decision.APPROVED if approve else SkillReview.Decision.DISMISSED})
    if approve:
        EmployeeSkill.objects.update_or_create(employee=employee, skill=skill,
                                               defaults={"level": level, "updated_by": who})


def check(site, now=None) -> int:
    """Avisa a cada supervisor las sugerencias nuevas de su equipo (una vez por operario, competencia y nivel)."""
    from .notify import K, notify

    sent = 0
    url = reverse("mes_core:skill_suggestions") + f"?site={site.pk}"
    for s in suggestions(site, now=now):
        sup = s.employee.supervisor
        if sup is None:
            continue
        sent += notify([sup], K.SKILL, f"{s.employee.display_name}: ¿subir a {s.level_label.lower()} en {s.skill.name}?",
                       f"Lleva {s.hours:g} h trabajadas en {s.skill.name} (hoy: {s.current_label.lower()}).", url, site,
                       f"skill:{s.employee.pk}:{s.skill.pk}:{s.level}")
    return sent
