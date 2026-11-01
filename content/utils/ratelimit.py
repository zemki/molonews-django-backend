"""Einfache Ratenbegrenzung pro Client-IP für Django-Views ohne DRF.

Nutzt dieselben Raten (``REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]``) und dieselbe
Client-IP-Bestimmung (``NUM_PROXIES``) wie die DRF-Throttles der API.
"""

import time

from django.conf import settings
from django.core.cache import cache

_PERIODS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_rate(rate):
    """'30/min' -> (30, 60); None oder ungültig -> None."""
    if not rate:
        return None
    try:
        count, period = str(rate).split("/", 1)
        return int(count), _PERIODS[period.strip()[0].lower()]
    except (ValueError, KeyError, IndexError):
        return None


def client_ip(request):
    """Client-IP wie DRF: die von ``NUM_PROXIES`` Proxys gesetzte Adresse aus X-Forwarded-For."""
    remote = request.META.get("REMOTE_ADDR", "")
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    num_proxies = settings.REST_FRAMEWORK.get("NUM_PROXIES")
    if forwarded and num_proxies:
        addresses = [a.strip() for a in forwarded.split(",") if a.strip()]
        if addresses:
            return addresses[-min(num_proxies, len(addresses))]
    return remote


def seconds_until_allowed(request, scope):
    """0, wenn die Anfrage erlaubt ist, sonst die Wartezeit in Sekunden.

    Zählt die Anfrage mit, wenn sie erlaubt ist.
    """
    parsed = _parse_rate(settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}).get(scope))
    if parsed is None:
        return 0
    limit, window = parsed
    key = f"ratelimit:{scope}:{client_ip(request)}"
    now = time.time()
    history = [t for t in (cache.get(key) or []) if now - t < window]
    if len(history) >= limit:
        return int(window - (now - history[0])) + 1
    history.append(now)
    cache.set(key, history, window)
    return 0
