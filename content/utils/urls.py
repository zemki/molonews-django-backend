"""Prüfung von Links, die in ``href`` ausgegeben werden (Schutz vor ``javascript:``-XSS).

Unsicher sind nur Schemata, über die ein Browser Code ausführt: ``javascript:``,
``vbscript:`` und ``data:``. Alles andere bleibt erhalten, auch Freitext wie
"Tickets: Abendkasse" oder Schemata wie ``webcal:``. Browser ignorieren
Steuerzeichen und Leerraum im Schema ("java\\tscript:"), deshalb werden sie vor
der Prüfung entfernt.
"""

import re

UNSAFE_LINK_SCHEMES = ("javascript", "vbscript", "data", "livescript")

_IGNORED_CHARS_RE = re.compile(r"[\x00-\x20\x7f]+")
_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.IGNORECASE)


def link_scheme(value) -> str:
    """Schema eines Links in Kleinbuchstaben oder '' für Links ohne Schema."""
    if not value:
        return ""
    compact = _IGNORED_CHARS_RE.sub("", str(value))
    match = _SCHEME_RE.match(compact)
    return match.group(1).lower() if match else ""


def is_safe_link(value) -> bool:
    """False nur für Links mit Script-Schema (javascript:, vbscript:, data:)."""
    if not value:
        return True
    return link_scheme(value) not in UNSAFE_LINK_SCHEMES


def safe_link_or_empty(value):
    """Gibt den Link zurück, wenn er sicher ist, sonst einen leeren String."""
    return value if is_safe_link(value) else ""
