"""Carga masiva de pedidos de ventas desde Excel (o CSV).

Cada fila es un pedido que queda «Por planear», igual que «Nuevo pedido». El sistema reconoce solo:
  * cliente: por nombre, sin importar mayúsculas, tildes ni espacios (si no existe, lo crea);
  * producto: por código (SKU) o por nombre (si no existe, lo crea, como en «Nuevo pedido»);
  * tipo de servicio: por clave o nombre (si no existe, lo agrega a la lista); vacío = sin tipo.
Todo o nada: si alguna fila tiene error no se guarda ningún pedido y se lista qué corregir."""

from __future__ import annotations

import contextvars
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from . import service_types, services
from .models import Customer, ScheduleChange, Site, WorkOrder
from .normalize import normalize_key

MAX_ROWS = 2000
# Mientras se importa en bloque, los avisos «pedido nuevo» van en uno solo al final (no uno por pedido).
bulk_import = contextvars.ContextVar("mes_bulk_order_import", default=False)

ALIASES = {
    "site": {"planta", "sede", "bodega", "cedi"},
    "customer": {"cliente", "nombre cliente", "razon social"},
    "product": {"producto", "sku", "referencia producto", "codigo producto", "codigo", "articulo", "item"},
    "service": {"tipo de servicio", "servicio", "proceso", "tipo"},
    "quantity": {"cantidad", "unidades", "cant", "cantidad pedida"},
    "delivery": {"fecha de entrega", "entrega", "fecha entrega", "fecha de entrega en el cliente"},
    "transit": {"dias de transito", "transito", "dias transito"},
    "received": {"fecha de recepcion", "recepcion", "fecha recepcion", "fecha del pedido"},
    "reference": {"referencia", "ref", "pedido cliente", "numero de pedido"},
    "po": {"oc", "orden de compra", "o c"},
    "op": {"op", "numero de op", "orden de produccion"},
    "shipment": {"embarque", "despacho"},
    "priority": {"prioridad"},
    "description": {"descripcion"},
    "notes": {"observaciones", "notas", "nota", "observacion"},
}
_ALIAS = {normalize_key(a): k for k, al in ALIASES.items() for a in al}


@dataclass
class OrderImport:
    created: list = field(default_factory=list)
    new_customers: list = field(default_factory=list)
    new_products: list = field(default_factory=list)
    new_services: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _date(value, label) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw.split(" ")[0], fmt).date()
        except ValueError:
            continue
    raise ValidationError(f"{label} «{raw}» no es una fecha (usa dd/mm/aaaa)")


def _num(value, label) -> Decimal:
    raw = str(value if value is not None else "").strip().replace(" ", "")
    if "," in raw and "." in raw:  # 1.200,50 → 1200.50
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):  # 1.200 o 12.500.000 → miles con punto (formato colombiano)
        raw = raw.replace(".", "")
    else:
        raw = raw.replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ValidationError(f"{label} «{value}» no es un número") from None


def find_customer(name: str, cache: dict) -> tuple[Customer | None, bool]:
    key = normalize_key(name)
    if not key:
        return None, False
    if not cache:
        for c in Customer.objects.all():
            cache.setdefault(normalize_key(c.name), c)
    if key in cache:
        return cache[key], False
    cust = Customer.objects.create(name=" ".join(str(name).split())[:120])
    cache[key] = cust
    return cust, True


