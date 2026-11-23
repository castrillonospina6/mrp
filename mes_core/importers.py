"""Importación de órdenes desde CSV (separador ``,`` o ``;``, UTF-8).

Columnas (encabezado obligatorio; las marcadas con * son requeridas):
  planta*      código de la planta (p. ej. CEDI-01)
  sku*         SKU del producto (debe existir)
  proceso*     clave del tipo de proceso (picking, acondicionado, deshuese…)
  cantidad*    número (acepta coma decimal)
  compromiso   AAAA-MM-DD o AAAA-MM-DD HH:MM
  prioridad    1 a 5 (defecto 3)
  referencia   texto libre
  cliente      nombre; se crea si no existe
  oc, op, embarque, descripcion   textos libres opcionales
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from . import services
from .models import Customer, Product, ScheduleChange, Site

REQUIRED = ("planta", "sku", "proceso", "cantidad")


def _parse_due(raw: str):
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            dt = dt.replace(hour=23, minute=59)
        return timezone.make_aware(dt)
    raise ValidationError(f"fecha inválida «{raw}»")


def import_orders_csv(raw: bytes, user=None) -> tuple[list[str], list[str]]:
    """Devuelve (números creados, errores por fila). Si hay errores no se crea nada."""
    text = raw.decode("utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0] if text else ",", delimiters=",;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = {h.strip().lower() for h in (reader.fieldnames or [])}
    missing = [c for c in REQUIRED if c not in headers]
    if missing:
        return [], [f"Faltan columnas: {', '.join(missing)}"]

    created, errors = [], []
    sites_touched = set()
    with transaction.atomic():
        for n, row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not any(row.values()):
                continue
            try:
                site = Site.objects.filter(code__iexact=row["planta"]).first()
                if not site:
                    raise ValidationError(f"planta «{row['planta']}» no existe")
                product = Product.objects.filter(sku__iexact=row["sku"]).first()
                if not product:
                    raise ValidationError(f"SKU «{row['sku']}» no existe")
                try:
                    qty = Decimal(row["cantidad"].replace(",", "."))
                except InvalidOperation:
                    raise ValidationError(f"cantidad inválida «{row['cantidad']}»") from None
                customer = None
                if row.get("cliente"):
                    customer, _ = Customer.objects.get_or_create(name=row["cliente"])
                order = services.create_order(
                    site=site, product=product, process_type=row["proceso"].lower(), quantity=qty,
                    priority=int(row.get("prioridad") or 3), due_date=_parse_due(row.get("compromiso", "")),
                    reference=row.get("referencia", "")[:60], customer=customer,
                    purchase_order=row.get("oc", "")[:60], external_op=row.get("op", "")[:60],
                    shipment=row.get("embarque", "")[:60], description=row.get("descripcion", ""),
                )
                created.append(order.number)
                sites_touched.add(site)
            except (ValidationError, ValueError) as exc:
                msg = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
                errors.append(f"Fila {n}: {msg}")
        if errors:
            transaction.set_rollback(True)
            return [], errors
        for site in sites_touched:
            ScheduleChange.objects.create(
                site=site, action=ScheduleChange.Action.IMPORT,
                description=f"Importadas {len(created)} órdenes desde CSV",
                user=user if user and user.is_authenticated else None,
            )
    return created, errors
