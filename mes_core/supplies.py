"""Insumos: inventario por planta, entregas a cada pedido y liberación a planta.

Flujo del pedido:
    Planeación planea y programa → «Enviar a insumos» (estado *En insumos*)
    → Insumos confirma lo que necesita, entrega cantidades a cada proceso / supervisor y libera su parte
      (solo, al entregar el 100 %, si la planta tiene liberación automática) o «Devolver a planeación» con motivo
    → en paralelo, logística merca la mercancía (logistics.py)
    → con las dos partes listas el pedido pasa solo a planta (estado *Liberada*)
    → el supervisor ejecuta (cuadrilla y reportes de producción).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from . import services
from .models import (
    Operation, Product, ScheduleChange, Site, SupplyMovement, SupplyRequirement, SupplyStock, WorkOrder,
)
from .normalize import normalize_key
from .plugins import registry

ZERO = Decimal("0")
SEND_FROM = (WorkOrder.Status.DRAFT, WorkOrder.Status.PLANNED)
DELIVER_IN = (WorkOrder.Status.SUPPLIES, WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS)


def fmt(q) -> str:
    q = Decimal(q)
    return f"{q.normalize():f}" if q == q.to_integral() else f"{q.normalize():f}".rstrip("0")


def _user(user):
    return user if user is not None and getattr(user, "is_authenticated", False) else None


def _log(site, text, user=None):
    ScheduleChange.objects.create(site=site, action=ScheduleChange.Action.SUPPLIES, description=text[:255], user=_user(user))


def _dec(value, label) -> Decimal:
    try:
        d = Decimal(str(value).strip().replace(",", ".")) if str(value).strip() else ZERO
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{label}: «{value}» no es un número.") from None
    if d < 0:
        raise ValidationError(f"{label} no puede ser negativo.")
    return d


# ---------------------------------------------------------------- inventario
def ensure_stock(site: Site, product: Product) -> SupplyStock:
    if product.kind != Product.Kind.SUPPLY:  # el inventario de insumos nunca lleva productos en proceso
        raise ValidationError(product.kind_error(Product.Kind.SUPPLY))
    stock, _ = SupplyStock.objects.get_or_create(site=site, product=product)
    return stock


def committed(site: Site) -> dict[int, Decimal]:
    """Lo que falta entregar a pedidos abiertos, por insumo (comprometido, aún en bodega)."""
    rows = (SupplyRequirement.objects.filter(stock__site=site, order__status__in=WorkOrder.OPEN_STATUSES,
                                             qty_required__gt=F("qty_released"))
            .values("stock_id").annotate(req=Sum("qty_required"), rel=Sum("qty_released")))
    return {r["stock_id"]: r["req"] - r["rel"] for r in rows}


def stock_state(stock: SupplyStock, pending: Decimal = ZERO) -> tuple[str, str]:
    """(clase, texto) para el semáforo del inventario."""
    if stock.on_hand <= 0:
        return "bad", "Agotado"
    if stock.on_hand - pending < 0:
        return "bad", "No alcanza"
    if stock.safety_stock and stock.on_hand - pending < stock.safety_stock:
        return "warn", "Bajo stock de seguridad"
    return "ok", "OK"


def _move(stock: SupplyStock, kind: str, qty: Decimal, user=None, note: str = "", order=None, requirement=None,
          deliver_to=None) -> SupplyMovement:
    stock.on_hand += qty
    if stock.on_hand < 0:
        raise ValidationError(f"{stock.product.name}: solo hay {fmt(stock.on_hand - qty)} {stock.product.unit} en "
                              f"existencia, no alcanza para entregar {fmt(-qty)}.")
    stock.save(update_fields=["on_hand", "updated_at"])
    if stock.safety_stock and stock.on_hand < stock.safety_stock <= stock.on_hand - qty:  # acaba de cruzar el mínimo
        from . import notify

        transaction.on_commit(lambda: notify._safe(notify.low_stock, stock))
    return SupplyMovement.objects.create(stock=stock, kind=kind, qty=qty, balance=stock.on_hand, order=order,
                                         requirement=requirement, deliver_to=deliver_to, note=note[:255], user=_user(user))


@transaction.atomic
def receive(stock: SupplyStock, qty, user=None, note: str = "") -> SupplyMovement:
    """Entrada de insumo (compra, traslado, devolución de planta)."""
    stock = SupplyStock.objects.select_for_update().select_related("product").get(pk=stock.pk)
    qty = _dec(qty, "La cantidad")
    if qty <= 0:
        raise ValidationError("La cantidad de la entrada debe ser mayor que cero.")
    return _move(stock, SupplyMovement.Kind.IN, qty, user, note)


@transaction.atomic
def adjust(stock: SupplyStock, counted, user=None, note: str = "") -> SupplyMovement | None:
    """Ajuste por conteo físico: deja la existencia en ``counted``."""
    stock = SupplyStock.objects.select_for_update().select_related("product").get(pk=stock.pk)
    counted = _dec(counted, "La existencia contada")
    delta = counted - stock.on_hand
    if delta == 0:
        return None
    return _move(stock, SupplyMovement.Kind.ADJUST, delta, user, note or "Ajuste por conteo")


# ---------------------------------------------------------------- pedido: enviar / devolver
@transaction.atomic
def send_to_supplies(order: WorkOrder, user=None) -> WorkOrder:
    """Planeación termina y pasa el pedido al equipo de insumos. Precarga lo que sugieran los módulos."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    if order.status not in SEND_FROM:
        raise ValidationError(f"El pedido está «{order.get_status_display()}»: solo se envían a insumos pedidos planeados.")
    if not order.operations.exists():
        raise ValidationError("El pedido no tiene etapas; primero planéalo.")
    order.status = WorkOrder.Status.SUPPLIES
    order.sent_to_supplies_at = timezone.now()
    order.supplies_note = ""
    order.supplies_released_by, order.supplies_released_at = None, None
    order.save(update_fields=["status", "sent_to_supplies_at", "supplies_note", "supplies_released_by",
                              "supplies_released_at"])
    if not order.supply_lines.exists():
        if not add_bom_lines(order, user):  # sin lista de materiales: lo que sugieran los módulos (ficha)
            for product, qty in registry.supply_requirements(order):
                if qty and qty > 0:
                    SupplyRequirement.objects.create(order=order, stock=ensure_stock(order.site, product),
                                                     qty_required=Decimal(qty), note="Sugerido por la ficha del producto")
    _log(order.site, f"{order.number} enviado a insumos", user)
    from . import notify

    transaction.on_commit(lambda: notify._safe(notify.order_to_supplies, order, _user(user)))
    return order


