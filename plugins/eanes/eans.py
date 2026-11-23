"""Reglas de los EAN de un pedido: leerlos (pegados, de un archivo o escaneados), revisarlos y guardarlos.

Sin dependencias de pantallas: se puede llevar tal cual a otra aplicación.

* Un EAN se guarda siempre en su forma de 13 dígitos (GTIN-13). Un UPC de 12 dígitos queda con un 0 adelante, así el
  escáner Zebra (que a veces agrega ese 0 y a veces no) siempre coincide.
* Los códigos estándar (8, 12, 13 o 14 dígitos) deben tener bien el dígito de control: así se detecta un número mal
  digitado. Otros largos se aceptan como códigos internos, con aviso.
* Si un EAN viene repetido, se suman sus cantidades.
* Sin cantidad = sin límite (hay que escribirlo explícitamente: «sin límite»).
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from mes_core.models import ProductionReport, WorkOrder

from .models import CustomerEanFormat, EanControl, EanLine, EanLog, EanSheet

PERM = "eanes.manage_eans"
STANDARD_LENGTHS = (8, 12, 13, 14)
MIN_LEN, MAX_LEN = 6, 14
MAX_ROWS = 20000
NO_LIMIT_WORDS = ("sin limite", "sinlimite", "ilimitado", "sin tope")
EAN_HEADERS = ("ean", "codigo de barras", "cod barras", "codigo barras", "barcode", "upc", "gtin", "plu", "ean13",
               "ean 13", "codigo ean", "cod ean")
QTY_HEADERS = ("cantidad", "cant", "unidades", "und", "unds", "qty", "quantity", "pedido", "cantidad pedida",
               "unidades pedidas", "cant pedida", "total")
DESC_HEADERS = ("descripcion", "description", "producto", "nombre", "articulo", "detalle", "referencia")


# ------------------------------------------------------------------ EAN
def check_digit_ok(code: str) -> bool:
    """Dígito de control GS1 (EAN-8, UPC-A, EAN-13, GTIN-14)."""
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def _digits(raw) -> str:
    if isinstance(raw, float):
        if raw != raw or raw < 0:  # NaN
            return ""
        raw = f"{raw:.0f}"
    elif isinstance(raw, (int, Decimal)):
        raw = f"{int(raw)}"
    return re.sub(r"\D", "", str(raw or ""))


def normalize(raw) -> tuple[str, str, str]:
    """Devuelve (ean, error, aviso). El EAN sale en su forma canónica (13 dígitos si es UPC/EAN-13)."""
    text = str(raw or "").strip()
    if re.search(r"\d[.,]\d+\s*e\s*\+?\d+", text, re.I):
        return "", (f"«{text}» llegó en notación científica y perdió dígitos. En Excel pon la columna del EAN "
                    "como texto (o número sin decimales) y vuelve a copiarla."), ""
    code = _digits(raw)
    if not code:
        return "", "No tiene un EAN.", ""
    if len(code) == 14 and code.startswith("0"):
        code = code[1:]
    if len(code) == 12:
        code = "0" + code
    if len(code) < MIN_LEN or len(code) > MAX_LEN:
        return "", f"«{code}» tiene {len(code)} dígitos: un EAN tiene 8, 12, 13 o 14.", ""
    if len(code) in STANDARD_LENGTHS:
        if not check_digit_ok(code):
            return "", f"«{code}»: el último dígito (de control) no cuadra. Puede estar mal digitado.", ""
        return code, "", ""
    return code, "", f"«{code}» no es un EAN estándar ({len(code)} dígitos): se guarda como código interno."


def same_ean(a: str, b: str) -> bool:
    na, nb = normalize(a)[0] or _digits(a), normalize(b)[0] or _digits(b)
    return bool(na) and na == nb


# ------------------------------------------------------------------ cantidades
def parse_qty(raw) -> tuple[int | None, str]:
    """(cantidad, error). None = sin límite."""
    if raw is None:
        return None, "Falta la cantidad (escribe «sin límite» si no tiene tope)."
    if isinstance(raw, bool):
        return None, "Cantidad inválida."
    if isinstance(raw, (int, float, Decimal)):
        value = Decimal(str(raw))
    else:
        text = _plain(str(raw))
        if not text:
            return None, "Falta la cantidad (escribe «sin límite» si no tiene tope)."
        if any(w in text for w in NO_LIMIT_WORDS):
            return None, ""
        text = text.replace(" ", "")
        if re.fullmatch(r"\d{1,3}([.,]\d{3})+", text):  # 1.200 / 1,200 = mil doscientos
            text = re.sub(r"[.,]", "", text)
        text = text.replace(",", ".")
        try:
            value = Decimal(text)
        except Exception:  # noqa: BLE001
            return None, f"«{raw}» no es una cantidad."
    if value != value.to_integral_value():
        return None, f"«{raw}»: la cantidad debe ser un número entero de unidades."
    if value < 0:
        return None, "La cantidad no puede ser negativa."
    if value == 0:
        return None, "La cantidad es 0: quita la línea o escribe cuántas unidades son."
    if value > 10**9:
        return None, f"«{raw}» es demasiado grande."
    return int(value), ""


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(text.lower().replace("_", " ").replace(".", " ").split()) if text else ""


# ------------------------------------------------------------------ lectura
@dataclass
class Row:
    ean: str
    quantity: int | None
    description: str = ""
    line: int = 0


@dataclass
class Parsed:
    rows: list[Row] = field(default_factory=list)
    errors: list[tuple[int, str, str]] = field(default_factory=list)   # (línea, texto, problema)
    warnings: list[str] = field(default_factory=list)
    skipped: int = 0                                                   # encabezados / líneas vacías
    columns: list[str] = field(default_factory=list)                   # archivo: columnas disponibles
    mapping: dict = field(default_factory=dict)                        # archivo: columnas elegidas


def _is_code_token(tok: str) -> bool:
    d = re.sub(r"[\s-]", "", tok)
    return d.isdigit() and MIN_LEN <= len(d) <= 15


def _is_qty_token(tok: str) -> bool:
    t = _plain(tok)
    return bool(re.fullmatch(r"\d{1,9}([.,]\d+)?|\d{1,3}([.,]\d{3})+", tok.strip())) or any(w in t for w in NO_LIMIT_WORDS)


def parse_text(text: str) -> Parsed:
    """Lee líneas «EAN  cantidad  descripción» en cualquier orden, separadas por tabulador (copiado de Excel),
    punto y coma, coma o espacios. Salta encabezados y líneas vacías."""
    out = Parsed()
    for n, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        loose = False  # separado por espacios: las palabras sueltas son descripción
        if "\t" in line:
            cells = [c.strip() for c in line.split("\t")]
        elif ";" in line:
            cells = [c.strip() for c in line.split(";")]
        elif "," in line and any(_is_code_token(c.strip()) for c in line.split(",")):
            cells = [c.strip() for c in line.split(",")]
        else:
            loose = True
            unlimited = next((w for w in NO_LIMIT_WORDS if w in _plain(line)), "")
            if unlimited:  # «sin límite» son dos palabras: se toma como una sola cantidad
                line = re.sub(r"sin\s+l[ií]mite|ilimitado|sin\s+tope|sinl[ií]mite", " ", line, flags=re.I)
            cells = line.split() + (["sin limite"] if unlimited else [])
        cells = [c for c in cells if c]
        codes = [c for c in cells if _is_code_token(c)]
        if not codes:
            if re.search(r"[a-zA-Z]", line) and not any(_is_qty_token(c) for c in cells):
                out.skipped += 1  # encabezado o texto suelto
                continue
            out.errors.append((n, raw.strip()[:120], "No encontré un EAN en esta línea."))
            continue
        code = max(codes, key=lambda c: len(re.sub(r"\D", "", c)))
        rest = list(cells)
        rest.remove(code)
        qty_tokens = [c for c in rest if _is_qty_token(c)]
        if loose and len(qty_tokens) > 1:  # «7705… 12 Talla 12»: la primera es la cantidad
            qty_tokens = [next((c for c in qty_tokens if any(w in _plain(c) for w in NO_LIMIT_WORDS)), qty_tokens[0])]
        desc_tokens = list(rest)
        for c in qty_tokens:
            desc_tokens.remove(c)
        desc = " ".join(desc_tokens)
        ean, err, warn = normalize(code)
        if err:
            out.errors.append((n, raw.strip()[:120], err))
            continue
        if len(qty_tokens) > 1:
            out.errors.append((n, raw.strip()[:120], "Hay más de una cantidad en la línea: deja solo una."))
            continue
        qty, qerr = parse_qty(qty_tokens[0] if qty_tokens else None)
        if qerr:
            out.errors.append((n, raw.strip()[:120], qerr))
            continue
        if warn:
            out.warnings.append(f"Línea {n}: {warn}")
        out.rows.append(Row(ean, qty, desc[:200], n))
        if len(out.rows) > MAX_ROWS:
            raise ValidationError(f"Son demasiadas líneas (más de {MAX_ROWS}).")
    return out


def read_table(upload) -> list[list]:
    """Filas de un archivo .xlsx, .xls(x) o .csv/.txt (primera hoja con datos)."""
    name = (getattr(upload, "name", "") or "").lower()
    data = upload.read()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError("No pude abrir el archivo de Excel. ¿Está dañado o tiene contraseña?") from exc
        for ws in wb.worksheets:
            rows = []
            for r in ws.iter_rows(values_only=True):
                rows.append(list(r))
                if len(rows) > MAX_ROWS + 50:
                    break
            rows = _trim(rows)
            if rows:
                return rows
        return []
    if name.endswith((".csv", ".txt", ".tsv")):
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        sample = text[:4096]
        delim = "\t" if "\t" in sample else (";" if sample.count(";") >= sample.count(",") else ",")
        return _trim([row for row in csv.reader(io.StringIO(text), delimiter=delim)][: MAX_ROWS + 50])
    if name.endswith(".xls"):
        raise ValidationError("Los archivos .xls (Excel 97) no se pueden leer: guárdalo como .xlsx o copia y pega las columnas.")
    raise ValidationError("Sube un archivo de Excel (.xlsx) o CSV, o copia y pega las columnas.")


def _trim(rows: list[list]) -> list[list]:
    rows = [r for r in rows if any(v not in (None, "") and str(v).strip() for v in r)]
    if not rows:
        return []
    width = max(max((i + 1 for i, v in enumerate(r) if v not in (None, "") and str(v).strip()), default=0) for r in rows)
    return [list(r[:width]) + [None] * (width - len(r[:width])) for r in rows]


def _col_label(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def _matches(header: str, names) -> bool:
    h = _plain(header)
    return bool(h) and any(h == n or h.startswith(n + " ") or h.endswith(" " + n) for n in names)


def detect_layout(rows: list[list], saved: CustomerEanFormat | None = None):
    """(fila de encabezado o -1, nombres de columna, mapping {ean, qty, desc} con índices)."""
    head_idx, headers = -1, []
    for i, r in enumerate(rows[:15]):
        cells = [str(v).strip() if v is not None else "" for v in r]
        if saved and any(_plain(c) == _plain(saved.ean_column) for c in cells):
            head_idx, headers = i, cells
            break
        if any(_matches(c, EAN_HEADERS) for c in cells) and any(_matches(c, QTY_HEADERS) for c in cells):
            head_idx, headers = i, cells
            break
    width = len(rows[0]) if rows else 0
    if head_idx < 0:
        headers = [f"Columna {_col_label(i)}" for i in range(width)]
    names = [h or f"Columna {_col_label(i)}" for i, h in enumerate(headers)]
    body = rows[head_idx + 1:] if head_idx >= 0 else rows
    mapping = {}
    if saved and head_idx >= 0:
        for key, col in (("ean", saved.ean_column), ("qty", saved.qty_column), ("desc", saved.desc_column)):
            if col:
                for i, n in enumerate(names):
                    if _plain(n) == _plain(col):
                        mapping[key] = i
                        break
    if head_idx >= 0:
        for key, syn in (("ean", EAN_HEADERS), ("qty", QTY_HEADERS), ("desc", DESC_HEADERS)):
            if key in mapping:
                continue
            for word in syn:  # los sinónimos van en orden de preferencia
                hit = next((i for i, n in enumerate(names) if _matches(n, (word,)) and i not in mapping.values()), None)
                if hit is not None:
                    mapping[key] = hit
                    break
    if "ean" not in mapping:  # por contenido: la columna con más EAN válidos
        best = max(range(width), key=lambda c: sum(1 for r in body[:300] if normalize(r[c])[0] and not normalize(r[c])[2]),
                   default=None)
        if best is not None and any(normalize(r[best])[0] for r in body[:300]):
            mapping["ean"] = best
    if "qty" not in mapping and "ean" in mapping:
        def qty_score(c):
            vals = [parse_qty(r[c]) for r in body[:300] if r[c] not in (None, "")]
            return sum(1 for q, e in vals if not e and q is not None and q < 10**6)
        cands = [c for c in range(width) if c != mapping["ean"]]
        if cands:
            best = max(cands, key=qty_score)
            if qty_score(best):
                mapping["qty"] = best
    return head_idx, names, mapping


def parse_table(rows: list[list], mapping: dict | None = None, saved: CustomerEanFormat | None = None) -> Parsed:
    head_idx, names, auto = detect_layout(rows, saved)
    mapping = {k: v for k, v in (mapping or auto).items() if v is not None and 0 <= v < len(names)}
    out = Parsed(columns=names, mapping=mapping)
    if "ean" not in mapping:
        out.errors.append((0, "", "No encontré la columna del EAN: elígela abajo."))
        return out
    if "qty" not in mapping:
        out.errors.append((0, "", "No encontré la columna de la cantidad: elígela abajo."))
        return out
    start = head_idx + 1 if head_idx >= 0 else 0
    out.skipped = start
    for i, r in enumerate(rows[start:], start=start + 1):
        raw_ean, raw_qty = r[mapping["ean"]], r[mapping["qty"]]
        desc = str(r[mapping["desc"]] or "").strip() if "desc" in mapping else ""
        if raw_ean in (None, "") and raw_qty in (None, ""):
            out.skipped += 1
            continue
        if raw_ean in (None, "") or (isinstance(raw_ean, str) and not re.search(r"\d", raw_ean)):
            if any(_plain(v) in ("total", "totales", "subtotal", "total general", "gran total")
                   for v in r if isinstance(v, str)):
                out.skipped += 1  # fila de totales del archivo del cliente
                continue
            out.errors.append((i, f"{raw_ean or ''} {raw_qty or ''}".strip()[:120], "No tiene EAN."))
            continue
        ean, err, warn = normalize(raw_ean)
        if err:
            out.errors.append((i, str(raw_ean)[:120], err))
            continue
        qty, qerr = parse_qty(raw_qty)
        if qerr:
            out.errors.append((i, f"{ean} · {raw_qty}", qerr))
            continue
        if warn:
            out.warnings.append(f"Fila {i}: {warn}")
        out.rows.append(Row(ean, qty, desc[:200], i))
    return out


# ------------------------------------------------------------------ revisión
@dataclass
class Review:
    rows: list[Row]
    errors: list[tuple[int, str, str]]
    warnings: list[str]
    merged: list[tuple[str, int]]          # (ean, cuántas veces venía) — se sumaron
    total: int                             # unidades con límite
    unlimited: int                         # referencias sin límite
    order_qty: Decimal
    diff: Decimal | None                   # total − cantidad del pedido (None si hay sin límite y total 0)
    skipped: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.rows) and not self.errors

    @property
    def matches_order(self) -> bool:
        return self.diff == 0

    @property
    def diff_abs(self):
        return abs(self.diff) if self.diff is not None else None


def merge(rows: list[Row]) -> tuple[list[Row], list[tuple[str, int]]]:
    acc: dict[str, Row] = {}
    count: dict[str, int] = {}
    for r in rows:
        if r.ean in acc:
            prev = acc[r.ean]
            prev.quantity = None if prev.quantity is None or r.quantity is None else prev.quantity + r.quantity
            if not prev.description and r.description:
                prev.description = r.description
            count[r.ean] += 1
        else:
            acc[r.ean] = Row(r.ean, r.quantity, r.description, r.line)
            count[r.ean] = 1
    return list(acc.values()), [(e, n) for e, n in count.items() if n > 1]


def review(order: WorkOrder, parsed: Parsed) -> Review:
    rows, merged = merge(parsed.rows)
    total = sum(r.quantity for r in rows if r.quantity is not None)
    unlimited = sum(1 for r in rows if r.quantity is None)
    qty = Decimal(order.quantity or 0)
    diff = Decimal(total) - qty if not (unlimited and total == 0) else None
    return Review(rows, list(parsed.errors), list(parsed.warnings), merged, total, unlimited, qty, diff, parsed.skipped)


def rows_to_text(rows) -> str:
    """Formato de texto que vuelve a leer ``parse_text`` (sirve para corregir y para copiar de otro pedido)."""
    return "\n".join(f"{r.ean}\t{r.quantity if r.quantity is not None else 'sin límite'}"
                     + (f"\t{r.description}" if r.description else "") for r in rows)


def rows_to_payload(rows) -> list[list]:
    return [[r.ean, r.quantity, r.description] for r in rows]


def rows_from_payload(payload) -> list[Row]:
    out = []
    for i, item in enumerate(payload or []):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            raise ValidationError("Los datos a guardar están dañados: vuelve a revisar la carga.")
        ean, err, _ = normalize(item[0])
        if err:
            raise ValidationError(err)
        qty = item[1]
        if qty is not None and (not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0):
            raise ValidationError("Los datos a guardar están dañados: vuelve a revisar la carga.")
        out.append(Row(ean, qty, str(item[2] if len(item) > 2 and item[2] else "")[:200], i + 1))
    rows, _ = merge(out)
    return rows


# ------------------------------------------------------------------ estado y guardado
def sheet_of(order: WorkOrder) -> EanSheet:
    return EanSheet.objects.get_or_create(order=order)[0]


def status_of(order: WorkOrder) -> str:
    try:
        return order.ean_sheet.status
    except EanSheet.DoesNotExist:
        return EanSheet.Status.PENDING


def in_production(order: WorkOrder) -> bool:
    return (order.status in (WorkOrder.Status.IN_PROGRESS, WorkOrder.Status.DONE)
            or ProductionReport.objects.filter(operation__order=order).exists())


def _user(user):
    return user if user is not None and getattr(user, "is_authenticated", False) else None


def _fmt(q) -> str:
    return "sin límite" if q is None else f"{q:,}".replace(",", ".")


def _check_editable(order: WorkOrder):
    if order.status == WorkOrder.Status.CANCELLED:
        raise ValidationError(f"El pedido {order.number} está cancelado: no se le cargan EAN.")


@transaction.atomic
def apply(order: WorkOrder, rows: list[Row], *, user=None, mode: str = "replace", source: str = "", reason: str = "",
          note: str = "") -> dict:
    """Guarda los EAN del pedido. ``mode``: «replace» (la lista queda exactamente así) o «add» (suma a lo que hay).
    Si el pedido ya está en producción y la lista cambia, exige el motivo. Devuelve el resumen del cambio."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    _check_editable(order)
    if not rows:
        raise ValidationError("No hay ningún EAN para guardar.")
    rows, _ = merge(rows)
    current = {line.ean: line for line in order.ean_lines.all()}
    if mode == "add":
        final: dict[str, Row] = {e: Row(e, l.quantity, l.description) for e, l in current.items()}
        for r in rows:
            if r.ean in final:
                prev = final[r.ean]
                prev.quantity = None if prev.quantity is None or r.quantity is None else prev.quantity + r.quantity
                prev.description = prev.description or r.description
            else:
                final[r.ean] = Row(r.ean, r.quantity, r.description)
        target = list(final.values())
    else:
        target = rows
    tmap = {r.ean: r for r in target}
    added = [e for e in tmap if e not in current]
    removed = [e for e in current if e not in tmap]
    changed = [e for e in tmap if e in current and current[e].quantity != tmap[e].quantity]
    renamed = [e for e in tmap if e in current and tmap[e].description and current[e].description != tmap[e].description]
    reason = " ".join(str(reason or "").split())[:255]
    if current and (added or removed or changed) and in_production(order) and not reason:
        raise ValidationError("El pedido ya está en producción: escribe el motivo del cambio de EAN.")
    with_desc = {e: (tmap[e].description or (current[e].description if e in current else "")) for e in tmap}
    order.ean_lines.all().delete()
    EanLine.objects.bulk_create([EanLine(order=order, ean=r.ean, quantity=r.quantity, description=with_desc[r.ean],
                                         position=i) for i, r in enumerate(target)])
    sheet = sheet_of(order)
    first = sheet.status != EanSheet.Status.LOADED
    sheet.status = EanSheet.Status.LOADED
    sheet.source = source if source in EanSheet.Source.values else sheet.source or EanSheet.Source.MANUAL
    if note:
        sheet.note = " ".join(note.split())[:255]
    if first or not sheet.loaded_at:
        sheet.loaded_by, sheet.loaded_at = _user(user), timezone.now()
    sheet.save()
    total = sum(r.quantity for r in target if r.quantity is not None)
    unlimited = sum(1 for r in target if r.quantity is None)
    summary = {"first": first, "refs": len(target), "total": total, "unlimited": unlimited, "added": len(added),
               "removed": len(removed), "changed": len(changed)}
    if first:
        action = "Carga"
        detail = f"{len(target)} referencias · {_fmt(total)} unidades" + (f" · {unlimited} sin límite" if unlimited else "")
    else:
        action = "Corrección" if mode == "replace" else "Agregado"
        parts = []
        if added:
            parts.append("Agregados: " + ", ".join(f"{e} ({_fmt(tmap[e].quantity)})" for e in added[:30])
                         + (f" y {len(added) - 30} más" if len(added) > 30 else ""))
        if removed:
            parts.append("Quitados: " + ", ".join(f"{e} ({_fmt(current[e].quantity)})" for e in removed[:30])
                         + (f" y {len(removed) - 30} más" if len(removed) > 30 else ""))
        if changed:
            parts.append("Cantidad cambiada: " + ", ".join(
                f"{e} {_fmt(current[e].quantity)} → {_fmt(tmap[e].quantity)}" for e in changed[:30])
                + (f" y {len(changed) - 30} más" if len(changed) > 30 else ""))
        if renamed and not parts:
            parts.append(f"Descripción actualizada en {len(renamed)} referencias")
        detail = "\n".join(parts) or "Sin cambios en la lista"
        detail += f"\nQueda: {len(target)} referencias · {_fmt(total)} unidades"
    EanLog.objects.create(order=order, user=_user(user), action=f"{action} ({EanSheet.Source(sheet.source).label.lower()})"
                          if sheet.source else action, detail=detail, reason=reason)
    return summary


