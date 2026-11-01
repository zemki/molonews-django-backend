from django.http import HttpResponse
from django.core.exceptions import ValidationError as DjangoValidationError
from django.contrib.auth.password_validation import validate_password
from rest_framework import viewsets, serializers
from drf_yasg.utils import swagger_auto_schema, swagger_serializer_method
from rest_framework.response import Response
from .util import header_string_parameter, string_parameter
from django.shortcuts import get_object_or_404
from logging import getLogger
from content.models import User, Organization, Source, Category, AppUser, Area
from rest_framework import status
from drf_yasg import openapi
from django.contrib.auth.models import Group
import json
import re
import uuid
from urllib.parse import quote

import jwt
from api.views.util import decode_access_token
from content.permissions import can_edit_organization
from content.utils.mail import send_html_mail
from api.tokens import CheckedTokenVerifySerializer
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from rest_framework_simplejwt.views import TokenObtainPairView, TokenVerifyView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from rest_framework.throttling import ScopedRateThrottle
from django.utils import timezone
import hashlib

logger = getLogger("molonews")

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'email', 'password']
        extra_kwargs = {'password': {'write_only': True}}

class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ['street', 'zip', 'town', 'description' ]

class AreaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Area
        fields = ['id', 'name']
        ref_name = "areas" 



def _password_error(password, user=None):
    """Prüft ein neues Passwort gegen AUTH_PASSWORD_VALIDATORS (None = ok)."""
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        return " ".join(exc.messages)
    return None

def _hash_reset_token(token):
    """SHA-256 eines Reset-Tokens; in der Datenbank steht nur dieser Hash."""
    return hashlib.sha256(str(token or "").strip().encode()).hexdigest()


def _reset_token_is_fresh(user):
    """True, wenn das Reset-Token jünger als PASSWORD_RESET_TIMEOUT ist."""
    requested_at = user.password_reset_requested_at
    if requested_at is None:
        return False
    return (timezone.now() - requested_at).total_seconds() <= settings.PASSWORD_RESET_TIMEOUT


def _valid_uuid(value):
    """Gibt ``value`` als normalisierte UUID zurück oder None."""
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _app_redirect_page(link, message, link_text):
    """Kleine HTML-Seite, die in die App weiterleitet (alle Werte escaped)."""
    html = format_html(
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url={link}"></head>'
        '<body><p>{message}</p><p><a href="{link}">{link_text}</a></p>'
        '<script>window.location.href = {link_js};</script></body></html>',
        link=link,
        message=message,
        link_text=link_text,
        link_js=mark_safe(json.dumps(link).replace('<', '\\u003c')),
    )
    response = HttpResponse(html)
    response['Referrer-Policy'] = 'no-referrer'
    response['Cache-Control'] = 'no-store'
    return response

def send_activation_email(user):
    """Send the activation mail and rotate the activation key on success.

    Generates a candidate key + fresh JWT, dispatches the mail, and only then
    persists the new key. On SMTP failure the previously stored key remains
    valid, so an earlier link (if any) is not silently invalidated.

    Returns True when a mail was dispatched, False otherwise.
    """
    if not user.email:
        return False

    candidate_key = uuid.uuid4()
    access_token = str(AccessToken.for_user(user))

    sender_email = settings.MAIL_SENDING['DEFAULT_FROM_EMAIL']
    mail_activation_url = settings.MAIL_SENDING['ACTIVATION_URL']
    subject = 'Bestätigungsmail'
    message = (
        f'<html><body>'
        f'<p>Liebe(r) {escape(user.first_name)} {escape(user.last_name)},</p>'
        f'<p>Bitte bestätigen Sie Ihre E-Mail-Adresse, um Ihr molo Konto zu aktivieren:</p>'
        f'<p><a href="{mail_activation_url.rstrip("/")}/?token={candidate_key}&bearer={access_token}">E-Mail bestätigen</a></p>'
        f'</body></html>'
    )

    try:
        send_html_mail(subject, message, user.email, sender_email)
    except Exception as e:
        logger.error(f"Activation mail dispatch failed for user_id={user.pk}: {e}")
        return False

    user.activation_key = candidate_key
    user.save(update_fields=['activation_key'])
    return True