def import_orders(raw: bytes, filename: str, default_site: Site, user=None) -> OrderImport:
    from .staffing import _read_rows

    res = OrderImport()
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
        if {"product", "quantity"} <= mapped.keys():
            head, cols = i, mapped
            break
    if head is None:
        res.errors = ["No encontré los encabezados: debe tener al menos «producto» y «cantidad» (y «fecha de entrega»)."]
        return res
    if "delivery" not in cols:
        res.errors = ["Falta la columna «fecha de entrega»: planeación la necesita para calcular el despacho."]
        return res
    body = [r for r in rows[head + 1:] if any(str(c).strip() for c in r if c is not None)]
    if len(body) > MAX_ROWS:
        res.errors = [f"El archivo tiene {len(body)} pedidos; el máximo por carga es {MAX_ROWS}."]
        return res

    def cell(row, key):
        j = cols.get(key)
        v = row[j] if j is not None and j < len(row) else None
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return v

    def text(row, key, n=60):
        v = cell(row, key)
        return " ".join(str(v).split())[:n] if v not in (None, "") else ""

    sites = {normalize_key(s.code): s for s in Site.objects.filter(active=True)}
    sites.update({normalize_key(s.name): s for s in Site.objects.filter(active=True)})
    customers: dict = {}
    seen_ops = {}
    token = bulk_import.set(True)
    try:
        with transaction.atomic():
            for n, row in enumerate(rows[head + 1:], start=head + 2):
                if not any(str(c).strip() for c in row if c is not None):
                    continue
                errs = []
                site = default_site
                if text(row, "site"):
                    site = sites.get(normalize_key(text(row, "site")))
                    if site is None:
                        errs.append(f"planta «{text(row, 'site')}» no existe")
                prod_txt = text(row, "product", 200)
                if not prod_txt:
                    errs.append("falta el producto")
                qty = None
                try:
                    qty = _num(cell(row, "quantity"), "cantidad")
                    if qty <= 0:
                        errs.append("la cantidad debe ser mayor que cero")
                except ValidationError as exc:
                    errs.extend(exc.messages)
                delivery = received = None
                try:
                    delivery = _date(cell(row, "delivery"), "fecha de entrega")
                    if delivery is None:
                        errs.append("falta la fecha de entrega")
                    received = _date(cell(row, "received"), "fecha de recepción")
                except ValidationError as exc:
                    errs.extend(exc.messages)
                transit, priority = 0, 3
                try:
                    if cell(row, "transit") not in (None, ""):
                        transit = int(_num(cell(row, "transit"), "días de tránsito"))
                    if cell(row, "priority") not in (None, ""):
                        priority = int(_num(cell(row, "priority"), "prioridad"))
                        if not 1 <= priority <= 5:
                            errs.append("la prioridad va de 1 a 5")
                except ValidationError as exc:
                    errs.extend(exc.messages)
                op = text(row, "op")
                if op:
                    if op in seen_ops:
                        errs.append(f"la OP {op} está repetida (fila {seen_ops[op]})")
                    elif WorkOrder.objects.filter(external_op=op).exclude(status=WorkOrder.Status.CANCELLED).exists():
                        errs.append(f"ya existe un pedido con la OP {op} (¿ya se cargó?)")
                    seen_ops.setdefault(op, n)
                if errs:
                    res.errors.append(f"Fila {n}: " + "; ".join(errs))
                    continue
                try:
                    with transaction.atomic():
                        customer, c_new = find_customer(text(row, "customer", 120), customers)
                        product, p_new = services.resolve_product(prod_txt)
                        service, s_new = service_types.resolve(text(row, "service", 80))
                        order = services.receive_order(
                            site=site, product=product, process_type=service.key if service else "", quantity=qty,
                            received_on=received, delivery_date=delivery, transit_days=transit, customer=customer,
                            reference=text(row, "reference"), purchase_order=text(row, "po"), external_op=op,
                            shipment=text(row, "shipment"), description=text(row, "description", 2000),
                            priority=priority, notes=text(row, "notes", 2000), user=user)
                except ValidationError as exc:
                    res.errors.append(f"Fila {n}: " + "; ".join(exc.messages))
                    continue
                res.created.append(order)
                if c_new:
                    res.new_customers.append(customer.name)
                if p_new:
                    res.new_products.append(f"{product.sku} ({product.name})")
                if s_new:
                    res.new_services.append(service.name)
            if res.errors:
                transaction.set_rollback(True)
                res.created, res.new_customers, res.new_products, res.new_services = [], [], [], []
                return res
            for site in {o.site for o in res.created}:
                n_site = sum(1 for o in res.created if o.site_id == site.pk)
                ScheduleChange.objects.create(site=site, action=ScheduleChange.Action.IMPORT,
                                              description=f"{n_site} pedido(s) cargados desde Excel ({filename})"[:255],
                                              user=user if user and user.is_authenticated else None)
            if res.created:
                from . import notify

                orders = list(res.created)
                transaction.on_commit(lambda: notify._safe(notify.orders_imported, orders, user))
    finally:
        bulk_import.reset(token)
    return res


def template(site: Site | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "Pedidos"
    ws.append(["cliente", "producto", "tipo de servicio", "cantidad", "fecha de entrega", "dias de transito",
               "referencia", "oc", "op", "embarque", "prioridad", "observaciones"])
    ws.append(["Almacenes Éxito", "CAM-001", "Acondicionado", 1200, "30/11/2026", 1, "PED-5521", "OC-998", "", "", 3, ""])
    ws.append(["Falabella", "Jean clásico azul", "", 600, "05/12/2026", 0, "", "", "", "", 2, "urgente"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2F3E8F")
    for col, w in zip("ABCDEFGHIJKL", (22, 22, 20, 10, 16, 14, 14, 12, 12, 12, 10, 30)):
        ws.column_dimensions[col].width = w
    types = [t.name for t in service_types.active_types()][:40]
    if types:
        lists = wb.create_sheet("Listas")
        for i, name in enumerate(types, start=1):
            lists.cell(row=i, column=1, value=name)
        dv = DataValidation(type="list", formula1=f"=Listas!$A$1:$A${len(types)}", allow_blank=True, showErrorMessage=False)
        ws.add_data_validation(dv)
        dv.add("C2:C2001")
    info = wb.create_sheet("Instrucciones")
    for ln in ["Carga de pedidos" + (f" · planta {site.code}" if site else ""), "",
               "• Una fila por pedido. Obligatorio: producto, cantidad y fecha de entrega (dd/mm/aaaa).",
               "• cliente: se reconoce por nombre aunque cambien mayúsculas o tildes; si no existe se crea.",
               "• producto: código (SKU) o nombre; si no existe se crea (unidad UN, ajustable en Administración).",
               "• tipo de servicio: de la lista o escrito a mano; vacío = sin tipo (lo define planeación).",
               "• op: si ya existe un pedido con esa OP, la fila se rechaza (evita cargar dos veces).",
               "• Opcional: planta (código), fecha de recepción, oc, embarque, prioridad 1-5, descripción, observaciones.",
               "• Si una fila tiene error no se carga nada. Borra las filas de ejemplo antes de cargar."]:
        info.append([ln])
    info.column_dimensions["A"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