@transaction.atomic
def mark_not_apply(order: WorkOrder, *, user=None, note: str = "") -> None:
    """El pedido no lleva código de barras (apoyos, procesos internos…): planta no tendrá que escanear."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    _check_editable(order)
    note = " ".join(str(note or "").split())[:255]
    n = order.ean_lines.count()
    if n and in_production(order) and not note:
        raise ValidationError("El pedido ya está en producción con EAN cargados: escribe por qué no aplica.")
    order.ean_lines.all().delete()
    sheet = sheet_of(order)
    sheet.status, sheet.note = EanSheet.Status.NOT_APPLY, note
    sheet.loaded_by, sheet.loaded_at = _user(user), timezone.now()
    sheet.source = ""
    sheet.save()
    EanLog.objects.create(order=order, user=_user(user), action="No aplica EAN",
                          detail=(f"Se quitaron {n} referencias" if n else ""), reason=note)


@transaction.atomic
def reopen(order: WorkOrder, *, user=None, reason: str = "") -> None:
    """Vuelve a «Pendiente de EAN» un pedido marcado como «No aplica»."""
    order = WorkOrder.objects.select_for_update().get(pk=order.pk)
    _check_editable(order)
    sheet = sheet_of(order)
    if sheet.status != EanSheet.Status.NOT_APPLY:
        return
    sheet.status, sheet.note = EanSheet.Status.PENDING, ""
    sheet.loaded_by, sheet.loaded_at = None, None
    sheet.save()
    EanLog.objects.create(order=order, user=_user(user), action="Vuelve a pendiente de EAN",
                          reason=" ".join(str(reason or "").split())[:255])


def remember_format(order: WorkOrder, columns: list[str], mapping: dict) -> None:
    if not order.customer_id or "ean" not in mapping or "qty" not in mapping:
        return
    name = lambda k: columns[mapping[k]] if k in mapping and mapping[k] < len(columns) else ""  # noqa: E731
    if name("ean").startswith("Columna ") and name("qty").startswith("Columna "):
        return  # archivo sin encabezados: no hay nombres que recordar
    CustomerEanFormat.objects.update_or_create(customer_id=order.customer_id, defaults={
        "ean_column": name("ean")[:120], "qty_column": name("qty")[:120], "desc_column": name("desc")[:120]})


# ------------------------------------------------------------------ al reportar en planta
MODE_ON, MODE_PENDING, MODE_OFF = "on", "pending", "off"


def mode_of(order: WorkOrder) -> str:
    """«on»: el pedido tiene EAN y hay que reportar por referencia · «off»: no lleva EAN · «pending»: aún no se cargan."""
    st = status_of(order)
    return {EanSheet.Status.LOADED: MODE_ON, EanSheet.Status.NOT_APPLY: MODE_OFF}.get(st, MODE_PENDING)


def stage_ops_q(op):
    from mes_core.models import Operation

    if op.stage_id:
        return Operation.objects.filter(order_id=op.order_id, stage_id=op.stage_id)
    return Operation.objects.filter(pk=op.pk)


def stage_progress(op, exclude_report=None, lines=None) -> dict:
    """Avance de cada referencia del pedido en la etapa de ``op`` (sumando sus lotes / partes):
    {ean: {qty, desc, good, left, just, done, remaining, status}}. ``remaining`` es None si no tiene límite."""
    from django.db.models import F, Sum

    from mes_core.models import Shortfall

    lines = lines if lines is not None else list(EanLine.objects.filter(order_id=op.order_id))
    ops = stage_ops_q(op)
    reps = ProductionReport.objects.filter(operation__in=ops)
    if exclude_report:
        reps = reps.exclude(pk=exclude_report)
    got = {r["ean"]: r for r in reps.values("ean").annotate(g=Sum(F("qty_good") + F("qty_extra")), l=Sum("qty_leftover"))}
    just = dict(Shortfall.objects.filter(operation__in=ops).values("ean").annotate(q=Sum("qty")).values_list("ean", "q"))
    out = {}
    for line in lines:
        r = got.get(line.ean, {})
        g, lft, j = Decimal(r.get("g") or 0), Decimal(r.get("l") or 0), Decimal(just.get(line.ean) or 0)
        done = g + lft + j
        if line.quantity is None:
            remaining, status = None, "Sin límite"
        else:
            remaining = max(Decimal(0), Decimal(line.quantity) - done)
            status = "Completado" if remaining <= 0 else ("En proceso" if done > 0 else "Pendiente")
        out[line.ean] = {"ean": line.ean, "desc": line.description, "qty": line.quantity, "good": g, "left": lft,
                         "just": j, "done": done, "remaining": remaining, "status": status}
    return out


# ------------------------------------------------------------------ control del supernumerario en planta
# En cada etapa el supernumerario decide en qué orden se trabajan las referencias y cuáles están abiertas para
# reportar. Cerrar una referencia no borra nada: solo impide enviarle datos hasta que la vuelvan a abrir.
CONTROL_ACTIONS = ("abrir", "cerrar", "solo", "todas", "subir", "bajar", "primero")


def stage_key(op) -> str:
    return f"s{op.stage_id}" if op.stage_id else f"o{op.pk}"


def stage_label(op) -> str:
    return op.stage.name if op.stage_id and op.stage else op.name


def controls_of(op) -> dict[str, EanControl]:
    return {c.ean: c for c in EanControl.objects.filter(order_id=op.order_id, stage_key=stage_key(op))}


def ordered_lines(lines, ctrl: dict) -> list:
    """Las referencias en el orden que eligió el supernumerario (las que no ordenó van al final, en el del pedido)."""
    def key(ln):
        c = ctrl.get(ln.ean)
        return (0, c.position, ln.position, ln.pk) if c and c.position else (1, 0, ln.position, ln.pk)
    return sorted(lines, key=key)


def is_open(ctrl: dict, ean: str) -> bool:
    c = ctrl.get(ean)
    return c is None or c.enabled


@transaction.atomic
def set_control(op, ean: str, action: str, *, who: str = "") -> None:
    """Abrir / cerrar una referencia, dejar abierta solo esa, abrir todas, o moverla en el orden de trabajo."""
    if action not in CONTROL_ACTIONS:
        raise ValidationError("Acción no válida.")
    if mode_of(op.order) != MODE_ON:
        raise ValidationError(f"El pedido {op.order.number} no tiene referencias (EAN) cargadas.")
    lines = list(EanLine.objects.filter(order_id=op.order_id))
    code = normalize(ean)[0] if ean else ""
    if action != "todas" and code not in {ln.ean for ln in lines}:
        raise ValidationError(f"El EAN {ean} no es del pedido {op.order.number}.")
    key = stage_key(op)
    ctrl = {c.ean: c for c in EanControl.objects.select_for_update().filter(order_id=op.order_id, stage_key=key)}
    order = [ln.ean for ln in ordered_lines(lines, ctrl)]
    enabled = {e: is_open(ctrl, e) for e in order}
    if action == "abrir":
        enabled[code] = True
    elif action == "cerrar":
        enabled[code] = False
    elif action == "solo":
        enabled = {e: e == code for e in order}
    elif action == "todas":
        enabled = {e: True for e in order}
    else:
        i = order.index(code)
        j = {"subir": max(0, i - 1), "bajar": min(len(order) - 1, i + 1), "primero": 0}[action]
        order.insert(j, order.pop(i))
    reordered = action in ("subir", "bajar", "primero")
    for pos, e in enumerate(order, start=1):
        c = ctrl.get(e)
        if c is None:
            if enabled[e] and not reordered:
                continue  # abierta y sin orden propio: no hace falta guardarla
            c = EanControl(order_id=op.order_id, stage_key=key, ean=e, position=0)
        if reordered or c.position:
            c.position = pos
        c.enabled = enabled[e]
        c.changed_by = who[:120]
        c.save()
    text = {"abrir": f"abrió la referencia {code}", "cerrar": f"cerró la referencia {code}",
            "solo": f"dejó abierta solo la referencia {code}", "todas": "abrió todas las referencias",
            "subir": f"subió la referencia {code} en el orden", "bajar": f"bajó la referencia {code} en el orden",
            "primero": f"puso de primera la referencia {code}"}[action]
    EanLog.objects.create(order_id=op.order_id, action="Planta: " + text.split(" ")[0],
                          detail=f"{who or 'Planta'} {text} en «{stage_label(op)}».")


def _n(v) -> str:
    v = Decimal(v)
    return f"{v.normalize():f}" if v == v.to_integral_value() else f"{v:g}"


def check_report(op, *, ean: str, qty_good, qty_leftover, exclude_report=None) -> dict:
    """Reglas de la app anterior al reportar: el EAN debe ser del pedido y no se puede pasar de lo que falta de esa
    referencia en la etapa. Pedido «No aplica»: sin EAN. Pedido sin EAN cargados: se permite (o se bloquea si la planta
    configuró MES_EAN_EXIGIR=1)."""
    from django.conf import settings

    order = op.order
    mode = mode_of(order)
    if mode == MODE_OFF:
        return {"ean": ""}
    code, err = "", ""
    if ean:
        code, err, _ = normalize(ean)
        if err:
            raise ValidationError(err)
    if mode == MODE_PENDING:
        if getattr(settings, "MES_EAN_EXIGIR", False) and (qty_good > 0 or qty_leftover > 0):
            raise ValidationError(f"El pedido {order.number} aún no tiene sus EAN cargados (Validación EAN): "
                                  "no se puede reportar hasta que los carguen.")
        return {"ean": code}
    lines = list(EanLine.objects.filter(order_id=order.pk))
    if not code:
        if qty_good <= 0 and qty_leftover <= 0:
            return {"ean": ""}  # solo merma: no hace falta la referencia
        raise ValidationError(f"Elige o escanea la referencia (EAN) que trabajaste: el pedido {order.number} tiene "
                              f"{len(lines)} referencia{'s' if len(lines) != 1 else ''}.")
    prog = stage_progress(op, exclude_report, lines)
    if code not in prog:
        raise ValidationError(f"El EAN {code} no es del pedido {order.number}. Revisa la referencia.")
    p = prog[code]
    total = Decimal(qty_good) + Decimal(qty_leftover)
    if exclude_report is None and total > 0 and not is_open(controls_of(op), code):
        desc = f" ({p['desc']})" if p["desc"] else ""
        raise ValidationError(f"La referencia {code}{desc} está cerrada en «{stage_label(op)}»: el supernumerario "
                              "debe abrirla para enviarle datos.")
    if p["remaining"] is not None and total > p["remaining"]:
        desc = f" ({p['desc']})" if p["desc"] else ""
        raise ValidationError(f"De la referencia {code}{desc} faltan {_n(p['remaining'])} de {_n(p['qty'])} en "
                              f"«{op.stage.name if op.stage_id else op.name}» y estás reportando {_n(total)}.")
    return {"ean": code}


def order_progress(order) -> dict:
    """Avance de cada referencia en cada etapa del pedido: {stages: [nombre…], rows: [{ean, desc, qty, cells}]}."""
    lines = list(EanLine.objects.filter(order=order))
    stages, seen = [], set()
    for op in order.operations.select_related("stage").order_by("sequence"):
        key = op.stage_id or f"op{op.pk}"
        if key in seen:
            continue
        seen.add(key)
        stages.append((op.stage.name if op.stage_id else op.name, stage_progress(op, lines=lines)))
    rows = [{"ean": ln.ean, "desc": ln.description, "qty": ln.quantity,
             "cells": [prog[ln.ean] for _n, prog in stages]} for ln in lines]
    return {"stages": [s[0] for s in stages], "rows": rows}


def report_info(ops) -> dict:
    """Para las tablets y el formulario del MES: las referencias de cada operación con lo que falta de cada una."""
    out, cache, by_order = {}, {}, {}
    for op in ops:
        o = op.order
        if o.pk not in by_order:
            mode = mode_of(o)
            by_order[o.pk] = (mode, list(EanLine.objects.filter(order_id=o.pk)) if mode == MODE_ON else [])
        mode, lines = by_order[o.pk]
        if mode != MODE_ON:
            out[op.pk] = {"ref": {"mode": mode, "items": []}}
            continue
        key = (o.pk, op.stage_id or f"op{op.pk}")
        if key not in cache:
            prog = stage_progress(op, lines=lines)
            ctrl = controls_of(op)
            cache[key] = [{"ean": ln.ean, "desc": p["desc"], "qty": None if p["qty"] is None else float(p["qty"]),
                           "done": float(p["done"]), "left": None if p["remaining"] is None else float(p["remaining"]),
                           "status": p["status"], "on": is_open(ctrl, ln.ean), "pos": i}
                          for i, ln in enumerate(ordered_lines(lines, ctrl), start=1) for p in [prog[ln.ean]]]
        out[op.pk] = {"ref": {"mode": mode, "items": cache[key]}}
    return out


def shortfall_lines(op, pending) -> list:
    """Al cerrar con faltante: lo que falta de la operación repartido por referencia (lo que le falta a cada una en la
    etapa, sin pasar de lo que le falta a la operación)."""
    if mode_of(op.order) != MODE_ON:
        return []
    left = Decimal(pending)
    out = []
    for p in stage_progress(op).values():
        if left <= 0:
            break
        if p["remaining"] is None or p["remaining"] <= 0:
            continue
        q = min(p["remaining"], left)
        out.append((p["ean"], q))
        left -= q
    if left > 0:
        out.append(("", left))
    return out


def copy_sources(order: WorkOrder, limit: int = 40):
    """Pedidos con EAN cargados para copiar: primero los del mismo cliente y producto, luego los más recientes."""
    from django.db.models import Case, Count, IntegerField, Value, When

    qs = (WorkOrder.objects.exclude(pk=order.pk).filter(ean_sheet__status=EanSheet.Status.LOADED)
          .select_related("customer", "product").annotate(n_eans=Count("ean_lines")))
    rank = []
    if order.customer_id:
        rank.append(When(customer_id=order.customer_id, product_id=order.product_id, then=Value(0)))
        rank.append(When(customer_id=order.customer_id, then=Value(1)))
    rank.append(When(product_id=order.product_id, then=Value(2)))
    return list(qs.annotate(rank=Case(*rank, default=Value(3), output_field=IntegerField()))
                .order_by("rank", "-ean_sheet__loaded_at")[:limit])