def add_organization_to_appusers(organization):
    try:


        # Through-Tabellen
        #T_org       = AppUser.organization.through
        T_alltags   = AppUser.organization_all_tags.through
        #T_desel     = AppUser.deselected_organization.through
        #T_desel_all = AppUser.deselected_organization_all_tags.through

        appuser_ids = list(AppUser.objects.values_list('id', flat=True))

        #rows_org     = [T_org(appuser_id=au_id,     organization_id=organization.id) for au_id in appuser_ids]
        rows_alltags = [T_alltags(appuser_id=au_id, organization_id=organization.id) for au_id in appuser_ids]

        with transaction.atomic():
            # aktiv setzen
            #T_org.objects.bulk_create(rows_org, ignore_conflicts=True)
            T_alltags.objects.bulk_create(rows_alltags, ignore_conflicts=True)
            # evtl. Abwahlen aufheben
            #T_desel.objects.filter(organization_id=organization.id).delete()
            #T_desel_all.objects.filter(organization_id=organization.id).delete()

    except Exception as e:
        logger.error(f"Fehler beim Hinzufügen der Organisation zu AppUsern: {e}")

class UserGetSerializer(serializers.ModelSerializer):

    town = serializers.SerializerMethodField()
    street = serializers.SerializerMethodField()
    zip = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    picture = serializers.SerializerMethodField()
    image_source = serializers.SerializerMethodField()
    homepage = serializers.SerializerMethodField()
    area = AreaSerializer(many=False, read_only=True)
    source_ids = serializers.SerializerMethodField()
    organization_id = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'description', 'picture', 'image_source','email', 'homepage', 'phone', 'display_name', 'area', 'town', 'street', 'zip', 'source_ids', 'organization_id']
    
    def get_id(self, obj):
        #return user id
        return obj.id
    
    def get_town(self, obj):
        organization = Organization.objects.get(related_user=obj)
        return organization.town if organization else None
    
    def get_street(self, obj):
        organization = Organization.objects.get(related_user=obj)
        return organization.street if organization else None
    
    def get_zip(self, obj):
        organization = Organization.objects.get(related_user=obj)
        return organization.zip if organization else None
    
    def get_description(self, obj):
        organization = Organization.objects.get(related_user=obj)
        return organization.description if organization else None
    
    def get_picture(self, obj):
        organization = Organization.objects.get(related_user=obj)
        return organization.image if organization else None
    
    def get_image_source(self, obj):
        organization = Organization.objects.get(related_user=obj)
        return organization.image_source if organization else None
    
    def get_homepage(self, obj):
        organization = Organization.objects.get(related_user=obj)
        return organization.homepage if organization else None

    def get_source_ids(self, obj):
        return list(obj.sources.values_list('id', flat=True))

    @swagger_serializer_method(serializers.IntegerField)
    def get_organization_id(self, obj):
        try:
            organization = Organization.objects.get(related_user=obj)
            return organization.id
        except Organization.DoesNotExist:
            return None