def add_bom_lines(order: WorkOrder, user=None) -> int:
    """Agrega las líneas que pide la lista de materiales y aún no están en el pedido. Devuelve cuántas agregó."""
    from .bom import requirements

    have = {(r.stock.product_id, r.operation_id) for r in order.supply_lines.select_related("stock")}
    added = 0
    for need in requirements(order):
        key = (need.component.pk, need.operation.pk if need.operation else None)
        if key in have:
            continue
        SupplyRequirement.objects.create(order=order, stock=ensure_stock(order.site, need.component),
                                         operation=need.operation, qty_required=need.qty, note=need.note[:255],
                                         updated_by=_user(user))
        have.add(key)
        added += 1
    return added


@transaction.atomic
def return_to_planning(order: WorkOrder, reason: str, user=None) -> WorkOrder:
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != WorkOrder.Status.SUPPLIES:
        raise ValidationError("Solo se devuelven pedidos que están en insumos.")
    reason = " ".join((reason or "").split())
    if not reason:
        raise ValidationError("Escribe el motivo para que planeación sepa qué ajustar (p. ej. falta etiqueta X).")
    scheduled = order.operations.exclude(planned_start__isnull=True).exists()
    order.status = WorkOrder.Status.PLANNED if scheduled else WorkOrder.Status.DRAFT
    order.supplies_note = f"Devuelto por insumos: {reason}"[:255]
    order.supplies_released_by, order.supplies_released_at = None, None  # al volver, insumos debe liberar de nuevo
    order.save(update_fields=["status", "supplies_note", "supplies_released_by", "supplies_released_at"])
    _log(order.site, f"{order.number} devuelto a planeación: {reason}", user)
    return order


