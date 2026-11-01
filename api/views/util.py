import collections.abc

from drf_yasg import openapi
from rest_framework.versioning import NamespaceVersioning
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from rest_framework import status


types_dict = {
    int: openapi.TYPE_INTEGER,
    str: openapi.TYPE_STRING
}


def string_parameter(name, description='', required=False):
    if not description:
        description = name
    return openapi.Parameter(
        name, openapi.IN_QUERY, description=description, type=openapi.TYPE_STRING, required=required,
    )


def integer_parameter(name, description='', required=False):
    if not description:
        description = name
    return openapi.Parameter(
        name, openapi.IN_QUERY, description=description, type=openapi.TYPE_INTEGER, required=required,
    )

def number_parameter(name, description='', required=False):
    if not description:
        description = name
    return openapi.Parameter(
        name, openapi.IN_QUERY, description=description, type=openapi.TYPE_NUMBER, required=required,
    )

def boolean_parameter(name, description='', default=None):
    if not description:
        description = name
    return openapi.Parameter(
        name, openapi.IN_QUERY, description=description, type=openapi.TYPE_BOOLEAN, default=default,
    )


def choices_parameter(name, choices, description='', required=False):
    if not description:
        description = name
    if isinstance(choices[0], collections.abc.Sequence) and not isinstance(choices[0], str):
        enum = [c[0] for c in choices]
    else:
        enum = choices
    type_ = types_dict[type(enum[0])]
    return openapi.Parameter(name, openapi.IN_QUERY, description=description, type=type_, enum=enum, required=required)


def isodate_parameter(name, description='', required=False):
    if not description:
        description = "ISO 8601 formatted"
    return openapi.Parameter(name, openapi.IN_QUERY, description=description, type=openapi.TYPE_STRING, required=required)


def header_string_parameter(name, description='', required=False):
    return openapi.Parameter(
        name, openapi.IN_HEADER, description=description, type=openapi.TYPE_STRING, required=required)


class UserPagination(LimitOffsetPagination):
    default_limit = 20
    max_limit = 100


def bad_request(detail):
    return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)


class MoloVersioning(NamespaceVersioning):
    default_version = 'v1'
    allowed_versions = ['v1', 'v2']
    version_param = 'api_version'


def default_area_id():
    """Erstes Gebiet aus settings.DEFAULT_AREA_IDS oder None."""
    from django.conf import settings
    ids = getattr(settings, "DEFAULT_AREA_IDS", None) or []
    return ids[0] if ids else None


def related_source(user):
    """Quelle, unter der ``user`` per API Inhalte anlegt: die erste Quelle mit
    ``related_user == user`` oder None. Mehrere oder keine verknüpften Quellen
    lösen so keinen Serverfehler aus."""
    from content.models import Source

    return Source.objects.filter(related_user=user).order_by("id").first()


def area_filter(area_id):
    """Filter-Argumente für ein Gebiet; ohne Gebiet wird nicht eingeschränkt."""
    return {"area": area_id} if area_id is not None else {}


def check_token_payload(payload):
    """Prüft Konto und Widerruf eines bereits signaturgeprüften Token-Payloads.

    Das Konto muss existieren und aktiv sein. Tokens, die vor der letzten
    Passwortänderung (``User.tokens_valid_after``) ausgestellt wurden, sind
    widerrufen - außer dem Access-Token, mit dem das Passwort geändert wurde.
    Gibt das Konto zurück, sonst ``jwt.InvalidTokenError``.
    """
    import jwt
    from content.models import User

    user_id = payload.get("user_id")
    user = User.objects.filter(pk=user_id, is_active=True).first() if user_id else None
    if user is None:
        raise jwt.InvalidTokenError("Konto unbekannt oder deaktiviert")
    if user.tokens_valid_after is not None:
        try:
            issued_at = int(payload.get("iat") or 0)
        except (TypeError, ValueError):
            issued_at = 0
        exempt = bool(user.token_exempt_jti) and payload.get("jti") == user.token_exempt_jti
        if issued_at < int(user.tokens_valid_after.timestamp()) and not exempt:
            raise jwt.InvalidTokenError("Token nach Passwortänderung widerrufen")
    return user


def decode_access_token(token):
    """Prüft ein API-Token wie ``jwt.decode`` und zusätzlich Typ, Konto und Widerruf.

    Akzeptiert nur Access-Tokens (keine Refresh-Tokens) von existierenden,
    aktiven Konten, die nicht durch eine Passwortänderung widerrufen sind.
    Fehler werden als ``jwt.InvalidTokenError`` gemeldet, damit die bestehenden
    ``except``-Zweige der Views greifen.
    """
    import jwt
    from django.conf import settings

    if token and token.startswith("Bearer "):
        token = token[len("Bearer "):]
    payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("token_type") != "access":
        raise jwt.InvalidTokenError("Kein Access-Token")
    check_token_payload(payload)
    return payload


def user_from_request_token(request):
    """Konto aus dem Authorization-Header oder None (ungültige Tokens ebenfalls None)."""
    import jwt
    from content.models import User

    token = request.headers.get("Authorization")
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except jwt.InvalidTokenError:
        return None
    return User.objects.filter(pk=payload.get("user_id")).first()


def can_view_unpublished(request, obj):
    """Unveröffentlichte Inhalte sehen nur die eigene Organisation und Superuser."""
    user = user_from_request_token(request)
    if user is None:
        return False
    if user.is_superuser:
        return True
    source = getattr(obj, "source", None)
    return bool(source and source.related_user_id == user.pk)
