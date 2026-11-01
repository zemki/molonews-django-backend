from django import template
from django.utils.safestring import mark_safe

from content.utils.html import sanitize_html
from content.utils.urls import safe_link_or_empty

register = template.Library()


@register.filter(name="sanitize_html")
def sanitize_html_filter(value):
    """Gibt HTML aus Inhalten bereinigt aus (Ersatz für |safe)."""
    return mark_safe(sanitize_html(value or ""))


@register.filter(name="safe_url")
def safe_url_filter(value):
    """Gibt einen Link nur aus, wenn sein Schema sicher ist (kein javascript: usw.)."""
    return safe_link_or_empty(value or "")
