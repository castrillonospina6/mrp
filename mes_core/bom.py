"""Lista de materiales por producto: de ahí salen solas las líneas de insumos de cada pedido."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .models import BomLine, Operation, Product, ScheduleChange, Site, WorkOrder
from .normalize import normalize_key

ZERO = Decimal("0")


@dataclass
class Need:
    component: Product
    qty: Decimal
    operation: Operation | None
    note: str


def _round(qty: Decimal, product: Product) -> Decimal:
    """Unidades y cajas se piden enteras (hacia arriba); kilos con 3 decimales."""
    if product.unit == Product.Unit.KG:
        return qty.quantize(Decimal("0.001"), rounding="ROUND_CEILING")
    return Decimal(math.ceil(qty))


def _stage_op(order: WorkOrder, stage: str, ops) -> Operation | None:
    if not stage:
        return None
    for op in ops:
        if stage in (normalize_key(op.work_center_kind), normalize_key(op.name)):
            return op
    for op in ops:  # «etiquetado» también encaja con «etiquetado de precios»
        if stage in normalize_key(op.name) or (op.work_center_kind and stage in normalize_key(op.work_center_kind)):
            return op
    return None


def requirements(order: WorkOrder) -> list[Need]:
    """Insumos del pedido según la lista de materiales del producto (vacío si el producto no tiene)."""
    lines = list(BomLine.objects.filter(product=order.product).select_related("component"))
    if not lines:
        return []
    ops = list(order.operations.order_by("sequence"))
    out = []
    for ln in lines:
        qty = order.quantity * ln.qty_per_unit * (1 + ln.scrap_pct / 100)
        if qty <= 0:
            continue
        note = f"Lista de materiales: {ln.qty_per_unit.normalize():f} por unidad"
        if ln.scrap_pct:
            note += f" + {ln.scrap_pct.normalize():f} % desperdicio"
        out.append(Need(ln.component, _round(qty, ln.component), _stage_op(order, ln.stage, ops), note))
    return out


# ---------------------------------------------------------------- edición
def _dec(value, label) -> Decimal:
    raw = str(value if value is not None else "").strip().replace(",", ".")
    try:
        d = Decimal(raw) if raw else ZERO
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{label}: «{value}» no es un número.") from None
    if d < 0:
        raise ValidationError(f"{label} no puede ser negativo.")
    return d


@dataclass
class LineInput:
    pk: int | None
    component: str  # SKU (o «SKU · nombre»)
    qty_per_unit: object
    scrap_pct: object = ""
    stage: str = ""
    note: str = ""
    delete: bool = False


def find_product(text: str, kind: str | None = None) -> Product | None:
    """Artículo por código (acepta «SKU · nombre»). Con `kind`, solo de ese tipo (producto o insumo)."""
    sku = str(text or "").split(" · ")[0].strip()
    qs = Product.objects.filter(sku__iexact=sku)
    if kind:
        qs = qs.filter(kind=kind)
    return qs.first() if sku else None


def _wrong_kind(text: str, kind: str) -> str:
    other = find_product(text)
    return other.kind_error(kind) if other is not None else ""


@transaction.atomic
def save_lines(product: Product, lines: list[LineInput], user=None) -> int:
    """Guarda la lista de materiales de un producto. Todo o nada. Devuelve cuántas líneas quedan."""
    if product.kind != Product.Kind.PRODUCT:
        raise ValidationError(product.kind_error(Product.Kind.PRODUCT) + " Solo los productos llevan lista de materiales.")
    existing = {ln.pk: ln for ln in product.bom_lines.all()}
    errors, keep = [], []
    for i, li in enumerate(lines, start=1):
        row = existing.get(li.pk) if li.pk else None
        if li.pk and row is None:
            errors.append("Una línea ya no existe; recarga la página.")
            continue
        if li.delete:
            if row:
                row.delete()
            continue
        comp = find_product(li.component, Product.Kind.SUPPLY)
        if comp is None:
            wrong = _wrong_kind(li.component, Product.Kind.SUPPLY) if li.component else ""
            errors.append(f"Línea {i}: {wrong}" if wrong else
                          f"Línea {i}: no encontré el insumo «{li.component}» (escribe su código; créalo en Inventario)."
                          if li.component else f"Línea {i}: elige el insumo.")
            continue
        if comp.pk == product.pk:
            errors.append(f"Línea {i}: un producto no puede ser insumo de sí mismo.")
            continue
        try:
            qty = _dec(li.qty_per_unit, f"{comp.sku}: cantidad por unidad")
            scrap = _dec(li.scrap_pct, f"{comp.sku}: desperdicio")
        except ValidationError as exc:
            errors.extend(exc.messages)
            continue
        if qty <= 0:
            errors.append(f"{comp.sku}: la cantidad por unidad debe ser mayor que cero.")
            continue
        if scrap > 100:
            errors.append(f"{comp.sku}: el desperdicio no puede pasar de 100 %.")
            continue
        stage = normalize_key(li.stage)
        if (comp.pk, stage) in keep:
            errors.append(f"{comp.sku}: está repetido{f' en la etapa {stage}' if stage else ''}.")
            continue
        keep.append((comp.pk, stage))
        row = row or BomLine(product=product)
        row.component, row.qty_per_unit, row.scrap_pct, row.stage = comp, qty, scrap, stage
        row.note, row.updated_by = (li.note or "").strip()[:200], user if user and user.is_authenticated else None
        try:
            with transaction.atomic():
                row.save()
        except IntegrityError:  # choque con otra línea ya guardada (mismo insumo y etapa)
            errors.append(f"{comp.sku}: está repetido.")
    if errors:
        raise ValidationError(errors)
    return product.bom_lines.count()


# ---------------------------------------------------------------- Excel
ALIASES = {
    "product": {"producto", "sku producto", "codigo producto", "referencia producto", "sku"},
    "component": {"insumo", "codigo insumo", "sku insumo", "material", "componente"},
    "qty": {"cantidad por unidad", "cantidad", "consumo", "consumo por unidad", "por unidad"},
    "scrap": {"desperdicio", "merma", "desperdicio %", "merma %"},
    "stage": {"etapa", "proceso", "linea", "operacion"},
    "note": {"nota", "observaciones", "observacion"},
}
_ALIAS = {normalize_key(a): k for k, al in ALIASES.items() for a in al}


@dataclass
class BomImport:
    products: int = 0
    lines: int = 0
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def import_bom(raw: bytes, filename: str, site: Site | None = None, user=None) -> BomImport:
    """Carga listas de materiales. Cada producto del archivo queda EXACTAMENTE con las líneas del archivo. Todo o nada."""
    from .staffing import _read_rows

    res = BomImport()
    try:
        rows = _read_rows(raw, filename)
    except ValidationError as exc:
        res.errors = exc.messages
        return res
    head, cols = None, {}
    for i, row in enumerate(rows[:10]):
        mapped = {}
        for j, c in enumerate(row):
            key = _ALIAS.get(normalize_key(c))
            if key and key not in mapped:
                mapped[key] = j
        if {"product", "component", "qty"} <= mapped.keys():
            head, cols = i, mapped
            break
    if head is None:
        res.errors = ["No encontré los encabezados: debe tener «producto», «insumo» y «cantidad por unidad»."]
        return res

    def cell(row, key):
        j = cols.get(key)
        v = row[j] if j is not None and j < len(row) else None
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return "" if v is None else str(v).strip()

    by_product: dict[int, tuple[Product, list[LineInput]]] = {}
    for n, row in enumerate(rows[head + 1:], start=head + 2):
        if not any(str(c).strip() for c in row if c is not None):
            continue
        prod = find_product(cell(row, "product"), Product.Kind.PRODUCT)
        if prod is None:
            wrong = _wrong_kind(cell(row, "product"), Product.Kind.PRODUCT)
            res.errors.append(f"Fila {n}: {wrong}" if wrong else f"Fila {n}: no existe el producto «{cell(row, 'product')}».")
            continue
        by_product.setdefault(prod.pk, (prod, []))[1].append(
            LineInput(None, cell(row, "component"), cell(row, "qty"), cell(row, "scrap"), cell(row, "stage"),
                      cell(row, "note")))
    with transaction.atomic():
        for prod, lines in by_product.values():
            prod.bom_lines.all().delete()
            try:
                res.lines += save_lines(prod, lines, user)
                res.products += 1
            except ValidationError as exc:
                res.errors.extend(f"{prod.sku}: {m}" for m in exc.messages)
        if res.errors:
            transaction.set_rollback(True)
            res.products = res.lines = 0
            return res
        if site is not None and res.products:
            ScheduleChange.objects.create(site=site, action=ScheduleChange.Action.SUPPLIES,
                                          description=f"Listas de materiales cargadas: {res.products} productos ({filename})"[:255],
                                          user=user if user and user.is_authenticated else None)
    return res


def template() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Lista de materiales"
    ws.append(["producto", "insumo", "cantidad por unidad", "desperdicio %", "etapa", "nota"])
    ws.append(["CAM-001", "ETQ-001", 1, 2, "etiquetado", "una etiqueta por prenda"])
    ws.append(["CAM-001", "BLS-010", 1, 0, "", ""])
    ws.append(["CAM-001", "CJA-024", 0.0417, 0, "empaque", "1 caja cada 24 unidades"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2F3E8F")
    for col, w in zip("ABCDEF", (16, 16, 20, 16, 16, 30)):
        ws.column_dimensions[col].width = w
    info = wb.create_sheet("Instrucciones")
    for ln in ["Lista de materiales: cuánto insumo lleva CADA UNIDAD del producto.", "",
               "• producto e insumo: el código (SKU). Los dos deben existir en el sistema.",
               "• cantidad por unidad: p. ej. 1 etiqueta por prenda; 0,0417 cajas si una caja lleva 24.",
               "• desperdicio %: se suma a lo requerido. Unidades y cajas se redondean hacia arriba.",
               "• etapa (opcional): la etapa o tipo de línea que lo consume (etiquetado, empaque…).",
               "• Cada producto del archivo queda solo con las líneas del archivo (reemplaza su lista).",
               "• Si una fila tiene error no se carga nada. Borra las filas de ejemplo antes de cargar."]:
        info.append([ln])
    info.column_dimensions["A"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
