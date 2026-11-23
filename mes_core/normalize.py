"""Normalización de textos que se escriben a mano o llegan desde Excel.

Reglas (se aplican solas al guardar operarios y competencias):
  * minúsculas,
  * sin tildes ni diéresis (á→a, Ü→u); la ñ se conserva porque es otra letra (diseño ≠ diseno),
  * espacios repetidos, tabulaciones y espacios raros (no separables) → un solo espacio, sin bordes,
  * comillas tipográficas → comillas simples.

    >>> normalize_text("  JOSÉ   Pérez Muñoz ")
    'jose perez muñoz'
    >>> normalize_key("Control_de  CALIDAD.")
    'control de calidad'
    >>> normalize_document("1.020.345.678")
    '1020345678'
    >>> parse_level("EXPERTO"), parse_level("medio"), parse_level(1)
    (3, 2, 1)
"""

from __future__ import annotations

import re
import unicodedata

_COMBINING_TILDE = "̃"
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "´": "'", "`": "'"})
_SPACES = re.compile(r"[ \t   ​]+")
_LOWER_PARTICLES = {"de", "del", "la", "las", "los", "y", "e", "da", "do", "dos", "van", "von"}


def strip_accents(value: str) -> str:
    """Quita tildes y diéresis, pero conserva la ñ."""
    out = []
    for ch in unicodedata.normalize("NFD", value):
        if unicodedata.category(ch) == "Mn":
            if ch == _COMBINING_TILDE and out and out[-1] in "nN":
                out.append(ch)
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def _cell_to_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():  # Excel guarda 1020345678 como 1020345678.0
        value = int(value)
    return str(value)


def normalize_text(value, keep_newlines: bool = False) -> str:
    """Texto libre: minúsculas, sin tildes y con espacios limpios."""
    s = strip_accents(_cell_to_str(value)).translate(_QUOTES).lower()
    s = "".join(ch for ch in s if ch in "\n\t" or unicodedata.category(ch)[0] != "C")  # sin caracteres de control
    if keep_newlines:
        lines = (_SPACES.sub(" ", ln).strip() for ln in s.splitlines())
        return "\n".join(ln for ln in lines if ln)
    return _SPACES.sub(" ", s.replace("\n", " ")).strip()


def normalize_key(value) -> str:
    """Nombre de competencia, turno o encabezado: como normalize_text y además sin guiones bajos ni puntuación
    en los bordes ("Control_de  CALIDAD." → "control de calidad")."""
    s = normalize_text(_cell_to_str(value).replace("_", " "))
    return s.strip(" .,;:-*#")


def normalize_document(value) -> str:
    """Documento de identidad: solo letras y números, en mayúscula (1.020.345.678 → 1020345678)."""
    s = strip_accents(_cell_to_str(value)).upper()
    return "".join(ch for ch in s if ch.isalnum())


def display_name(value: str) -> str:
    """Nombre normalizado para mostrar: «jose de la cruz» → «Jose de la Cruz»."""
    words = (value or "").split(" ")
    return " ".join(w if (i and w in _LOWER_PARTICLES) else w[:1].upper() + w[1:] for i, w in enumerate(words))


# Nivel de la matriz de polivalencia
_LEVEL_WORDS = {
    1: {"1", "n", "nov", "novato", "novata", "principiante", "basico", "basica", "bajo", "baja", "aprendiz", "junior",
        "en formacion", "en entrenamiento"},
    2: {"2", "i", "int", "intermedio", "intermedia", "medio", "media", "regular", "competente"},
    3: {"3", "e", "exp", "experto", "experta", "avanzado", "avanzada", "alto", "alta", "senior", "maestro", "maestra",
        "lider", "instructor", "instructora"},
}
_NO_LEVEL = {"0", "-", "no", "ninguno", "ninguna", "na", "n/a", "sin nivel", "no aplica"}


class LevelError(ValueError):
    pass


def parse_level(value) -> int | None:
    """Convierte lo que escriban (EXPERTO, avanzado, 3, «Intermedio ») en 1, 2 o 3.

    Devuelve None si la celda indica «sin nivel» (0, no, ninguno…). Lanza LevelError si no se entiende."""
    s = normalize_key(value)
    if s in _NO_LEVEL:
        return None
    for level, words in _LEVEL_WORDS.items():
        if s in words:
            return level
    raise LevelError(f"nivel «{_cell_to_str(value).strip()}» no reconocido (usa novato, intermedio o experto)")
