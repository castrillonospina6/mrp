"""Personal: supervisores responsables, asistencia nominal, cuadrillas y carga masiva de operarios.

Flujo:
  1. Planeación asigna un supervisor a cada operación (la operación ya trae los operarios requeridos).
  2. El supervisor confirma la asistencia de su equipo (sincroniza las ausencias del tablero de capacidad).
  3. El supervisor asigna personas a cada operación, ayudado por la matriz de polivalencia.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Attendance, CrewAssignment, Employee, EmployeeSkill, Operation, ScheduleChange, Shift, ShiftAttendance, Site, Skill,
)
from .normalize import LevelError, display_name, normalize_document, normalize_key, normalize_text, parse_level
from .scheduling import shift_window, site_shifts

SUPERVISE_PERM = "supervise_crew"
MAX_IMPORT_ROWS = 5000


def _log(site, description, user=None):
    ScheduleChange.objects.create(site=site, action=ScheduleChange.Action.STAFF, description=description[:255],
                                  user=user if user is not None and user.is_authenticated else None)


# ---------------------------------------------------------------- supervisores
def supervisors():
    """Usuarios activos con el permiso de supervisor (por grupo o directo). Los superusuarios no se listan
    salvo que estén en el grupo."""
    User = get_user_model()
    return (User.objects.filter(is_active=True)
            .filter(Q(groups__permissions__codename=SUPERVISE_PERM, groups__permissions__content_type__app_label="mes_core")
                    | Q(user_permissions__codename=SUPERVISE_PERM, user_permissions__content_type__app_label="mes_core"))
            .distinct().order_by("first_name", "last_name", "username"))


def user_label(user) -> str:
    if user is None:
        return ""
    return user.get_full_name() or user.get_username()


# ---------------------------------------------------------------- quién puede editar a quién
def manages_all(user) -> bool:
    """Planeación, Jefe de planta y administradores gestionan a todo el personal; un supervisor, solo a su equipo."""
    return (user.is_superuser or user.has_perm("mes_core.assign_supervisor")
            or not user.has_perm("mes_core.supervise_crew"))


def team_scope(user):
    """El supervisor dueño del equipo cuando el usuario solo gestiona su equipo; None si gestiona a todos."""
    return None if manages_all(user) else user


def can_edit_employee(user, emp: Employee) -> bool:
    """Un supervisor edita a los de su equipo y a los que aún no tienen supervisor (al editarlos pasan a su equipo)."""
    return manages_all(user) or emp.supervisor_id in (None, user.pk)


def editable_employees(user, qs):
    return qs if manages_all(user) else qs.filter(Q(supervisor=user) | Q(supervisor__isnull=True))


@transaction.atomic
def assign_supervisor(ops, supervisor, user=None) -> int:
    """Asigna (o quita, con supervisor=None) el supervisor responsable de varias operaciones. Devuelve cuántas cambiaron."""
    if supervisor is not None and not supervisors().filter(pk=supervisor.pk).exists():
        raise ValidationError(f"{user_label(supervisor)} no tiene el rol de supervisor.")
    now = timezone.now()
    changed = []
    for op in ops:
        if op.supervisor_id == (supervisor.pk if supervisor else None):
            continue
        op.supervisor = supervisor
        op.supervisor_assigned_by = user if user is not None and user.is_authenticated else None
        op.supervisor_assigned_at = now if supervisor else None
        op.supervisor_auto = False  # elegido a mano: la regla de línea y turno ya no lo cambia
        op.save(update_fields=["supervisor", "supervisor_assigned_by", "supervisor_assigned_at", "supervisor_auto"])
        changed.append(op)
    by_site: dict[int, list[Operation]] = {}
    for op in changed:
        by_site.setdefault(op.order.site_id, []).append(op)
    for ops_site in by_site.values():
        names = ", ".join(f"{o.order.number}/{o.sequence:02d}" for o in ops_site[:8]) + ("…" if len(ops_site) > 8 else "")
        who = user_label(supervisor) if supervisor else "sin supervisor"
        _log(ops_site[0].order.site, f"Supervisor {who}: {names}", user)
    return len(changed)


# ---------------------------------------------------------------- supervisor por línea y turno (regla)
def line_rules(site) -> dict[tuple[int, int], object]:
    """{(línea, turno): supervisor} de la planta (solo supervisores vigentes)."""
    from .models import LineSupervisor

    valid = set(supervisors().values_list("pk", flat=True))
    return {(r.work_center_id, r.shift_id): r.supervisor
            for r in LineSupervisor.objects.filter(work_center__site=site).select_related("supervisor")
            if r.supervisor_id in valid}


def rule_supervisor(op: Operation, rules: dict, shifts=None):
    if not op.work_center_id or not op.planned_start:
        return None
    shift, _ = shift_day_of(op.order.site if shifts is None else None, op.planned_start, shifts)
    return rules.get((op.work_center_id, shift.pk)) if shift else None


def apply_line_rules(ops, rules: dict | None = None, shifts=None) -> int:
    """Pone el supervisor de la regla a las operaciones sin supervisor o con supervisor por regla (no toca los
    elegidos a mano ni las terminadas). Devuelve cuántas cambiaron."""
    ops = [o for o in ops if o.status != Operation.Status.DONE and (o.supervisor_id is None or o.supervisor_auto)]
    if not ops:
        return 0
    site = ops[0].order.site
    rules = line_rules(site) if rules is None else rules
    if not rules and not any(o.supervisor_auto for o in ops):
        return 0
    shifts = site_shifts(site) if shifts is None else shifts
    now, changed = timezone.now(), []
    for op in ops:
        target = rule_supervisor(op, rules, shifts)
        target_id = target.pk if target else None
        if target_id == op.supervisor_id:
            continue
        if target is None and not op.supervisor_auto:
            continue
        Operation.objects.filter(pk=op.pk).update(supervisor_id=target_id, supervisor_auto=target is not None,
                                                  supervisor_assigned_by=None,
                                                  supervisor_assigned_at=now if target else None)
        op.supervisor_id, op.supervisor_auto = target_id, target is not None
        changed.append(op.pk)
    crew = list(CrewAssignment.objects.filter(operation_id__in=changed).values_list("employee_id", flat=True))
    if crew:  # su cuadrilla pasa al nuevo supervisor
        sync_crew_supervisors(crew)
    return len(changed)


def apply_rules_to_site(site, user=None) -> int:
    """Aplica las reglas a las operaciones abiertas de hoy en adelante de la planta."""
    from .models import WorkOrder

    start = timezone.now() - timedelta(hours=12)
    ops = list(Operation.objects.filter(order__site=site, order__status__in=WorkOrder.OPEN_STATUSES,
                                        planned_end__gt=start).select_related("order__site"))
    n = apply_line_rules(ops)
    if n:
        _log(site, f"Supervisor por línea y turno: {n} operación(es) asignadas por regla", user)
    return n


def _on_operation_saved(sender, instance, created, raw=False, update_fields=None, **kwargs):
    if raw:
        return
    if update_fields is not None and not {"planned_start", "planned_end", "work_center"} & set(update_fields):
        return
    if instance.supervisor_id and not instance.supervisor_auto:
        return
    if not instance.planned_start or not instance.work_center_id:
        return
    try:
        apply_line_rules([instance])
    except Exception:  # noqa: BLE001 - una regla mal configurada nunca debe impedir guardar la programación
        import logging

        logging.getLogger(__name__).exception("No se pudo aplicar la regla de supervisor a %s", instance.pk)


def _on_operation_crew(sender, instance, created, raw=False, update_fields=None, **kwargs):
    """Al terminar una operación (o cambiarle el supervisor) su cuadrilla queda libre o pasa al nuevo supervisor."""
    if raw or created:
        return
    if update_fields is not None and not {"status", "supervisor"} & set(update_fields):
        return
    ids = list(CrewAssignment.objects.filter(operation_id=instance.pk).values_list("employee_id", flat=True))
    if ids:
        sync_crew_supervisors(ids)


def connect_signals():
    from django.db.models.signals import post_save

    post_save.connect(_on_operation_saved, sender=Operation, dispatch_uid="mes_line_supervisor_rule")
    post_save.connect(_on_operation_crew, sender=Operation, dispatch_uid="mes_crew_supervisor")


# ---------------------------------------------------------------- cuadrilla → supervisor (temporal)
def _crew_boss(ca: CrewAssignment, sup_ids: set[int]):
    """El supervisor con quien queda la persona: el responsable de la operación o, si no tiene, quien armó la
    cuadrilla (si es supervisor)."""
    op = ca.operation
    if op.supervisor_id:
        return op.supervisor_id
    return ca.assigned_by_id if ca.assigned_by_id in sup_ids else None


def _current_assignment(cas: list[CrewAssignment], now):
    """De sus cuadrillas abiertas, la que cuenta: la que está en curso; si no, la próxima; si no, la más antigua."""
    def win(ca):
        return ca.operation.planned_start, ca.operation.planned_end
    running = [ca for ca in cas if win(ca)[0] and win(ca)[1] and win(ca)[0] <= now <= win(ca)[1]]
    if running:
        return running[0]
    upcoming = [ca for ca in cas if win(ca)[1] and win(ca)[1] >= now]
    return (upcoming or cas or [None])[0]


@transaction.atomic
def sync_crew_supervisors(employee_ids=None, now=None) -> int:
    """Deja a cada persona con el supervisor de la cuadrilla en la que está (operación abierta) y, cuando ya no está
    en ninguna, la devuelve libre (o a su supervisor habitual). Sin ``employee_ids`` revisa a todos los que están
    con un supervisor por cuadrilla o en una cuadrilla abierta (lo corre la revisión automática cada 5 minutos).
    Devuelve cuántos cambiaron."""
    from django.db.models import F

    from .models import WorkOrder

    now = now or timezone.now()
    if employee_ids is None:  # revisión general: los prestados y los que están en alguna cuadrilla abierta
        employee_ids = set(Employee.objects.filter(crew_borrowed=True).values_list("pk", flat=True)) | set(
            CrewAssignment.objects.exclude(operation__status=Operation.Status.DONE)
            .filter(operation__order__status__in=WorkOrder.OPEN_STATUSES).values_list("employee_id", flat=True))
    emps = list(Employee.objects.select_for_update().filter(pk__in=list(employee_ids)))
    if not emps:
        return 0
    cas: dict[int, list[CrewAssignment]] = {}
    for ca in (CrewAssignment.objects.filter(employee_id__in=[e.pk for e in emps])
               .exclude(operation__status=Operation.Status.DONE)
               .filter(operation__order__status__in=WorkOrder.OPEN_STATUSES)
               .select_related("operation").order_by(F("operation__planned_start").asc(nulls_last=True), "pk")):
        cas.setdefault(ca.employee_id, []).append(ca)
    sup_ids = set(supervisors().values_list("pk", flat=True))
    changed = 0
    for e in emps:
        ca = _current_assignment(cas.get(e.pk, []), now)
        boss = _crew_boss(ca, sup_ids) if ca else None
        before = (e.supervisor_id, e.crew_borrowed, e.crew_operation_id, e.home_supervisor_id)
        if boss is not None:
            if not e.crew_borrowed:
                if e.supervisor_id == boss:
                    continue  # ya es de su equipo habitual: nada que prestar ni devolver
                e.home_supervisor_id = e.supervisor_id
            e.supervisor_id, e.crew_borrowed, e.crew_operation_id = boss, True, ca.operation_id
        elif e.crew_borrowed:  # terminó (o lo sacaron): vuelve a quedar libre o con su supervisor de siempre
            e.supervisor_id, e.crew_borrowed, e.crew_operation_id = e.home_supervisor_id, False, None
            e.home_supervisor_id = None
        if (e.supervisor_id, e.crew_borrowed, e.crew_operation_id, e.home_supervisor_id) != before:
            e.save(update_fields=["supervisor", "crew_borrowed", "crew_operation", "home_supervisor", "updated_at"])
            changed += 1
    return changed


def make_permanent(qs) -> None:
    """Cuando alguien cambia a mano el supervisor, deja de ser préstamo de cuadrilla (ya no se devuelve solo)."""
    qs.filter(crew_borrowed=True).update(crew_borrowed=False, crew_operation=None, home_supervisor=None)


# ---------------------------------------------------------------- turno y día de una operación
def shift_day_of(site, dt: datetime | None, shifts=None) -> tuple[Shift | None, date | None]:
    """Turno y día de turno al que pertenece un instante (el turno de noche cuenta para el día en que termina)."""
    if dt is None:
        return None, None
    local = timezone.localtime(dt)
    for sh in (shifts if shifts is not None else site_shifts(site)):
        for d in (local.date() - timedelta(days=1), local.date(), local.date() + timedelta(days=1)):
            a, b = shift_window(sh, d)
            if a <= local < b:
                return sh, d
    return None, local.date()


def operation_day(op: Operation, shifts=None) -> date | None:
    return shift_day_of(op.order.site, op.planned_start, shifts)[1]


# ---------------------------------------------------------------- asistencia
@dataclass
class AttendanceEntry:
    employee: Employee
    status: str  # "" = quitar la confirmación
    notes: str = ""


@transaction.atomic
def save_attendance(day: date, entries: list[AttendanceEntry], user=None) -> dict:
    """Guarda la asistencia de varios operarios en un día y actualiza las ausencias del tablero de capacidad."""
    valid = set(Attendance.Status.values)
    existing = {a.employee_id: a for a in Attendance.objects.filter(day=day, employee__in=[e.employee for e in entries])}
    touched_shifts: set[int] = set()
    counts = {"saved": 0, "cleared": 0}
    for e in entries:
        att = existing.get(e.employee.pk)
        notes = " ".join((e.notes or "").split())[:255]
        if not e.status:
            if att:
                if att.shift_id:
                    touched_shifts.add(att.shift_id)
                att.delete()
                counts["cleared"] += 1
            continue
        if e.status not in valid:
            raise ValidationError(f"Estado de asistencia inválido: {e.status}")
        if att and att.status == e.status and att.notes == notes:
            continue
        shift = att.shift if att and att.shift_id else e.employee.shift
        if att and att.shift_id:
            touched_shifts.add(att.shift_id)
        Attendance.objects.update_or_create(employee=e.employee, day=day, defaults={
            "status": e.status, "notes": notes, "shift": shift, "confirmed_by": user if user and user.is_authenticated else None})
        if shift:
            touched_shifts.add(shift.pk)
        counts["saved"] += 1
    sync_shift_absences(day, touched_shifts, user)
    if counts["saved"] or counts["cleared"]:
        # El personal es general: la bitácora va a la planta de los turnos tocados (o a la primera planta activa).
        site_ids = set(Shift.objects.filter(pk__in=touched_shifts).values_list("site_id", flat=True))
        qs = Attendance.objects.filter(day=day)
        present = qs.filter(status__in=Attendance.WORKING).count()
        text = (f"Asistencia {day:%d/%m}: {present} presentes, {qs.count() - present} con novedad "
                f"({counts['saved']} marcados por {user_label(user) or 'el sistema'})")
        for site in (Site.objects.filter(pk__in=site_ids) or Site.objects.filter(active=True)[:1]):
            _log(site, text, user)
    return counts


def sync_shift_absences(day: date, shift_ids, user=None) -> None:
    """Lleva al tablero de capacidad (ShiftAttendance) el número de ausentes confirmados por los supervisores."""
    for shift in Shift.objects.filter(pk__in=shift_ids):
        absent = Attendance.objects.filter(day=day, shift=shift).exclude(status__in=Attendance.WORKING).count()
        ShiftAttendance.objects.update_or_create(shift=shift, day=day, defaults={
            "absent": absent, "notes": "Asistencia confirmada por supervisores",
            "updated_by": user if user is not None and user.is_authenticated else None})


# ---------------------------------------------------------------- cuadrillas
def _window(op: Operation):
    if op.planned_start and op.planned_end:
        return op.planned_start, op.planned_end
    return None


def busy_elsewhere(op: Operation, employee_ids=None) -> dict[int, Operation]:
    """Operarios ya asignados a otra operación que se cruza en el tiempo con ``op``."""
    win = _window(op)
    if not win:
        return {}
    qs = (CrewAssignment.objects.filter(operation__planned_start__lt=win[1], operation__planned_end__gt=win[0])
          .exclude(operation=op).select_related("operation", "operation__order", "operation__work_center"))
    if employee_ids is not None:
        qs = qs.filter(employee_id__in=employee_ids)
    return {ca.employee_id: ca.operation for ca in qs}  # personal general: cuenta lo de cualquier planta


def op_label(op: Operation) -> str:
    wc = f" · {op.work_center.code}" if op.work_center_id else ""
    return f"{op.order.number} {op.name}{wc}"


@transaction.atomic
def set_crew(op: Operation, employee_ids, user=None) -> tuple[int, int]:
    """Deja asignados exactamente ``employee_ids`` a la operación. Devuelve (agregados, retirados)."""
    op = Operation.objects.select_for_update().select_related("order", "order__site").get(pk=op.pk)
    if op.status == Operation.Status.DONE:
        raise ValidationError("La operación ya terminó; no se puede cambiar su personal.")
    ids = {int(i) for i in employee_ids}
    emps = {e.pk: e for e in Employee.objects.filter(pk__in=ids)}
    errors = []
    missing = ids - set(emps)
    if missing:
        errors.append(f"{len(missing)} operario(s) no existen.")
    current = set(op.crew_assignments.values_list("employee_id", flat=True))
    new_ids = ids - current
    for e in emps.values():
        if e.pk in new_ids and not e.active:
            errors.append(f"{e.display_name} está inactivo.")
    day = operation_day(op)
    if day:
        absent = (Attendance.objects.filter(day=day, employee_id__in=new_ids).exclude(status__in=Attendance.WORKING)
                  .select_related("employee"))
        for att in absent:
            errors.append(f"{att.employee.display_name} está marcado «{att.get_status_display()}» el {day:%d/%m}.")
    for emp_id, other in busy_elsewhere(op, ids).items():
        if emp_id in emps:
            errors.append(f"{emps[emp_id].display_name} ya está en {op_label(other)} a la misma hora.")
    if errors:
        raise ValidationError(errors)
    removed = current - ids
    if removed:
        op.crew_assignments.filter(employee_id__in=removed).delete()
    CrewAssignment.objects.bulk_create([
        CrewAssignment(operation=op, employee_id=i, assigned_by=user if user and user.is_authenticated else None)
        for i in sorted(new_ids)])
    if new_ids or removed:
        sync_crew_supervisors(new_ids | removed)  # los elegidos quedan con el supervisor; los que salen, libres
        _log(op.order.site, f"{op.order.number}/{op.sequence:02d} {op.name}: {len(ids)} de {op.crew} operarios asignados"
                            f" (+{len(new_ids)} / -{len(removed)})", user)
    return len(new_ids), len(removed)


def match_skill(op: Operation, skills=None) -> Skill | None:
    """Competencia que mejor describe la operación (tipo de línea, nombre de la operación o de la etapa)."""
    keys = [normalize_key(k) for k in (
        op.work_center.kind if op.work_center_id else "", op.work_center_kind, op.name.split(" · parte ")[0],
        op.stage.name if op.stage_id else "") if k]
    keys = [k for k in keys if k]
    if not keys:
        return None
    skills = list(skills if skills is not None else Skill.objects.all())
    by_name = {s.name: s for s in skills}
    for k in keys:
        if k in by_name:
            return by_name[k]
    for k in keys:
        for s in skills:
            if len(s.name) >= 4 and (s.name in k or k in s.name):
                return s
    return None


@dataclass
class Candidate:
    employee: Employee
    attendance: Attendance | None
    level: int | None
    busy: Operation | None
    assigned: bool
    mine: bool
    skills: list = field(default_factory=list)

    @property
    def working(self) -> bool | None:
        return None if self.attendance is None else self.attendance.is_working

    @property
    def blocked_reason(self) -> str:
        if self.attendance is not None and not self.attendance.is_working:
            return self.attendance.get_status_display()
        if self.busy is not None:
            return f"En {op_label(self.busy)}"
        return ""

    @property
    def available(self) -> bool:
        return not self.blocked_reason


def crew_candidates(op: Operation, skill: Skill | None, user=None) -> list[Candidate]:
    """Todo el personal (es general) ordenado para la operación: disponibles, presentes, mejor nivel y, primero, los que
    el supervisor marcó presentes con él ese día o son de su equipo habitual."""
    emps = list(Employee.objects.filter(active=True)
                .select_related("shift", "supervisor", "crew_operation__order").prefetch_related("skills__skill"))
    day = operation_day(op)
    att = {a.employee_id: a for a in Attendance.objects.filter(day=day)} if day else {}
    busy = busy_elsewhere(op)
    assigned = set(op.crew_assignments.values_list("employee_id", flat=True))
    rows = []
    for e in emps:
        levels = sorted(((es.level, es.skill.name) for es in e.skills.all()), reverse=True)
        lvl = next((es.level for es in e.skills.all() if skill and es.skill_id == skill.pk), None)
        a = att.get(e.pk)
        with_me = bool(user and (e.supervisor_id == user.pk or (a is not None and a.confirmed_by_id == user.pk
                                                                    and a.is_working)))
        rows.append(Candidate(employee=e, attendance=a, level=lvl, busy=busy.get(e.pk),
                              assigned=e.pk in assigned, mine=with_me, skills=levels))
    rows.sort(key=lambda c: (not c.assigned, not c.available, c.working is not True, -(c.level or 0), not c.mine,
                             c.employee.full_name))
    return rows


# ---------------------------------------------------------------- carga masiva desde Excel / CSV
HEADER_ALIASES = {
    "document": {"documento", "cedula", "cc", "c.c", "identificacion", "numero de documento", "no documento",
                 "nro documento", "documento de identidad", "doc", "id"},
    "full_name": {"nombre", "nombres", "nombre completo", "operario", "empleado", "trabajador", "nombre y apellido",
                  "nombres y apellidos", "nombre del operario"},
    "last_name": {"apellido", "apellidos"},
    "position": {"cargo", "puesto", "rol", "ocupacion"},
    "shift": {"turno", "turno habitual"},
    "supervisor": {"supervisor", "jefe", "jefe inmediato", "lider", "responsable"},
    "phone": {"telefono", "celular", "movil", "contacto", "tel"},
    "notes": {"conocimientos", "observaciones", "notas", "comentarios", "experiencia"},
    "active": {"activo", "estado"},
}
IGNORED_HEADERS = {"#", "n", "no", "n°", "nº", "item", "consecutivo", "fila", "planta", "sede"}
_ALIAS = {alias: key for key, aliases in HEADER_ALIASES.items() for alias in aliases}
_TRUE = {"si", "s", "1", "activo", "activa", "true", "verdadero", "x"}
_FALSE = {"no", "0", "inactivo", "inactiva", "retirado", "retirada", "false", "falso"}


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    levels: int = 0
    new_skills: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def _read_rows(raw: bytes, filename: str) -> list[list]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        try:
            wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - archivo corrupto o no es Excel
            raise ValidationError(f"No se pudo leer el Excel: {exc}") from None
        ws = wb["Operarios"] if "Operarios" in wb.sheetnames else wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
        return rows
    if name.endswith((".csv", ".txt")):
        for enc in ("utf-8-sig", "cp1252"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        first = text.splitlines()[0] if text else ""
        delim = ";" if first.count(";") > first.count(",") else ","
        return [list(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]
    if name.endswith(".xls"):
        raise ValidationError("El formato .xls (Excel 97-2003) no se admite: guarda el archivo como .xlsx.")
    raise ValidationError("Sube un archivo de Excel (.xlsx) o CSV.")


def _find_header(rows) -> tuple[int, list[str]]:
    """Primera fila (de las 10 primeras) que tenga columnas de documento y nombre: permite títulos encima."""
    for i, row in enumerate(rows[:10]):
        keys = [normalize_key(c) for c in row]
        mapped = {_ALIAS.get(k) for k in keys}
        if "document" in mapped and "full_name" in mapped:
            return i, keys
    raise ValidationError("No encontré la fila de encabezados: debe tener al menos las columnas «documento» y «nombre».")


def all_shifts():
    """Turnos activos de todas las plantas (el personal es general)."""
    return list(Shift.objects.filter(active=True).select_related("site").order_by("site__code", "start_time"))


def shift_label(shift, shifts=None) -> str:
    """«Mañana» si el nombre es único; «CEDI · Mañana» si otra planta tiene un turno con el mismo nombre."""
    shifts = shifts if shifts is not None else all_shifts()
    same = sum(1 for s in shifts if normalize_key(s.name) == normalize_key(shift.name))
    return f"{shift.site.code} · {shift.name}" if same > 1 else shift.name


def shift_index(shifts=None) -> dict[str, list]:
    """Texto normalizado → turnos: por nombre («mañana») y por planta y nombre («cedi mañana», «cedi · mañana»)."""
    out: dict[str, list] = {}
    for s in (shifts if shifts is not None else all_shifts()):
        for key in {normalize_key(s.name), normalize_key(f"{s.site.code} {s.name}"),
                    normalize_key(f"{s.site.code} · {s.name}"), normalize_key(f"{s.name} ({s.site.code})")}:
            if s not in out.setdefault(key, []):
                out[key].append(s)
    return out


def import_employees(raw: bytes, filename: str, site: Site | None = None, user=None, team_of=None) -> ImportResult:
    """Crea o actualiza operarios (por documento). Columnas extra = competencias con su nivel en la celda.
    Todo o nada: si alguna fila tiene error no se guarda ninguna. El personal es general (de todas las plantas):
    ``site`` solo se usa para la bitácora.

    ``team_of`` (un supervisor): todos los operarios quedan en su equipo y no puede tocar los de otro supervisor."""
    res = ImportResult()
    try:
        rows = _read_rows(raw, filename)
        h, headers = _find_header(rows)
    except ValidationError as exc:
        res.errors = exc.messages
        return res
    cols: dict[str, int] = {}
    skill_cols: list[tuple[int, str]] = []
    for idx, key in enumerate(headers):
        if not key or key in IGNORED_HEADERS:
            continue
        target = _ALIAS.get(key)
        if target and target not in cols:
            cols[target] = idx
        elif not target:
            skill_cols.append((idx, key))
    data = rows[h + 1:]
    if len(data) > MAX_IMPORT_ROWS:
        res.errors = [f"El archivo tiene {len(data)} filas; el máximo por carga es {MAX_IMPORT_ROWS}."]
        return res

    shift_list = all_shifts()
    shifts = shift_index(shift_list)
    shift_names = sorted({shift_label(s, shift_list) for s in shift_list})
    sup_index = {}
    for u in supervisors():
        sup_index[u.get_username().lower()] = u
        if u.get_full_name():
            sup_index.setdefault(normalize_text(u.get_full_name()), u)
    existing_skills = {s.name: s for s in Skill.objects.all()}
    seen_docs: dict[str, int] = {}

    def cell(row, key):
        i = cols.get(key)
        return row[i] if i is not None and i < len(row) else None

    with transaction.atomic():
        for n, row in enumerate(data, start=h + 2):
            if not any(c not in (None, "") and str(c).strip() for c in row):
                continue
            res.rows += 1
            errs = []
            doc = normalize_document(cell(row, "document"))
            name = normalize_text(cell(row, "full_name"))
            if cell(row, "last_name"):
                name = normalize_text(f"{name} {normalize_text(cell(row, 'last_name'))}")
            if not doc:
                errs.append("falta el documento")
            elif doc in seen_docs:
                errs.append(f"documento {doc} repetido (también en la fila {seen_docs[doc]})")
            else:
                seen_docs[doc] = n
            emp = Employee.objects.filter(document=doc).select_related("supervisor").first() if doc else None
            if not name and emp is None:
                errs.append("falta el nombre")
            if team_of is not None and emp is not None and emp.supervisor_id not in (None, team_of.pk):
                errs.append(f"el documento {doc} ya está en el equipo de {user_label(emp.supervisor)}")

            shift = supervisor = None
            raw_shift = normalize_key(cell(row, "shift"))
            if raw_shift:
                found = shifts.get(raw_shift, [])
                if len(found) == 1:
                    shift = found[0]
                elif found:
                    errs.append(f"turno «{raw_shift}» existe en varias plantas: escribe "
                                + " o ".join(f"«{s.site.code} · {s.name}»" for s in found))
                else:
                    errs.append(f"turno «{raw_shift}» no existe (hay: {', '.join(shift_names) or 'ninguno'})")
            raw_sup = normalize_text(cell(row, "supervisor"))
            if raw_sup:
                supervisor = sup_index.get(raw_sup)
                if supervisor is None:
                    errs.append(f"supervisor «{raw_sup}» no encontrado (usa su usuario o nombre; debe tener el rol Supervisor)")
                elif team_of is not None and supervisor.pk != team_of.pk:
                    errs.append(f"solo puedes cargar operarios a tu equipo (la fila dice «{raw_sup}»)")
            if team_of is not None:
                supervisor = team_of
            active = None
            raw_active = normalize_key(cell(row, "active"))
            if raw_active:
                if raw_active in _TRUE:
                    active = True
                elif raw_active in _FALSE:
                    active = False
                else:
                    errs.append(f"activo «{raw_active}» no reconocido (usa sí o no)")
            levels = []
            for idx, skill_name in skill_cols:
                value = row[idx] if idx < len(row) else None
                if value is None or str(value).strip() == "":
                    continue
                try:
                    levels.append((skill_name, parse_level(value)))
                except LevelError as exc:
                    errs.append(f"{skill_name}: {exc}")
            if errs:
                res.errors.append(f"Fila {n}: " + "; ".join(errs))
                continue

            created = emp is None
            if created:
                emp = Employee(document=doc)
            if name:
                emp.full_name = name
            for key in ("position", "phone", "notes"):
                v = cell(row, key)
                if v not in (None, "") and str(v).strip():
                    setattr(emp, key, str(int(v) if isinstance(v, float) and v.is_integer() else v))
            if shift:
                emp.shift = shift
            if supervisor:
                emp.supervisor = supervisor
            if active is not None:
                emp.active = active
            emp.save()
            res.created += created
            res.updated += not created
            for skill_name, level in levels:
                skill = existing_skills.get(skill_name)
                if skill is None:
                    skill = Skill.objects.create(name=skill_name)
                    existing_skills[skill.name] = skill
                    res.new_skills.append(skill.name)
                if level is None:
                    EmployeeSkill.objects.filter(employee=emp, skill=skill).delete()
                else:
                    EmployeeSkill.objects.update_or_create(employee=emp, skill=skill, defaults={
                        "level": level, "updated_by": user if user and user.is_authenticated else None})
                res.levels += 1
        if res.errors:
            transaction.set_rollback(True)
            res.created = res.updated = res.levels = 0
            res.new_skills = []
            return res
        log_site = site or Site.objects.filter(active=True).first()
        if (res.created or res.updated) and log_site is not None:
            _log(log_site, f"Carga masiva de operarios: {res.created} nuevos, {res.updated} actualizados, "
                       f"{res.levels} niveles de competencia ({filename})", user)
    return res


def template_xlsx(site: Site | None = None, team_of=None) -> bytes:
    """Plantilla de Excel para la carga masiva, con listas desplegables de turno, supervisor y nivel.
    Para un supervisor (``team_of``) la columna supervisor solo ofrece su propio usuario."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    skills = list(Skill.objects.order_by("name").values_list("name", flat=True)[:12]) or ["picking", "etiquetado", "empaque"]
    shift_list = all_shifts()
    shifts = list(dict.fromkeys(shift_label(s, shift_list) for s in shift_list))  # de todas las plantas
    sups = [team_of.get_username()] if team_of is not None else [u.get_username() for u in supervisors()]
    headers = ["documento", "nombre completo", "cargo", "turno", "supervisor", "telefono", "conocimientos", *skills]

    wb = Workbook()
    ws = wb.active
    ws.title = "Operarios"
    ws.append(headers)
    examples = [
        ["1020345678", "Ana María Gómez", "Operaria", shifts[0] if shifts else "", sups[0] if sups else "",
         "3001234567", "Montacargas, manejo de RF", "experto", "intermedio"],
        ["79123456", "JULIÁN PÉREZ", "Auxiliar", shifts[-1] if shifts else "", "", "", "", "novato"],
    ]
    for ex in examples:
        ws.append(ex[:len(headers)])
    head_fill = PatternFill("solid", fgColor="2F3E8F")
    skill_fill = PatternFill("solid", fgColor="625499")
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=i)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = skill_fill if i > 7 else head_fill
        c.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(h) + 4)
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["G"].width = 30
    ws.freeze_panes = "C2"

    def add_list(values, col_from, col_to):
        if not values:
            return
        dv = DataValidation(type="list", formula1='"' + ",".join(values)[:250] + '"', allow_blank=True,
                            showErrorMessage=False)
        ws.add_data_validation(dv)
        dv.add(f"{get_column_letter(col_from)}2:{get_column_letter(col_to)}{MAX_IMPORT_ROWS + 1}")

    add_list(shifts, 4, 4)
    add_list(sups, 5, 5)
    add_list(["novato", "intermedio", "experto", "ninguno"], 8, len(headers))

    info = wb.create_sheet("Instrucciones")
    lines = [
        "Carga masiva de operarios",
        "",
        "• Obligatorias: documento y nombre completo. El documento identifica al operario: si ya existe, se actualiza.",
        "• El personal es general (sirve para todas las plantas).",
        "• Opcionales: cargo, turno habitual (si dos plantas tienen un turno con el mismo nombre, escribe «PLANTA · turno»), "
        "supervisor (usuario o nombre), teléfono, conocimientos.",
        "• Columnas moradas = competencias de la matriz de polivalencia. Agrega las que necesites (una columna por competencia).",
        "  En cada celda escribe el nivel: novato, intermedio o experto (también sirve 1, 2, 3, básico, avanzado…).",
        "  Celda vacía = no cambia. «ninguno» o 0 = quita esa competencia al operario.",
        "• No te preocupes por mayúsculas, tildes o espacios: el sistema normaliza todo (JULIÁN PÉREZ → julian perez).",
        "• Los puntos y espacios del documento se quitan (1.020.345.678 → 1020345678).",
        "• Si una fila tiene error no se carga nada; el sistema te dice qué fila corregir.",
        "• Borra las filas de ejemplo antes de cargar.",
    ]
    for ln in lines:
        info.append([ln])
    info["A1"].font = Font(bold=True, size=14)
    info.column_dimensions["A"].width = 120
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def employee_skill_summary(emp: Employee) -> list[tuple[str, int]]:
    return sorted(((es.skill.name, es.level) for es in emp.skills.all()), key=lambda x: (-x[1], x[0]))


__all__ = [
    "AttendanceEntry", "Candidate", "ImportResult", "assign_supervisor", "busy_elsewhere", "crew_candidates",
    "display_name", "import_employees", "match_skill", "operation_day", "save_attendance", "set_crew", "shift_day_of",
    "supervisors", "sync_crew_supervisors", "make_permanent", "template_xlsx", "user_label",
]