# ---------------------------------------------------------------- pedido: líneas de insumos
@dataclass
class LineInput:
    pk: int | None
    stock_id: int | None
    qty_required: object
    operation_id: int | None = None
    deliver_to_id: int | None = None
    note: str = ""
    delete: bool = False
    deliver_now: object = ""


@transaction.atomic
def save_lines(order: WorkOrder, lines: list[LineInput], user=None) -> list[SupplyRequirement]:
    """Crea, actualiza o quita las líneas de insumos de un pedido (no mueve inventario)."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    if order.status not in DELIVER_IN:
        raise ValidationError("Las líneas de insumos se editan cuando el pedido está en insumos o en planta.")
    existing = {r.pk: r for r in order.supply_lines.select_related("stock__product")}
    ops = set(order.operations.values_list("pk", flat=True))
    stocks = {s.pk: s for s in SupplyStock.objects.filter(site=order.site).select_related("product")}
    out, errors = [], []
    for ln in lines:
        req = existing.get(ln.pk) if ln.pk else None
        if ln.pk and req is None:
            errors.append("Una línea ya no existe; recarga la página.")
            continue
        if ln.delete:
            if req and req.qty_released > 0:
                errors.append(f"{req.stock.product.name}: ya tiene entregas, no se puede quitar (ajusta el requerido).")
            elif req:
                req.delete()
            continue
        stock = req.stock if req else stocks.get(ln.stock_id)
        if stock is None:
            errors.append("Elige el insumo de cada línea nueva.")
            continue
        try:
            qty = _dec(ln.qty_required, f"{stock.product.name}: requerido")
        except ValidationError as exc:
            errors.extend(exc.messages)
            continue
        if qty <= 0:
            errors.append(f"{stock.product.name}: el requerido debe ser mayor que cero.")
            continue
        if req and qty < req.qty_released:
            errors.append(f"{stock.product.name}: ya se entregaron {fmt(req.qty_released)}; el requerido no puede ser menor.")
            continue
        if ln.operation_id and ln.operation_id not in ops:
            errors.append(f"{stock.product.name}: la etapa no es de este pedido.")
            continue
        req = req or SupplyRequirement(order=order, stock=stock)
        req.qty_required, req.operation_id, req.deliver_to_id = qty, ln.operation_id, ln.deliver_to_id
        req.note, req.updated_by = (ln.note or "").strip()[:255], _user(user)
        req.save()
        out.append(req)
    if errors:
        raise ValidationError(errors)
    _refresh_pct(order)
    if out:
        _auto_release(order, user)  # p. ej. bajaron el requerido a lo ya entregado
    return out


@transaction.atomic
def deliver(order: WorkOrder, quantities: dict[int, object], user=None, note: str = "") -> list[SupplyMovement]:
    """Entrega insumos al proceso: descuenta inventario y suma a lo entregado de cada línea."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    if order.status not in DELIVER_IN:
        raise ValidationError("Solo se entregan insumos a pedidos en insumos o en planta.")
    lines = {r.pk: r for r in order.supply_lines.select_related("stock__product", "operation__supervisor")}
    moves, errors = [], []
    for pk, raw in quantities.items():
        req = lines.get(pk)
        if req is None:
            continue
        try:
            qty = _dec(raw, f"{lines[pk].stock.product.name}: entregar")
        except ValidationError as exc:
            errors.extend(exc.messages)
            continue
        if qty == 0:
            continue
        if qty > req.qty_pending:
            errors.append(f"{req.stock.product.name}: faltan {fmt(req.qty_pending)} por entregar, no {fmt(qty)}. "
                          "Si el proceso necesita más, sube primero el requerido.")
            continue
        stock = SupplyStock.objects.select_for_update().select_related("product").get(pk=req.stock_id)
        to = req.deliver_to or (req.operation.supervisor if req.operation_id else None)
        try:
            moves.append(_move(stock, SupplyMovement.Kind.OUT, -qty, user, note or f"Entrega a {order.number}",
                               order=order, requirement=req, deliver_to=to))
        except ValidationError as exc:
            errors.extend(exc.messages)
            continue
        req.qty_released += qty
        req.updated_by = _user(user)
        req.save(update_fields=["qty_released", "updated_by", "updated_at"])
    if errors:
        raise ValidationError(errors)
    if moves:
        _refresh_pct(order)
        _log(order.site, f"{order.number}: entregados {len(moves)} insumo(s)", user)
        _auto_release(order, user)
    return moves


