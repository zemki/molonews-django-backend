"""HTML-Bereinigung für Inhalte aus Feeds, Formularen und der API.

Artikel- und Event-Texte enthalten HTML aus fremden Quellen (RSS, Webseiten)
und von Redaktionen. Bevor sie gespeichert oder auf Seiten dieses Servers
ausgegeben werden, entfernt nh3 alles, was Script ausführen kann (script-Tags,
Event-Handler, javascript:-Links, iframes, ...). Normale Formatierung bleibt.
"""
import nh3

ALLOWED_TAGS = set(nh3.ALLOWED_TAGS)

ALLOWED_ATTRIBUTES = {
    "*": {"class", "style", "title", "lang", "dir"},
    "a": {"href", "hreflang", "target"},
    "img": {"src", "alt", "width", "height", "loading"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "ol": {"start", "type"},
    "time": {"datetime"},
}

ALLOWED_STYLE_PROPERTIES = {
    "text-align",
    "color",
    "background-color",
    "font-weight",
    "font-style",
    "text-decoration",
    "width",
    "height",
    "float",
    "margin",
    "margin-left",
    "margin-right",
}


def sanitize_html(value):
    """Gibt eine bereinigte Kopie von ``value`` zurück (None bleibt None)."""
    if not value:
        return value
    return nh3.clean(
        str(value),
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        filter_style_properties=ALLOWED_STYLE_PROPERTIES,
        link_rel="noopener noreferrer",
    )