class AdminUserViewSet(viewsets.ViewSet):
    queryset = User.objects.all()

    # Ratenbegrenzung pro Client-IP zusätzlich zu den nginx-Limits
    # (Raten: REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
    THROTTLE_SCOPES = {
        "create": "auth",
        "change_password": "auth",
        "reset_password_with_token": "auth",
        "request_password_reset": "mail",
        "resend_activation": "mail",
    }

    def get_throttles(self):
        scope = self.THROTTLE_SCOPES.get(getattr(self, "action", None))
        if not scope:
            return []
        self.throttle_scope = scope
        return [ScopedRateThrottle()]

    # create a new admin user
    @swagger_auto_schema(
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'username': openapi.Schema(type=openapi.TYPE_STRING),
                'first_name': openapi.Schema(type=openapi.TYPE_STRING),
                'last_name': openapi.Schema(type=openapi.TYPE_STRING),
                'phone': openapi.Schema(type=openapi.TYPE_STRING),
                'email': openapi.Schema(type=openapi.TYPE_STRING),
                'password': openapi.Schema(type=openapi.TYPE_STRING),
                'display_name': openapi.Schema(type=openapi.TYPE_STRING),
                'street': openapi.Schema(type=openapi.TYPE_STRING),
                'zip': openapi.Schema(type=openapi.TYPE_STRING),
                'town': openapi.Schema(type=openapi.TYPE_STRING),
                'area': openapi.Schema(type=openapi.TYPE_INTEGER),
            }
        ),
         manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        responses={
            status.HTTP_204_NO_CONTENT: "User created successfully",
            status.HTTP_400_BAD_REQUEST: "Invalid data provided"
        }
    )
    def create(self, request):

        try:
            with transaction.atomic():
                # Benutzer erstellen
                user = User()
                if 'username' in request.data:
                    user.username = request.data['username']
                    # replace all spaces in the username with underscores
                    user.username = user.username.replace(" ", "_")
                    # remove all whitespaces at the beginning and end of user.username
                    user.username = user.username.strip()

                else:
                    logger.error("Username is required")
                    return Response(data={"message": "Username is required"}, status=status.HTTP_400_BAD_REQUEST)

                if 'first_name' in request.data:
                    user.first_name = request.data['first_name']

                if 'last_name' in request.data:
                    user.last_name = request.data['last_name']

                if 'display_name' in request.data:
                    display_name = request.data['display_name']
                    if not display_name:
                        logger.error("Empty display_name")
                        return Response(data={"message": "Empty display_name"}, status=status.HTTP_400_BAD_REQUEST)
                    
                    if User.objects.filter(display_name=display_name).exclude(username=request.data['username']).exists():
                        logger.error("User display name already exists")
                        return Response(data={"message": "User display name already exists"}, status=status.HTTP_400_BAD_REQUEST)
                    user.display_name = display_name
                else:
                    logger.error("display name is required")
                    return Response(data={"message": "Display name is required"}, status=status.HTTP_400_BAD_REQUEST)

                if 'email' in request.data and request.data['email']:
                    if User.objects.filter(email=request.data['email']).exists():
                        logger.warning("Email address already taken")
                        return Response(data={"message": "Email address already taken"}, status=status.HTTP_400_BAD_REQUEST)
                    user.email = request.data['email']
                else:
                    # E-Mail ist Pflicht, weil die Aktivierung ueber den Bestaetigungslink laeuft.
                    logger.warning("Email is required")
                    return Response(data={"message": "Email is required"}, status=status.HTTP_400_BAD_REQUEST)

                if 'phone' in request.data:
                    user.phone = request.data['phone']
                else:
                    user.phone = ""

                if 'password' in request.data:
                    password_error = _password_error(request.data['password'], user)
                    if password_error:
                        return Response(data={"message": password_error}, status=status.HTTP_400_BAD_REQUEST)
                    user.set_password(request.data['password'])
                else:
                    return Response(data={"message": "Password is required"}, status=status.HTTP_400_BAD_REQUEST)

                user.is_staff = True
                user.is_superuser = False
                user.can_publish = True
                # Selbst registrierte Konten verwalten ihre eigene Organisation
                user.can_modify_organization_info = True
                # Konto bleibt inaktiv, bis der Nutzer den Bestaetigungslink aus der Aktivierungsmail oeffnet.
                # activation_key wird erst durch send_activation_email nach erfolgreichem Versand gesetzt.
                user.is_active = False

                # Gebiete vor dem Speichern prüfen (ein return nach save() im atomic-Block
                # würde ein halbes Konto committen): aus der Anfrage, sonst vom Gerät, sonst DEFAULT_AREA_IDS
                if 'area' in request.data:
                    try:
                        registration_areas = [Area.objects.get(id=request.data['area'])]
                    except (Area.DoesNotExist, ValueError, TypeError):
                        logger.error("invalid area id")
                        return Response(data={"message": "Invalid area ID"}, status=status.HTTP_400_BAD_REQUEST)
                else:
                    app_device_id = request.headers.get('X-Device-ID')
                    app_user = AppUser.objects.filter(device_id=app_device_id).first() if app_device_id else None
                    if app_user and app_user.area_id:
                        registration_areas = [app_user.area]
                    else:
                        registration_areas = list(Area.objects.filter(pk__in=settings.DEFAULT_AREA_IDS))

                user.save()
                user.area.set(registration_areas)
                
                # Gruppe zuweisen
                group = Group.objects.get(name="Contributors")
                user.groups.add(group)
                user.save()

                # Organisation erstellen und Benutzer zuweisen
                organization = Organization()
                organization.related_user = user
                organization.name = display_name
                organization.title = display_name

                if 'street' in request.data:
                    organization.street = request.data['street']
                else:
                    organization.street = ""

                if 'zip' in request.data:
                    organization.zip = request.data['zip']
                else:
                    organization.zip = ""

                if 'town' in request.data:
                    organization.town = request.data['town']
                else:
                    organization.town = ""
                organization.type = "collective"
                organization.active = True
                organization.description = ""
                organization.homepage = ""
                organization.image_source = ""
                organization.image = ""
                organization.save()

                organization.area.set(registration_areas)

                organization.save()
                """
                Fügt die gegebene Organisation zu allen bestehenden AppUsern hinzu.
                """
                add_organization_to_appusers(organization)
                # Quelle erstellen und Benutzer zuweisen
                source = Source()
                source.related_user = user
                source.name = display_name
                source.type = "local"
                source.active = True
                # Kategorie 1 = Artikel-Themen (angelegt von Migration 0005)
                source.default_category = Category.objects.get(id=1)
                source.organization = organization
                source.save()

                # Quelle zu Benutzer hinzufügen
                user.sources.add(source)
                user.save()
                source.area.set(registration_areas)
                source.save()

                # Mail erst nach erfolgreichem Commit senden, damit SMTP keine Row-Locks haelt.
                transaction.on_commit(lambda u=user: send_activation_email(u))

        except Exception as e:
            logger.error(f"Error during user creation: {str(e)}")
            logger.error("Registration failed: %s", e)
            return Response(data={"message": "Error creating user or related entities."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_204_NO_CONTENT)


    # update an existing user
    @swagger_auto_schema(
        operation_description='Update a molo user',
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'username': openapi.Schema(type=openapi.TYPE_STRING),
                'first_name': openapi.Schema(type=openapi.TYPE_STRING),
                'last_name': openapi.Schema(type=openapi.TYPE_STRING),
                'phone': openapi.Schema(type=openapi.TYPE_STRING),
                'email': openapi.Schema(type=openapi.TYPE_STRING),
                'homepage': openapi.Schema(type=openapi.TYPE_STRING),
                'area': openapi.Schema(type=openapi.TYPE_INTEGER),
                'display_name': openapi.Schema(type=openapi.TYPE_STRING),
                'description': openapi.Schema(type=openapi.TYPE_STRING),
                'street': openapi.Schema(type=openapi.TYPE_STRING),
                'zip': openapi.Schema(type=openapi.TYPE_STRING),
                'town': openapi.Schema(type=openapi.TYPE_STRING),
            }
        ),

        responses={
            status.HTTP_204_NO_CONTENT: "User updated successfully",
            status.HTTP_400_BAD_REQUEST: "Invalid data provided",
            status.HTTP_401_UNAUTHORIZED: "Unauthorized"
        }
    )
    def update_user(self, request):
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return Response({"detail": "Authorization header missing."}, status=status.HTTP_401_UNAUTHORIZED)

        token = auth_header

        try:
            payload = decode_access_token(token)
            user_id = payload.get('user_id')
            if not user_id:
                return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)

            user = get_object_or_404(User, id=user_id)
        except jwt.ExpiredSignatureError:
            return Response({"detail": "Token has expired."}, status=status.HTTP_401_UNAUTHORIZED)
        except jwt.InvalidTokenError:
            return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)

        if not user.is_authenticated:
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        try:
            with transaction.atomic():
                # Update User Details
                if 'username' in request.data:
                    user.username = request.data['username']
                if 'first_name' in request.data:
                    user.first_name = request.data['first_name']
                if 'last_name' in request.data:
                    user.last_name = request.data['last_name']
                if 'display_name' in request.data:
                    new_display_name = request.data['display_name']
                    if User.objects.filter(display_name=new_display_name).exclude(id=user.id).exists():
                        return Response(data={"message": "User display name already exists"}, status=status.HTTP_400_BAD_REQUEST)
                    if new_display_name.strip():
                        user.display_name = new_display_name

                if 'email' in request.data and request.data['email']:
                    if User.objects.filter(email=request.data['email']).exclude(id=user.id).exists():
                        return Response(data={"message": "Email address already taken"}, status=status.HTTP_400_BAD_REQUEST)
                    user.email = request.data['email']
                elif 'email' in request.data:
                    user.email = None
                if 'phone' in request.data:
                    user.phone = request.data['phone']
                if 'area' in request.data:
                    try:
                        area = Area.objects.get(id=request.data['area'])
                        user.area.clear()
                        user.area.add(area)
                    except Area.DoesNotExist:
                        return Response(data={"message": "Invalid area ID"}, status=status.HTTP_400_BAD_REQUEST)

                user.save()

                # Update Organization Details – nur mit can_modify_organization_info
                organization = Organization.objects.filter(related_user=user).first()
                may_edit_organization = can_edit_organization(user, organization)
                if organization and may_edit_organization:
                    if 'display_name' in request.data:
                        if new_display_name.strip():
                            organization.name = new_display_name
                            organization.title = new_display_name
                    if 'street' in request.data:
                        organization.street = request.data['street']
                    if 'zip' in request.data:
                        organization.zip = request.data['zip']
                    if 'description' in request.data:
                        organization.description = request.data['description']
                    if 'town' in request.data:
                        organization.town = request.data['town']
                    if 'area' in request.data:
                        organization.area.clear()
                        organization.area.add(area)
                    if 'homepage' in request.data:
                        new_homepage = request.data['homepage']
                        if new_homepage.strip():
                            organization.homepage = new_homepage
                    organization.save()

                # Update Source Details
                source = Source.objects.filter(related_user=user).first()
                if source:
                    if 'display_name' in request.data and may_edit_organization:
                        if new_display_name.strip():
                            source.name = new_display_name
                    if 'area' in request.data:
                        source.area.clear()
                        source.area.add(area)
                    if organization:
                        source.organization = organization
                    source.save()

        except Exception as e:
            return Response(data={"message": f"Error updating user or related entities: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_204_NO_CONTENT)


    # method to activate a user
    @swagger_auto_schema(
        operation_description='Activate a user',
        manual_parameters=[
            string_parameter("token", "Activation token", required=True),
        ],
        responses={
            status.HTTP_200_OK: "User activated successfully",
            status.HTTP_400_BAD_REQUEST: "Invalid token provided",
            status.HTTP_404_NOT_FOUND: "User not found"
        }
    )
    def activate_user(self, request):
        # get the activation token from the request
        token = _valid_uuid(request.GET.get('token'))
        bearer = request.GET.get('bearer') or ''
        # JWTs bestehen nur aus Base64url-Zeichen und Punkten; alles andere verwerfen
        if not re.fullmatch(r'[A-Za-z0-9_\-.]*', bearer):
            bearer = ''

        if not token:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        app_link = 'molonews://already-activated?bearer=' + quote(bearer, safe='')

        # find the user with the matching activation token
        user = User.objects.filter(activation_key=token).first()
        if user is None:
            return _app_redirect_page(app_link, 'Das Konto ist bereits aktiviert.', 'zur App')

        # activate the user
        user.is_active = True
        # remove the activation key
        user.activation_key = None
        user.save()

        return _app_redirect_page(app_link, 'Das Konto wurde erfolgreich aktiviert.', 'zur App')

    @swagger_auto_schema(
        operation_description='Resend the activation email for an inactive account.',
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'email': openapi.Schema(type=openapi.TYPE_STRING),
                'username': openapi.Schema(type=openapi.TYPE_STRING),
            }
        ),
        responses={status.HTTP_204_NO_CONTENT: "Resend accepted"}
    )
    def resend_activation(self, request):
        """Trigger a fresh activation mail for an inactive account.

        Always returns 204 so the caller cannot enumerate accounts. Dispatches
        a mail only when an inactive user with a matching identifier exists
        and the per-user cooldown has elapsed. The cooldown is set only on
        successful dispatch, so an SMTP failure does not block a retry.
        """
        identifier = (request.data.get('email') or request.data.get('username') or '').strip()
        if identifier:
            user = User.objects.filter(
                Q(username=identifier) | Q(email=identifier),
                is_active=False,
            ).first()
            if user:
                cache_key = f'resend-activation:{user.pk}'
                if not cache.get(cache_key):
                    if send_activation_email(user):
                        cache.set(cache_key, True, 60)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # method to delete a user
    @swagger_auto_schema(
        operation_description='Delete an admin user',
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        responses={
            status.HTTP_204_NO_CONTENT: "User deleted successfully",
            status.HTTP_404_NOT_FOUND: "User not found"
        }
    )
    def delete_user(self, request):
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return Response({"detail": "Authorization header missing."}, status=status.HTTP_401_UNAUTHORIZED)

        token = auth_header

        try:
            payload = decode_access_token(token)
            user_id = payload.get('user_id')
            if not user_id:
                return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)
            user = get_object_or_404(User, id=user_id)
        except jwt.ExpiredSignatureError:
            return Response({"detail": "Token has expired."}, status=status.HTTP_401_UNAUTHORIZED)
        except jwt.InvalidTokenError:
            return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)

        if not user.is_authenticated:
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        try:
            with transaction.atomic():
                # Lösche alle Organisationen, die mit dem Benutzer verknüpft sind
                organizations = Organization.objects.filter(related_user=user)
                for organization in organizations:
                    organization.delete()

                # Lösche alle Quellen, die mit dem Benutzer verknüpft sind
                sources = Source.objects.filter(related_user=user)
                for source in sources:
                    source.delete()

                # Lösche den Benutzer
                user.delete()

        except Exception as e:
            logger.error(f"Error deleting user or related entities: {str(e)}")
            return Response(data={"message": f"Error deleting user or related entities: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_204_NO_CONTENT)
  

    @swagger_auto_schema(
        operation_description='Change user password',
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'new_password': openapi.Schema(type=openapi.TYPE_STRING),
            }
        ),
        responses={
            status.HTTP_200_OK: "Password changed successfully",
            status.HTTP_400_BAD_REQUEST: "Invalid data provided",
            status.HTTP_401_UNAUTHORIZED: "Unauthorized",
            status.HTTP_404_NOT_FOUND: "User not found"
        }
    )
    def change_password(self, request):
       
        # get the user from the token
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return Response({"detail": "Authorization header missing."}, status=status.HTTP_401_UNAUTHORIZED)

        token = auth_header

        try:
            payload = decode_access_token(token)
            user_id = payload.get('user_id')
            if not user_id:
                return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)
            
            user = get_object_or_404(User, id=user_id)
        except jwt.ExpiredSignatureError:
            return Response({"detail": "Token has expired."}, status=status.HTTP_401_UNAUTHORIZED)
        except jwt.InvalidTokenError:
            return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)

        # only delete the user if the user is authenticated
        if user.is_authenticated:
            # get the new password from the request
            new_password = request.data.get('new_password')

            if not new_password:
                return Response(status=status.HTTP_400_BAD_REQUEST)
            password_error = _password_error(new_password, user)
            if password_error:
                return Response(data={"message": password_error}, status=status.HTTP_400_BAD_REQUEST)

            # Passwort setzen; User.save() widerruft dabei alle bestehenden Tokens
            user.set_password(new_password)
            user.save()
            # Das aufrufende Gerät bleibt mit seinem Access-Token angemeldet
            user.token_exempt_jti = str(payload.get('jti') or '')
            user.save(update_fields=['token_exempt_jti'])
        else:
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        # Neue Tokens für Clients, die sie übernehmen wollen
        refresh = RefreshToken.for_user(user)
        return Response(
            {"access": str(refresh.access_token), "refresh": str(refresh)},
            status=status.HTTP_200_OK,
        )


    # this method is used to reset the password of a user 
    # using a token provided in a reset email
    @swagger_auto_schema(
        operation_description='Change user password based on reset token',
        manual_parameters=[
            header_string_parameter("Authorization", "Reset token", required=True),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'new_password': openapi.Schema(type=openapi.TYPE_STRING),
            }
        ),
        responses={
            status.HTTP_204_NO_CONTENT: "Password reset successful",
            status.HTTP_400_BAD_REQUEST: "Invalid data provided",
            status.HTTP_401_UNAUTHORIZED: "Unauthorized",
        }
    )
    def reset_password_with_token(self, request, *args, **kwargs):
       
        # get the user from the token
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return Response({"detail": "Authorization header missing."}, status=status.HTTP_401_UNAUTHORIZED)

        token = auth_header

        user = User.objects.filter(password_reset_token=_hash_reset_token(token)).first()
        if user is None:
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        if not _reset_token_is_fresh(user):
            # Abgelaufen: Token verwerfen, der Nutzer muss einen neuen Link anfordern
            user.password_reset_token = None
            user.password_reset_requested_at = None
            user.save(update_fields=['password_reset_token', 'password_reset_requested_at'])
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        # only delete the user if the user is authenticated
        try:
            # get the new password from the request
            new_password = request.data.get('new_password')

            if not new_password:
                return Response(status=status.HTTP_400_BAD_REQUEST)
            password_error = _password_error(new_password, user)
            if password_error:
                return Response(data={"message": password_error}, status=status.HTTP_400_BAD_REQUEST)

            # Passwort setzen; User.save() verbraucht das Reset-Token und
            # widerruft alle bestehenden API-Tokens
            user.set_password(new_password)
            user.save()
        except Exception:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_204_NO_CONTENT)



    # method to get user data
    @swagger_auto_schema(
        operation_description='Get user data',
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        responses={
            200: UserGetSerializer,
            401: "Unauthorized",
            404: "User not found",
        },
    )
    def get_user_details(self, request):
         # get the user from the token
         
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return Response({"detail": "Authorization header missing."}, status=status.HTTP_401_UNAUTHORIZED)

        token = auth_header

        try:
            payload = decode_access_token(token)
            user_id = payload.get('user_id')

            logger.error (user_id)

            if not user_id:
                return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)
            user = get_object_or_404(User, id=user_id)

        except jwt.ExpiredSignatureError:
            return Response({"detail": "Token has expired."}, status=status.HTTP_401_UNAUTHORIZED)
        except jwt.InvalidTokenError:
            return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)

        # only delete the user if the user is authenticated
        if user.is_authenticated:

            try:
                organization = Organization.objects.get(related_user=user)
            except:
                logger.error("Organization not found")
                return Response(status=status.HTTP_404_NOT_FOUND)

            # get the picture url and image source from the organization db if it exists
            try:
                picture = str(organization.image) if organization.image else None
                image_source = str(organization.image_source) if organization.image_source else None
                homepage = str(organization.homepage) if organization.homepage else None
                server_hostname = request.get_host()
                # Nur URL bauen wenn picture vorhanden ist
                picture_url = 'https://' + server_hostname + '/media/' + picture if picture else None
            except:
                picture_url = None
                image_source = None
                homepage = None

            data = {
                'username': user.username,
                'id': user.id,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'description': organization.description,
                'phone': user.phone,
                'email': user.email,
                'homepage': homepage,
                'display_name': str(user.display_name),
                'picture': picture_url,
                'image_source': image_source,
                'street': organization.street,
                'zip': organization.zip,
                'town': organization.town,
                'organization_id': organization.id,
            }

            areas = user.area.all()
            # create an area object containing the first area of the user
            for area in areas:
                area_list = {'id': area.id, 'name': area.name}
                break

            # add the area_list to the data
            data['area'] = area_list

            return Response(data, status=status.HTTP_200_OK)
            
        else:
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        

    @swagger_auto_schema(
        operation_description='Redirect user into app and reset password with token',
        manual_parameters=[
            string_parameter("token", "Reset token", required=True),
        ],
        responses={
            status.HTTP_302_FOUND: "Redirect to app",
            status.HTTP_400_BAD_REQUEST: "Token missing",
        }
    )
    def redirect_user_into_app(self, request):
        # get token from url
        token = _valid_uuid(request.GET.get('token'))
        if not token:
            return Response({"detail": "Token missing"}, status=status.HTTP_400_BAD_REQUEST)
        link = settings.MAIL_SENDING['PASSWORD_RESET_URL'] + '?token=' + quote(token, safe='')

        return _app_redirect_page(
            link,
            '',
            'Klicke hier um dein Passwort zurückzusetzen. Dieser Link muss auf dem '
            'Smartphone geöffnet werden, auf dem die molo App installiert ist.',
        )


    @swagger_auto_schema(
        operation_description='Reset password',
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'email': openapi.Schema(type=openapi.TYPE_STRING),
            }
        ),
        responses={
            status.HTTP_204_NO_CONTENT: "No Content",
            status.HTTP_404_NOT_FOUND: "Email not found",
        }
    )
    def request_password_reset(self, request):
        email = request.data.get('email')
        if not isinstance(email, str) or not email.strip():
            return Response({"detail": "Email is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Check if the email exists in the database
        if not User.objects.filter(email=email).exists():
            return Response({"detail": "Email not found"}, status=status.HTTP_404_NOT_FOUND)

        # Generate a random password reset token
        token = str(uuid.uuid4())

        # Save the token in the user's database record
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(data={"message": "User not found"}, status=status.HTTP_404_NOT_FOUND)
        
        # Nur den Hash speichern, mit Ausgabezeitpunkt für das Ablaufdatum
        user.password_reset_token = _hash_reset_token(token)
        user.password_reset_requested_at = timezone.now()
        user.save(update_fields=['password_reset_token', 'password_reset_requested_at'])

        try:
            # Send confirmation email
            sender_email = settings.MAIL_SENDING['DEFAULT_FROM_EMAIL']
            receiver_email = email
            subject = 'Passwort zurücksetzen'
            message = '<html><body>'
            message += f'<p>Liebe(r) {escape(user.first_name)} {escape(user.last_name)}, </p>'
            message += '<p>klicke auf den folgenden Link auf deinem Smartphone, um dein Passwort in der molo-App zurückzusetzen:</p>'
            
            server_url = settings.SERVER_URL.rstrip('/')
            link = server_url + settings.MAIL_SENDING['REDIRECT_AFTER_PASSWORD_RESET_REQUEST_PATH'] + '?token=' + token
            message += '<p><a href="' + link +  '">Passwort zurücksetzen</a></p>'
            message += '</body></html>'

            
            send_html_mail(subject, message, receiver_email, sender_email)

        except Exception as e:
            # Keine SMTP-Details an Clients ausgeben
            logger.error("Password reset mail could not be sent: %s", e)
            return Response({"detail": "Mail could not be sent."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_204_NO_CONTENT)
        

# Custom TokenObtainPairSerializer to add user_id to the token
class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    # add user_id to the token
    def validate(self, attrs):
        username = attrs.get("username", None)
        password = attrs.get("password", None)

        if not username or not password:
            raise serializers.ValidationError('Username/email and password are required.')

        # Benutzername hat Vorrang, sonst eindeutige E-Mail-Adresse
        user = User.objects.filter(username=username).first()
        if user is None:
            matches = list(User.objects.filter(email__iexact=username)[:2])
            user = matches[0] if len(matches) == 1 else None
        if user is None:
            # Laufzeit angleichen, damit sich Konten nicht über die Antwortzeit erkennen lassen
            User().set_password(password)
            raise serializers.ValidationError('No active account found with the given credentials')

        if user.check_password(password):
            attrs['username'] = user.username
        else:
            raise serializers.ValidationError('No active account found with the given credentials')
        
        # get the token from the request
        data = super().validate(attrs)
        # get the token from the data
        token = data.get('access')#

        # get the user_id from the token
        try:
            payload = decode_access_token(token)
            user_id = payload.get('user_id')
            if not user_id:
                return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)
        except jwt.ExpiredSignatureError:
            return Response({"detail": "Token has expired."}, status=status.HTTP_401_UNAUTHORIZED)
        except jwt.InvalidTokenError:
            return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)    
       
        # add user_id to the token (SimpleJWT 5.x stores user_id as string, iOS app expects int)
        data['user_id'] = int(user_id)

        return data


# Custom TokenObtainPairView to use the custom serializer
class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    class TokenResponse(serializers.Serializer):
        refresh = serializers.CharField()
        access = serializers.CharField()
        user_id = serializers.IntegerField()

    @swagger_auto_schema(
        operation_description='Obtain a JWT token',
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'username': openapi.Schema(type=openapi.TYPE_STRING),
                'password': openapi.Schema(type=openapi.TYPE_STRING),
            }
        ),
        responses={
            status.HTTP_200_OK: TokenResponse,
            status.HTTP_400_BAD_REQUEST: "Invalid credentials",
        }
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)
    

class CustomTokenVerifyView(TokenVerifyView):
    serializer_class = CheckedTokenVerifySerializer

    class TokenResponse(serializers.Serializer):
        token = serializers.CharField()

    @swagger_auto_schema(
        operation_description='Verify a JWT token',
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'token': openapi.Schema(type=openapi.TYPE_STRING),
            }
        ),
        responses={
            status.HTTP_201_CREATED: "Token verified successfully",
            status.HTTP_400_BAD_REQUEST: "Invalid token",
        }
    )
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
            return Response({"message": "Token verified successfully", "token": request.data["token"]}, status=status.HTTP_201_CREATED)
        except Exception:
            return Response({"message": "Token verification failed"}, status=status.HTTP_400_BAD_REQUEST)