def completeness(order: WorkOrder) -> tuple[int, list[SupplyRequirement]]:
    """(% entregado, líneas incompletas). Sin líneas = 100 %."""
    lines = list(order.supply_lines.select_related("stock__product"))
    if not lines:
        return 100, []
    pct = sum(min(Decimal(1), r.qty_released / r.qty_required) for r in lines) / len(lines) * 100
    return int(pct), [r for r in lines if r.qty_pending > 0]


def _refresh_pct(order: WorkOrder) -> None:
    pct, _ = completeness(order)
    if order.supply_lines.exists() or order.status in (WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS):
        WorkOrder.objects.filter(pk=order.pk).update(supplies_pct=pct)
        order.supplies_pct = pct


# ---------------------------------------------------------------- liberación a planta
# Insumos y logística trabajan en paralelo. Cada uno «libera su parte»:
#   insumos → supplies_released_at (a mano, o solo al entregar el 100 % si la planta tiene liberación automática)
#   logística → goods_status = mercado
# El pedido pasa a planta (Liberada) solo cuando las dos partes están listas; el supervisor depende de ambas.
def auto_release_on(site) -> bool:
    from .planning import get_rules

    rules = get_rules(site)
    return rules.auto_release if rules is not None else True


def supplies_ready(order: WorkOrder) -> bool:
    return order.supplies_released_at is not None


def goods_ready(order: WorkOrder) -> bool:
    return order.goods_status == WorkOrder.GoodsStatus.PICKED


def waiting_for(order: WorkOrder) -> list[str]:
    """Qué falta para que el pedido pase a planta: [«insumos», «logística»]."""
    return [name for name, ok in (("insumos", supplies_ready(order)), ("logística", goods_ready(order))) if not ok]


