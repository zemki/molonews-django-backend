"""
Schutz vor Server-Side Request Forgery (SSRF) bei Quell-URLs.

Quellen-Links, Bild- und Artikeladressen aus Feeds werden vom Server abgerufen
(Import, Script-Generierung, Link-Check). Ohne Prüfung könnte eine Adresse auf
interne Dienste zeigen, z. B. ``http://127.0.0.1:11434``,
``http://169.254.169.254`` (Cloud-Metadaten) oder Rechner im lokalen Netz.

``check_url`` / ``is_public_http_url`` erlauben nur http(s)-URLs, deren Hostname
ausschließlich auf öffentliche Adressen auflöst. Diese Vorabprüfung liefert
verständliche Fehlermeldungen (``ValueError``) für Formulare und Importe.

``safe_get`` prüft zusätzlich beim Verbindungsaufbau: Der Hostname wird in dem
Moment aufgelöst, in dem die TCP-Verbindung entsteht, alle Adressen werden
geprüft, und die Verbindung geht genau an eine dieser geprüften Adressen. Eine
zweite DNS-Antwort, die auf ein internes Ziel zeigt (DNS-Rebinding), wird so
nicht mehr verwendet. Host-Header, TLS-SNI und Zertifikatsprüfung nutzen
weiterhin den Hostnamen. Jede Weiterleitung wird erneut geprüft.

Grenzen: Ist für ausgehende Verbindungen ein Proxy gesetzt (``HTTP_PROXY`` /
``HTTPS_PROXY``), verbindet sich der Server mit dem Proxy; dann gilt nur die
Vorabprüfung, und der Proxy muss interne Ziele selbst sperren. Für zusätzlichen
Schutz ausgehende Verbindungen des Servers per Firewall beschränken.

Für lokale Entwicklung lassen sich alle Prüfungen mit der Einstellung
``ALLOW_PRIVATE_IMPORT_URLS = True`` abschalten.
"""

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError
from urllib3.util.connection import allowed_gai_family

ALLOWED_SCHEMES = ("http", "https")


def _private_checks_disabled() -> bool:
    return bool(getattr(settings, "ALLOW_PRIVATE_IMPORT_URLS", False))


def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def check_url(url: str) -> str:
    """Gibt eine Fehlermeldung zurück oder einen leeren String, wenn die URL erlaubt ist."""
    if not url or not isinstance(url, str):
        return "Keine URL angegeben."
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "Ungültige URL."
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return "Nur http- und https-Adressen sind erlaubt."
    host = parts.hostname
    if not host:
        return "Die URL enthält keinen Hostnamen."
    if _private_checks_disabled():
        return ""
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError:
        return "Ungültiger Port in der URL."
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return f"Der Hostname {host} lässt sich nicht auflösen."
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return f"Der Hostname {host} lässt sich nicht auflösen."
    for address in addresses:
        if not _is_public_ip(address):
            return f"Die Adresse {host} zeigt auf ein internes Netz ({address}) und ist nicht erlaubt."
    return ""


def is_public_http_url(url: str) -> bool:
    """True, wenn ``url`` eine http(s)-URL ist, die nur auf öffentliche Adressen auflöst."""
    return check_url(url) == ""


def assert_public_http_url(url: str) -> None:
    """Wie ``is_public_http_url``, wirft aber ``ValueError`` mit Begründung."""
    error = check_url(url)
    if error:
        raise ValueError(error)


# --------------------------------------------------------------------------
# Prüfung beim Verbindungsaufbau (Schutz vor DNS-Rebinding)
# --------------------------------------------------------------------------
class BlockedAddressError(NewConnectionError):
    """Der Hostname löst beim Verbindungsaufbau auf eine interne Adresse auf.

    Erbt von ``NewConnectionError``; ``requests`` meldet den Fall daher als
    ``requests.ConnectionError``.
    """


class _PublicAddressMixin:
    """Löst den Hostnamen selbst auf und verbindet nur zu geprüften öffentlichen Adressen."""

    def _new_conn(self):
        if _private_checks_disabled():
            return super()._new_conn()

        hostname = self._dns_host
        try:
            infos = socket.getaddrinfo(
                hostname.strip("[]"), self.port, allowed_gai_family(), socket.SOCK_STREAM
            )
        except socket.gaierror as exc:
            raise NameResolutionError(self.host, self, exc) from exc

        # Reihenfolge der Auflösung beibehalten, Dubletten entfernen
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        blocked = [address for address in addresses if not _is_public_ip(address)]
        if not addresses or blocked:
            raise BlockedAddressError(
                self,
                f"Verbindung zu {self.host} gesperrt: Der Hostname zeigt auf ein "
                f"internes Netz ({blocked[0] if blocked else 'keine Adresse'}).",
            )

        # Zur geprüften Adresse verbinden. _dns_host wird nur für den
        # Socket-Aufbau ersetzt: Host-Header, TLS-SNI und Zertifikatsprüfung
        # lesen self.host erst danach und sehen wieder den Hostnamen.
        last_error = None
        for address in addresses:
            self._dns_host = address
            try:
                return super()._new_conn()
            except ConnectTimeoutError as exc:  # schließt NewConnectionError ein
                last_error = exc
            finally:
                self._dns_host = hostname
        raise last_error


class _PublicHTTPConnection(_PublicAddressMixin, HTTPConnection):
    pass


class _PublicHTTPSConnection(_PublicAddressMixin, HTTPSConnection):
    pass


class _PublicHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PublicHTTPConnection


class _PublicHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PublicHTTPSConnection


class PublicAddressAdapter(HTTPAdapter):
    """requests-Adapter, dessen Verbindungen nur zu öffentlichen Adressen aufgebaut werden.

    Gilt für direkte Verbindungen. Über einen Proxy verbindet sich der Server mit
    dem Proxy; dann greift nur die Vorabprüfung in ``safe_get``.
    """

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _PublicHTTPConnectionPool,
            "https": _PublicHTTPSConnectionPool,
        }


def safe_get(url: str, max_redirects: int = 5, **kwargs):
    """``requests.get`` mit SSRF-Prüfung der Start-URL, jeder Weiterleitung und jeder Verbindung.

    Wirft ``ValueError``, wenn die Vorabprüfung eine URL ablehnt (Schema,
    Hostname, interne Adresse). Zeigt der Hostname erst beim Verbindungsaufbau
    auf eine interne Adresse, entsteht ``requests.ConnectionError``. Sonst wie
    ``requests.get`` (alle Argumente außer ``allow_redirects``, auch
    ``stream=True``).
    """
    kwargs.setdefault("timeout", 20)
    kwargs["allow_redirects"] = False
    assert_public_http_url(url)
    with requests.Session() as session:
        adapter = PublicAddressAdapter()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        response = session.get(url, **kwargs)
        hops = 0
        while response.is_redirect and hops < max_redirects:
            target = urljoin(response.url, response.headers.get("Location", ""))
            assert_public_http_url(target)
            response.close()
            response = session.get(target, **kwargs)
            hops += 1
    return response
