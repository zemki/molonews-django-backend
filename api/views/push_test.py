from rest_framework import viewsets, status
from rest_framework.response import Response
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django.conf import settings
from bs4 import BeautifulSoup
import json
import secrets
import firebase_admin
from firebase_admin import credentials, messaging

from content.models import AppUser, Article, EventV4


class PushTestViewSet(viewsets.ViewSet):
    """
    Test-ViewSet für Push-Benachrichtigungen

    Sendet Test-Push-Notifications an Users über Firebase Admin SDK.
    Erfordert den Zugangscode aus settings.PUSH_TEST_ACCESS_CODE; ohne
    gesetzten Code (oder mit ENABLE_PUSH=false) ist der Endpunkt gesperrt.
    """

    # Firebase-Init-Status (Singleton)
    _firebase_initialized = False

    @classmethod
    def initialize_firebase(cls):
        """Initialisiert Firebase Admin SDK einmalig"""
        if cls._firebase_initialized:
            return

        if not hasattr(settings, 'FIREBASE_SERVICE_ACCOUNT_KEY'):
            raise Exception("FIREBASE_SERVICE_ACCOUNT_KEY ist nicht in den Settings konfiguriert!")

        try:
            # Prüfen ob Firebase Admin bereits initialisiert ist
            try:
                firebase_admin.get_app()
            except ValueError:
                # App existiert noch nicht, neue erstellen
                service_account_key = settings.FIREBASE_SERVICE_ACCOUNT_KEY

                if isinstance(service_account_key, str):
                    cred = credentials.Certificate(service_account_key)
                elif isinstance(service_account_key, dict):
                    cred = credentials.Certificate(service_account_key)
                else:
                    raise Exception("FIREBASE_SERVICE_ACCOUNT_KEY muss Dateipfad oder Dictionary sein")

                firebase_admin.initialize_app(cred)

            cls._firebase_initialized = True

        except Exception as e:
            raise Exception(f"Fehler beim Initialisieren von Firebase Admin SDK: {str(e)}")

    @swagger_auto_schema(
        
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['code'],
            properties={
                'code': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description='Zugangscode (erforderlich)'
                ),
                'device_id': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description='Device ID des Users'
                ),
                'firebase_token': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description='Firebase Token direkt (alternativ'
                ),
                'title': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description='Push-Titel (Custom-Modus)',
                    default='Test Nachricht'
                ),
                'message': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description='Push-Nachricht (Custom-Modus)',
                    default='Dies ist eine Test-Nachricht'
                ),
                'article_id': openapi.Schema(
                    type=openapi.TYPE_INTEGER,
                    description='Artikel-ID (Artikel-Modus)'
                ),
                'event_id': openapi.Schema(
                    type=openapi.TYPE_INTEGER,
                    description='Event-ID (Event-Modus)'
                ),
                'data': openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    description='Zusätzliche Daten als JSON (Custom-Modus)'
                )
            }
        ),
        responses={
            200: openapi.Response(
                description='Push erfolgreich gesendet',
            ),
            400: openapi.Response(
                description='Fehlerhafte Anfrage',
            ),
            401: openapi.Response(
                description='Ungültiger Zugangscode',
            ),
            404: openapi.Response(
                description='Ressource nicht gefunden',
            )
        }
    )
    def create(self, request):
        """
        POST /api/v4/admin/test-push/
        """
        # 1. Zugangscode validieren
        access_code = getattr(settings, 'PUSH_TEST_ACCESS_CODE', '')
        if not settings.ENABLE_PUSH or not access_code:
            return Response(
                {"error": "Push-Test ist auf dieser Instanz deaktiviert"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        code = request.data.get('code') or ''
        if not secrets.compare_digest(str(code), access_code):
            return Response(
                {"error": "Ungültiger Zugangscode"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        # 2. Firebase initialisieren
        try:
            self.initialize_firebase()
        except Exception as e:
            return Response(
                {"error": f"Firebase-Initialisierung fehlgeschlagen: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # 3. Firebase Token ermitteln
        device_id = request.data.get('device_id')
        firebase_token = request.data.get('firebase_token')

        if not device_id and not firebase_token:
            return Response(
                {"error": "Entweder device_id oder firebase_token erforderlich"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Token aus DB holen wenn device_id gegeben
        if device_id and not firebase_token:
            try:
                user = AppUser.objects.get(device_id=device_id)
                if not user.firebase_token:
                    return Response(
                        {"error": f"User mit Device ID '{device_id}' hat keinen Firebase Token"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                firebase_token = user.firebase_token
            except AppUser.DoesNotExist:
                return Response(
                    {"error": f"User mit Device ID '{device_id}' nicht gefunden"},
                    status=status.HTTP_404_NOT_FOUND
                )

        # 4. Push-Modus bestimmen und Nachricht vorbereiten
        # Priorität: Custom (höchste) > Event (mittel) > Artikel (niedrigste)
        article_id = request.data.get('article_id')
        event_id = request.data.get('event_id')
        has_custom_params = 'title' in request.data or 'message' in request.data

        try:
            if has_custom_params:
                # Custom-Push-Modus (höchste Priorität)
                return self._send_custom_push(request, firebase_token)
            elif event_id:
                # Event-Push-Modus (mittlere Priorität)
                return self._send_event_push(firebase_token, event_id)
            elif article_id:
                # Artikel-Push-Modus (niedrigste Priorität)
                return self._send_article_push(firebase_token, article_id)
            else:
                # Fallback: Custom mit Defaults
                return self._send_custom_push(request, firebase_token)

        except Exception as e:
            return Response(
                {"error": f"Fehler beim Senden: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _send_custom_push(self, request, firebase_token):
        """Sendet Custom-Push mit beliebigem Text"""
        title = request.data.get('title', 'Test Nachricht')
        message_text = request.data.get('message', 'Dies ist eine Test-Nachricht')
        data_str = request.data.get('data', '{}')

        # Daten parsen
        try:
            if isinstance(data_str, str):
                data = json.loads(data_str) if data_str and data_str != '{}' else {}
            else:
                data = data_str or {}
        except json.JSONDecodeError:
            return Response(
                {"error": "Ungültiges JSON Format für data Parameter"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Message erstellen und senden
        message_obj = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=message_text
            ),
            token=firebase_token,
            data={str(k): str(v) for k, v in data.items()} if data else {}
        )

        try:
            messaging.send(message_obj)
            return Response({"success": True})
        except messaging.UnregisteredError:
            return Response(
                {"error": "Firebase Token ist nicht registriert oder ungültig"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except messaging.SenderIdMismatchError:
            return Response(
                {"error": "Token gehört zu einem anderen Firebase-Projekt"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return Response(
                {"error": f"Firebase-Fehler: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _send_article_push(self, firebase_token, article_id):
        """Sendet Push-Benachrichtigung für einen Artikel"""
        try:
            article = Article.objects.get(id=article_id)
        except Article.DoesNotExist:
            return Response(
                {"error": f"Artikel mit ID {article_id} nicht gefunden"},
                status=status.HTTP_404_NOT_FOUND
            )

        # Nachricht vorbereiten
        title = article.title
        body = BeautifulSoup(article.abstract, "html.parser").text
        data = {"article_id": str(article.id)}

        # Message erstellen
        message_obj = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
                image=article.image_url or None
            ),
            token=firebase_token,
            data={str(k): str(v) for k, v in data.items()}
        )

        # Senden
        try:
            messaging.send(message_obj)
            return Response({"success": True})
        except messaging.UnregisteredError:
            return Response(
                {"error": "Firebase Token ist nicht registriert oder ungültig"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except messaging.SenderIdMismatchError:
            return Response(
                {"error": "Token gehört zu einem anderen Firebase-Projekt"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return Response(
                {"error": f"Firebase-Fehler: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _send_event_push(self, firebase_token, event_id):
        """Sendet Push-Benachrichtigung für ein Event"""
        try:
            event = EventV4.objects.get(id=event_id)
        except EventV4.DoesNotExist:
            return Response(
                {"error": f"Event mit ID {event_id} nicht gefunden"},
                status=status.HTTP_404_NOT_FOUND
            )

        # Nachricht vorbereiten
        title = "Test: Event Reminder"
        body = f"Test-Benachrichtigung für {event.title}"
        data = {"event_id": str(event.id)}

        # Message erstellen
        message_obj = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
                image=event.image_url or None
            ),
            token=firebase_token,
            data={str(k): str(v) for k, v in data.items()}
        )

        # Senden
        try:
            messaging.send(message_obj)
            return Response({"success": True})
        except messaging.UnregisteredError:
            return Response(
                {"error": "Firebase Token ist nicht registriert oder ungültig"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except messaging.SenderIdMismatchError:
            return Response(
                {"error": "Token gehört zu einem anderen Firebase-Projekt"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return Response(
                {"error": f"Firebase-Fehler: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