@transaction.atomic
def try_release(order: WorkOrder, user=None) -> bool:
    """Pasa el pedido a planta si está en insumos y ya liberaron insumos y logística. Devuelve True si lo liberó."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != WorkOrder.Status.SUPPLIES or waiting_for(order):
        return False
    services.release_order(order)
    _log(order.site, f"{order.number} liberado a planta: insumos y logística listos", user)
    return True


@transaction.atomic
def release_to_plant(order: WorkOrder, user=None, partial_ok: bool = False, note: str = "", auto: bool = False) -> WorkOrder:
    """Insumos libera SU parte del pedido. Con insumos incompletos exige confirmarlo y dejar una nota.
    Si logística ya lo mercó, el pedido pasa a planta en ese momento; si no, pasa cuando logística lo merque."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    if order.status in (WorkOrder.Status.RELEASED, WorkOrder.Status.IN_PROGRESS) and supplies_ready(order):
        return order  # ya se liberó (p. ej. solo, al completar la entrega en este mismo paso)
    if order.status != WorkOrder.Status.SUPPLIES:
        raise ValidationError(f"El pedido está «{order.get_status_display()}»: solo se liberan pedidos en insumos.")
    if not supplies_ready(order):
        pct, missing = completeness(order)
        note = " ".join((note or "").split())
        if missing and not partial_ok:
            detail = "; ".join(f"{r.stock.product.name}: faltan {fmt(r.qty_pending)}" for r in missing[:6])
            raise ValidationError(f"Faltan insumos por entregar ({detail}). Entrégalos o marca «Liberar con insumos "
                                  "incompletos» y explica por qué.")
        if missing and not note:
            raise ValidationError("Escribe por qué se libera con insumos incompletos.")
        order.supplies_pct = pct
        order.supplies_released_by = _user(user)
        order.supplies_released_at = timezone.now()
        order.supplies_note = (f"Liberado con insumos incompletos: {note}" if missing else "")[:255]
        order.save(update_fields=["supplies_pct", "supplies_released_by", "supplies_released_at", "supplies_note"])
        how = "automáticamente (100 % entregado)" if auto else f"({pct} % de insumos)"
        _log(order.site, f"{order.number}: insumos liberados {how}" + (f": {note}" if missing else ""), user)
    try_release(order, user)
    order.refresh_from_db()
    return order


def _auto_release(order: WorkOrder, user=None) -> None:
    """Liberación automática: al quedar el 100 % de las líneas entregado, insumos queda liberado solo."""
    order.refresh_from_db()
    if (order.status == WorkOrder.Status.SUPPLIES and not supplies_ready(order) and order.supply_lines.exists()
            and not completeness(order)[1] and auto_release_on(order.site)):
        release_to_plant(order, user, auto=True)


# ---------------------------------------------------------------- carga de inventario desde Excel / CSV
INV_ALIASES = {
    "sku": {"codigo", "código", "sku", "referencia", "ref", "item", "codigo insumo"},
    "name": {"nombre", "insumo", "descripcion", "material", "nombre insumo"},
    "unit": {"unidad", "um", "u.m", "unidad de medida"},
    "on_hand": {"existencia", "stock", "cantidad", "inventario", "saldo", "stock actual"},
    "safety": {"stock de seguridad", "stock seguridad", "minimo", "stock minimo", "seguridad"},
    "location": {"ubicacion", "bodega", "posicion"},
}
_INV = {normalize_key(a): k for k, al in INV_ALIASES.items() for a in al}
_UNITS = {"un": "UN", "und": "UN", "unidad": "UN", "unidades": "UN", "u": "UN", "kg": "KG", "kilo": "KG",
          "kilos": "KG", "kilogramo": "KG", "kilogramos": "KG", "cj": "CJ", "caja": "CJ", "cajas": "CJ"}


