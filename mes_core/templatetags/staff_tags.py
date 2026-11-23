"""Filtros para las pantallas de personal (niveles de polivalencia y avance de cuadrillas)."""

from django import template

from mes_core.normalize import display_name

register = template.Library()

_SHORT = {1: "N", 2: "I", 3: "E"}
_LABEL = {1: "Novato", 2: "Intermedio", 3: "Experto"}


@register.filter
def lvl_short(level):
    return _SHORT.get(level, "")


@register.filter
def lvl_label(level):
    return _LABEL.get(level, "")


@register.filter
def person(value):
    """Nombre normalizado para mostrar con mayúscula inicial."""
    return display_name(value or "")


@register.filter
def uname(user, fallback="—"):
    """Nombre completo o usuario; ``fallback`` si no hay usuario (seguro con None)."""
    if not user:
        return fallback
    return user.get_full_name() or user.get_username()


@register.filter
def minus(a, b):
    try:
        return int(a) - int(b)
    except (TypeError, ValueError):
        return ""


@register.filter
def crew_pct(assigned, required):
    try:
        return max(0, min(100, round(int(assigned) * 100 / int(required)))) if int(required) else 100
    except (TypeError, ValueError):
        return 0


@register.filter
def crew_state(assigned, required):
    """ok = completa, warn = parcial, bad = nadie asignado."""
    try:
        a, r = int(assigned), int(required)
    except (TypeError, ValueError):
        return ""
    if a >= r:
        return "ok"
    return "warn" if a else "bad"
