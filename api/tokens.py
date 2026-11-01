"""Token-Serializer mit Konto- und Widerrufsprüfung für Refresh und Verify.

SimpleJWT prüft beim Refresh nur Signatur, Laufzeit und ``is_active``. Nach
einer Passwortänderung müssen aber auch vorher ausgestellte Refresh-Tokens
ungültig sein (siehe ``User.tokens_valid_after``).
"""

import jwt
from rest_framework import serializers
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenRefreshSerializer, TokenVerifySerializer
from rest_framework_simplejwt.tokens import RefreshToken, UntypedToken

from api.views.util import check_token_payload


class CheckedTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        refresh = RefreshToken(attrs["refresh"])
        try:
            check_token_payload(refresh.payload)
        except jwt.InvalidTokenError:
            raise AuthenticationFailed("Token is invalid or revoked", "token_not_valid")
        return super().validate(attrs)


class CheckedTokenVerifySerializer(TokenVerifySerializer):
    def validate(self, attrs):
        token = UntypedToken(attrs["token"])
        try:
            check_token_payload(token.payload)
        except jwt.InvalidTokenError:
            raise serializers.ValidationError("Token is invalid or revoked")
        return super().validate(attrs)