@dataclass
class InventoryImport:
    created: int = 0
    updated: int = 0
    adjusted: int = 0
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def import_inventory(raw: bytes, filename: str, site: Site, user=None) -> InventoryImport:
    """Crea o actualiza insumos del inventario de la planta (por código). La existencia queda como ajuste.
    Todo o nada."""
    from .staffing import _read_rows

    res = InventoryImport()
    try:
        rows = _read_rows(raw, filename)
    except ValidationError as exc:
        res.errors = exc.messages
        return res
    h, cols = None, {}
    for i, row in enumerate(rows[:10]):
        mapped = {_INV.get(normalize_key(c)): j for j, c in enumerate(row) if _INV.get(normalize_key(c))}
        if "sku" in mapped and "name" in mapped:
            h, cols = i, mapped
            break
    if h is None:
        res.errors = ["No encontré la fila de encabezados: debe tener al menos «código» y «nombre»."]
        return res

    def cell(row, key):
        j = cols.get(key)
        v = row[j] if j is not None and j < len(row) else None
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return "" if v is None else str(v).strip()

    seen = {}
    with transaction.atomic():
        for n, row in enumerate(rows[h + 1:], start=h + 2):
            if not any(str(c).strip() for c in row if c is not None):
                continue
            sku, name = cell(row, "sku").upper(), " ".join(cell(row, "name").split())
            errs = []
            if not sku:
                errs.append("falta el código")
            elif sku in seen:
                errs.append(f"código {sku} repetido (fila {seen[sku]})")
            seen.setdefault(sku, n)
            unit_raw = normalize_key(cell(row, "unit"))
            unit = _UNITS.get(unit_raw, unit_raw.upper() if unit_raw.upper() in Product.Unit.values else None)
            if unit_raw and unit is None:
                errs.append(f"unidad «{cell(row, 'unit')}» no reconocida (usa UN, KG o CJ)")
            vals = {}
            for key, label in (("on_hand", "existencia"), ("safety", "stock de seguridad")):
                if cell(row, key):
                    try:
                        vals[key] = _dec(cell(row, key), label)
                    except ValidationError as exc:
                        errs.extend(exc.messages)
            product = Product.objects.filter(sku__iexact=sku).first() if sku else None
            if product is not None and product.kind != Product.Kind.SUPPLY:
                errs.append(product.kind_error(Product.Kind.SUPPLY) + " Usa otro código para el insumo")
            if product is None and not name and sku:
                errs.append("falta el nombre (el insumo es nuevo)")
            if errs:
                res.errors.append(f"Fila {n}: " + "; ".join(errs))
                continue
            if product is None:
                product = Product.objects.create(sku=sku[:40], name=name[:200], unit=unit or Product.Unit.UNIT,
                                                 kind=Product.Kind.SUPPLY)
            elif unit and product.unit != unit:
                product.unit = unit
                product.save(update_fields=["unit"])
            stock, created = SupplyStock.objects.get_or_create(site=site, product=product)
            res.created += created
            res.updated += not created
            if "safety" in vals:
                stock.safety_stock = vals["safety"]
            if cell(row, "location"):
                stock.location = cell(row, "location")[:60]
            stock.active = True
            stock.save()
            if "on_hand" in vals and adjust(stock, vals["on_hand"], user, f"Carga de inventario ({filename})"):
                res.adjusted += 1
        if res.errors:
            transaction.set_rollback(True)
            res.created = res.updated = res.adjusted = 0
            return res
        _log(site, f"Inventario de insumos cargado: {res.created} nuevos, {res.updated} actualizados ({filename})", user)
    return res


def inventory_template(site: Site | None) -> bytes:
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Insumos"
    ws.append(["codigo", "nombre", "unidad", "existencia", "stock de seguridad", "ubicacion"])
    ws.append(["ETQ-001", "Etiqueta de precio", "UN", 12000, 2000, "Bodega A-1"])
    ws.append(["CJA-024", "Caja corrugada x24", "UN", 350, 100, "Bodega B-3"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2F3E8F")
    for col, w in zip("ABCDEF", (14, 34, 10, 14, 20, 18)):
        ws.column_dimensions[col].width = w
    info = wb.create_sheet("Instrucciones")
    for ln in ["Carga de inventario de insumos" + (f" · {site.code}" if site else ""), "",
               "• Obligatorio: código y nombre. Si el código ya existe, se actualiza.",
               "• Unidad: UN, KG o CJ (también sirve unidad, kilos, caja…).",
               "• Existencia: lo que hay hoy en bodega. Si cambia, queda un movimiento de ajuste.",
               "• Stock de seguridad: por debajo de esa cantidad el sistema avisa para reponer.",
               "• Si una fila tiene error no se carga nada. Borra las filas de ejemplo antes de cargar."]:
        info.append([ln])
    info.column_dimensions["A"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
