import secrets
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from content.models import Article, EventV4, Tag
from django.conf import settings
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
import logging
from datetime import timedelta
from django.utils import timezone

logger = logging.getLogger('ai_tagging')


def is_nighttime():
    """Prüft ob es zwischen 23:00 und 06:00 Uhr ist (lokale Zeit)."""
    current_hour = timezone.localtime().hour
    return current_hour >= 23 or current_hour < 6


def check_ai_api_key(request):
    """
    Validiert den AI API Key aus dem Authorization Header.

    Returns:
        tuple: (is_valid, error_response)
    """
    auth_header = request.headers.get('Authorization', '')

    if not auth_header.startswith('ApiKey '):
        logger.warning(f"Invalid authorization format from IP: {request.META.get('REMOTE_ADDR')}")
        return False, Response(
            {"error": "Invalid authorization format. Expected: 'ApiKey <key>'"},
            status=status.HTTP_401_UNAUTHORIZED
        )

    api_key = auth_header.replace('ApiKey ', '').strip()

    configured_key = settings.AI_TAGGING_API_KEY or ""
    if not configured_key or not secrets.compare_digest(api_key, configured_key):
        logger.warning(f"Invalid API key attempt from IP: {request.META.get('REMOTE_ADDR')}")
        return False, Response(
            {"error": "Invalid API key"},
            status=status.HTTP_403_FORBIDDEN
        )

    return True, None


class AIArticleTaggingViewSet(viewsets.ViewSet):
    """
    ViewSet für KI-basiertes Tagging von Artikeln
    """

    @swagger_auto_schema(
        operation_description="Holt einen ungetaggten Artikel zum Tagging (nur letzte 30 Tage). "
                            "Hinweis: Das 'content' Feld wird nicht zurückgegeben, da bei 99,998% "
                            "aller Artikel dieses Feld leer ist. Der eigentliche Artikeltext steht "
                            "im 'abstract' Feld.",
        manual_parameters=[
            openapi.Parameter(
                "Authorization",
                openapi.IN_HEADER,
                description="API Key im Format: ApiKey <your_api_key>",
                type=openapi.TYPE_STRING,
                required=True
            ),
        ],
        responses={
            200: openapi.Response(
                description="Ungetaggter Artikel gefunden",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'title': openapi.Schema(type=openapi.TYPE_STRING),
                        'abstract': openapi.Schema(type=openapi.TYPE_STRING),
                        'current_tags': openapi.Schema(
                            type=openapi.TYPE_ARRAY,
                            items=openapi.Schema(type=openapi.TYPE_INTEGER)
                        ),
                    }
                )
            ),
            401: "Unauthorized - Invalid authorization format",
            403: "Forbidden - Invalid API key",
            404: "Not Found - No untagged articles found",
        },
    )
    @action(detail=False, methods=['get'], url_path='get-untagged')
    def get_untagged(self, request):
        """
        Holt einen ungetaggten Artikel zum Tagging (nur letzte 30 Tage)
        """
        # API-Key validieren
        is_valid, error_response = check_ai_api_key(request)
        if not is_valid:
            return error_response

        # Nur Artikel der letzten 30 Tage
        one_month_ago = timezone.now() - timedelta(days=30)

        article = Article.objects.filter(
            ai_tagged=False,
            published=True,
            date__gte=one_month_ago
        ).first()

        if not article:
            logger.info("No untagged articles found")
            return Response(
                {"message": "No untagged articles found"},
                status=status.HTTP_404_NOT_FOUND
            )

        logger.info(f"Retrieved untagged article: {article.id} - {article.title}")
        return Response({
            "id": article.id,
            "title": article.title,
            "abstract": article.abstract or "",
            "current_tags": list(article.tags.values_list('id', flat=True))
        })

    @swagger_auto_schema(
        operation_description="Holt mehrere ungetaggte Artikel auf einmal (nur letzte 30 Tage). "
                            "Query Parameter: limit - Anzahl der Artikel (default: 10, max: 100)",
        manual_parameters=[
            openapi.Parameter(
                "Authorization",
                openapi.IN_HEADER,
                description="API Key im Format: ApiKey <your_api_key>",
                type=openapi.TYPE_STRING,
                required=True
            ),
            openapi.Parameter(
                "limit",
                openapi.IN_QUERY,
                description="Anzahl der Artikel (default: 10, max: 100)",
                type=openapi.TYPE_INTEGER,
                required=False
            ),
        ],
        responses={
            200: openapi.Response(
                description="Ungetaggte Artikel gefunden",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'count': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'articles': openapi.Schema(
                            type=openapi.TYPE_ARRAY,
                            items=openapi.Schema(
                                type=openapi.TYPE_OBJECT,
                                properties={
                                    'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                                    'title': openapi.Schema(type=openapi.TYPE_STRING),
                                    'abstract': openapi.Schema(type=openapi.TYPE_STRING),
                                }
                            )
                        ),
                    }
                )
            ),
            401: "Unauthorized - Invalid authorization format",
            403: "Forbidden - Invalid API key",
        },
    )
    @action(detail=False, methods=['get'], url_path='get-untagged-batch')
    def get_untagged_batch(self, request):
        """
        Holt mehrere ungetaggte Artikel auf einmal (nur letzte 30 Tage)

        Query Parameter:
        - limit: Anzahl der Artikel (default: 10, max: 100)
        """
        # API-Key validieren
        is_valid, error_response = check_ai_api_key(request)
        if not is_valid:
            return error_response

        limit = min(int(request.query_params.get('limit', 10)), 100)

        # Nur Artikel der letzten 30 Tage
        one_month_ago = timezone.now() - timedelta(days=30)

        articles = Article.objects.filter(
            ai_tagged=False,
            published=True,
            date__gte=one_month_ago
        )[:limit]

        logger.info(f"Retrieved {len(articles)} untagged articles (limit: {limit})")
        return Response({
            "count": len(articles),
            "articles": [
                {
                    "id": a.id,
                    "title": a.title,
                    "abstract": a.abstract or ""
                }
                for a in articles
            ]
        })

    @swagger_auto_schema(
        operation_description="Speichert die KI-generierten Tags für einen Artikel",
        manual_parameters=[
            openapi.Parameter(
                "Authorization",
                openapi.IN_HEADER,
                description="API Key im Format: ApiKey <your_api_key>",
                type=openapi.TYPE_STRING,
                required=True
            ),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['article_id', 'tag_ids'],
            properties={
                'article_id': openapi.Schema(
                    type=openapi.TYPE_INTEGER,
                    description="ID des zu taggenden Artikels"
                ),
                'tag_ids': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                    description="Liste der Tag-IDs"
                ),
            },
        ),
        responses={
            200: openapi.Response(
                description="Tags erfolgreich gespeichert",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                        'article_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'tags_applied': openapi.Schema(
                            type=openapi.TYPE_ARRAY,
                            items=openapi.Schema(
                                type=openapi.TYPE_OBJECT,
                                properties={
                                    'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                                    'name': openapi.Schema(type=openapi.TYPE_STRING),
                                }
                            )
                        ),
                    }
                )
            ),
            400: "Bad Request - article_id missing or invalid tag IDs",
            401: "Unauthorized - Invalid authorization format",
            403: "Forbidden - Invalid API key",
            404: "Not Found - Article not found",
        },
    )
    @action(detail=False, methods=['post'], url_path='submit-tags')
    def submit_tags(self, request):
        """
        Speichert die KI-generierten Tags für einen Artikel
        """
        # API-Key validieren
        is_valid, error_response = check_ai_api_key(request)
        if not is_valid:
            return error_response

        article_id = request.data.get('article_id')
        tag_ids = request.data.get('tag_ids', [])

        logger.info(f"AI tagging request for article {article_id} with tags {tag_ids}")

        if not article_id:
            logger.error("article_id is required but not provided")
            return Response(
                {"error": "article_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            article = Article.objects.get(id=article_id)
        except Article.DoesNotExist:
            logger.error(f"Article with id {article_id} not found")
            return Response(
                {"error": "Article not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        # Validiere Tags (nur Article-Tags erlauben)
        valid_tags = Tag.objects.filter(
            id__in=tag_ids,
            type='article'
        )

        if len(valid_tags) != len(tag_ids):
            invalid_ids = set(tag_ids) - set(valid_tags.values_list('id', flat=True))
            logger.error(f"Invalid tag IDs for article {article_id}: {invalid_ids}")
            return Response(
                {"error": "Some tag IDs are invalid or not article tags"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Tags zuweisen
        article.tags.set(valid_tags)
        article.ai_tagged = True
        article.save()

        logger.info(f"Successfully tagged article {article_id} with tags {list(valid_tags.values('id', 'name'))}")
        return Response({
            "message": "Tags successfully applied",
            "article_id": article.id,
            "tags_applied": list(valid_tags.values('id', 'name'))
        })

    @swagger_auto_schema(
        operation_description="Speichert die KI-generierten Tags für mehrere Artikel auf einmal",
        manual_parameters=[
            openapi.Parameter(
                "Authorization",
                openapi.IN_HEADER,
                description="API Key im Format: ApiKey <your_api_key>",
                type=openapi.TYPE_STRING,
                required=True
            ),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['articles'],
            properties={
                'articles': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(
                        type=openapi.TYPE_OBJECT,
                        required=['article_id', 'tag_ids'],
                        properties={
                            'article_id': openapi.Schema(
                                type=openapi.TYPE_INTEGER,
                                description="ID des zu taggenden Artikels"
                            ),
                            'tag_ids': openapi.Schema(
                                type=openapi.TYPE_ARRAY,
                                items=openapi.Schema(type=openapi.TYPE_INTEGER),
                                description="Liste der Tag-IDs"
                            ),
                        }
                    ),
                    description="Liste von Artikeln mit ihren Tags"
                ),
            },
        ),
        responses={
            200: openapi.Response(
                description="Batch-Tagging abgeschlossen",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                        'total': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'successful': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'failed': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'results': openapi.Schema(
                            type=openapi.TYPE_ARRAY,
                            items=openapi.Schema(
                                type=openapi.TYPE_OBJECT,
                                properties={
                                    'article_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                                    'success': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                                    'tags_applied': openapi.Schema(
                                        type=openapi.TYPE_ARRAY,
                                        items=openapi.Schema(
                                            type=openapi.TYPE_OBJECT,
                                            properties={
                                                'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                                                'name': openapi.Schema(type=openapi.TYPE_STRING),
                                            }
                                        )
                                    ),
                                    'error': openapi.Schema(type=openapi.TYPE_STRING),
                                }
                            )
                        ),
                    }
                )
            ),
            400: "Bad Request - Invalid request format",
            401: "Unauthorized - Invalid authorization format",
            403: "Forbidden - Invalid API key",
        },
    )
    @action(detail=False, methods=['post'], url_path='submit-tags-batch')
    def submit_tags_batch(self, request):
        """
        Speichert die KI-generierten Tags für mehrere Artikel auf einmal
        """
        # API-Key validieren
        is_valid, error_response = check_ai_api_key(request)
        if not is_valid:
            return error_response

        articles_data = request.data.get('articles', [])

        if not articles_data or not isinstance(articles_data, list):
            logger.error("Invalid request: 'articles' must be a non-empty list")
            return Response(
                {"error": "'articles' must be a non-empty list"},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(f"AI batch tagging request for {len(articles_data)} articles")

        results = []
        successful = 0
        failed = 0

        for item in articles_data:
            article_id = item.get('article_id')
            tag_ids = item.get('tag_ids', [])

            result = {
                'article_id': article_id,
                'success': False,
            }

            # Validierung
            if not article_id:
                result['error'] = 'article_id is required'
                failed += 1
                results.append(result)
                logger.warning(f"Batch item missing article_id: {item}")
                continue

            try:
                article = Article.objects.get(id=article_id)
            except Article.DoesNotExist:
                result['error'] = f'Article {article_id} not found'
                failed += 1
                results.append(result)
                logger.error(f"Article {article_id} not found in batch request")
                continue

            # Validiere Tags (nur Article-Tags erlauben)
            valid_tags = Tag.objects.filter(
                id__in=tag_ids,
                type='article'
            )

            if len(valid_tags) != len(tag_ids):
                invalid_ids = set(tag_ids) - set(valid_tags.values_list('id', flat=True))
                result['error'] = f'Invalid tag IDs: {list(invalid_ids)}'
                failed += 1
                results.append(result)
                logger.error(f"Invalid tag IDs for article {article_id}: {invalid_ids}")
                continue

            # Tags zuweisen
            try:
                article.tags.set(valid_tags)
                article.ai_tagged = True
                article.save()

                result['success'] = True
                result['tags_applied'] = list(valid_tags.values('id', 'name'))
                successful += 1
                logger.info(f"Successfully tagged article {article_id} with tags {list(valid_tags.values('id', 'name'))}")
            except Exception as e:
                result['error'] = f'Error saving tags: {str(e)}'
                failed += 1
                logger.error(f"Error tagging article {article_id}: {str(e)}")

            results.append(result)

        logger.info(f"Batch tagging completed: {successful} successful, {failed} failed out of {len(articles_data)} total")

        return Response({
            "message": "Batch tagging completed",
            "total": len(articles_data),
            "successful": successful,
            "failed": failed,
            "results": results
        })


class AIEventTaggingViewSet(viewsets.ViewSet):
    """
    ViewSet für KI-basiertes Tagging von Events
    """

    @swagger_auto_schema(
        operation_description="Holt ein ungetaggtes Event zum Tagging. "
                            "Events von Sources mit type='local' werden ausgeschlossen.",
        manual_parameters=[
            openapi.Parameter(
                "Authorization",
                openapi.IN_HEADER,
                description="API Key im Format: ApiKey <your_api_key>",
                type=openapi.TYPE_STRING,
                required=True
            ),
        ],
        responses={
            200: openapi.Response(
                description="Ungetaggtes Event gefunden",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'title': openapi.Schema(type=openapi.TYPE_STRING),
                        'content': openapi.Schema(type=openapi.TYPE_STRING),
                        'start_date': openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_DATETIME),
                        'event_location': openapi.Schema(type=openapi.TYPE_STRING),
                        'current_tags': openapi.Schema(
                            type=openapi.TYPE_ARRAY,
                            items=openapi.Schema(type=openapi.TYPE_INTEGER)
                        ),
                    }
                )
            ),
            401: "Unauthorized - Invalid authorization format",
            403: "Forbidden - Invalid API key",
            404: "Not Found - No untagged events found",
        },
    )
    @action(detail=False, methods=['get'], url_path='get-untagged')
    def get_untagged(self, request):
        """
        Holt ein ungetaggtes Event zum Tagging
        """
        # API-Key validieren
        is_valid, error_response = check_ai_api_key(request)
        if not is_valid:
            return error_response

        qs = EventV4.objects.filter(
            ai_tagged=False,
            published=True,
        ).exclude(source__type='local')
        if not is_nighttime():
            qs = qs.filter(is_past=False)
        # Zuerst die Events, die als naechstes stattfinden (vorher: ohne Sortierung,
        # also Import-Reihenfolge - grosse Importe mit Terminen in Monaten blockierten
        # zeitnahe Events)
        event = qs.order_by('start_date', 'id').first()

        if not event:
            logger.info("No untagged events found")
            return Response(
                {"message": "No untagged events found"},
                status=status.HTTP_404_NOT_FOUND
            )

        logger.info(f"Retrieved untagged event: {event.id} - {event.title}")
        return Response({
            "id": event.id,
            "title": event.title,
            "content": event.content,
            "start_date": event.start_date,
            "event_location": event.event_location or "",
            "current_tags": list(event.tags.values_list('id', flat=True))
        })

    @swagger_auto_schema(
        operation_description="Holt mehrere ungetaggte Events auf einmal. "
                            "Query Parameter: limit - Anzahl der Events (default: 10, max: 100). "
                            "Events von Sources mit type='local' werden ausgeschlossen.",
        manual_parameters=[
            openapi.Parameter(
                "Authorization",
                openapi.IN_HEADER,
                description="API Key im Format: ApiKey <your_api_key>",
                type=openapi.TYPE_STRING,
                required=True
            ),
            openapi.Parameter(
                "limit",
                openapi.IN_QUERY,
                description="Anzahl der Events (default: 10, max: 100)",
                type=openapi.TYPE_INTEGER,
                required=False
            ),
        ],
        responses={
            200: openapi.Response(
                description="Ungetaggte Events gefunden",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'count': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'events': openapi.Schema(
                            type=openapi.TYPE_ARRAY,
                            items=openapi.Schema(
                                type=openapi.TYPE_OBJECT,
                                properties={
                                    'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                                    'title': openapi.Schema(type=openapi.TYPE_STRING),
                                    'content': openapi.Schema(type=openapi.TYPE_STRING),
                                    'start_date': openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_DATETIME),
                                    'event_location': openapi.Schema(type=openapi.TYPE_STRING),
                                }
                            )
                        ),
                    }
                )
            ),
            401: "Unauthorized - Invalid authorization format",
            403: "Forbidden - Invalid API key",
        },
    )
    @action(detail=False, methods=['get'], url_path='get-untagged-batch')
    def get_untagged_batch(self, request):
        """
        Holt mehrere ungetaggte Events auf einmal

        Query Parameter:
        - limit: Anzahl der Events (default: 10, max: 100)
        """
        # API-Key validieren
        is_valid, error_response = check_ai_api_key(request)
        if not is_valid:
            return error_response

        limit = min(int(request.query_params.get('limit', 10)), 100)

        qs = EventV4.objects.filter(
            ai_tagged=False,
            published=True,
        ).exclude(source__type='local')
        if not is_nighttime():
            qs = qs.filter(is_past=False)
        # Zuerst die Events, die als naechstes stattfinden (siehe get_untagged)
        events = qs.order_by('start_date', 'id')[:limit]

        logger.info(f"Retrieved {len(events)} untagged events (limit: {limit})")
        return Response({
            "count": len(events),
            "events": [
                {
                    "id": e.id,
                    "title": e.title,
                    "content": e.content,
                    "start_date": e.start_date,
                    "event_location": e.event_location or ""
                }
                for e in events
            ]
        })

    @swagger_auto_schema(
        operation_description="Speichert die KI-generierten Tags für ein Event",
        manual_parameters=[
            openapi.Parameter(
                "Authorization",
                openapi.IN_HEADER,
                description="API Key im Format: ApiKey <your_api_key>",
                type=openapi.TYPE_STRING,
                required=True
            ),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['event_id', 'tag_ids'],
            properties={
                'event_id': openapi.Schema(
                    type=openapi.TYPE_INTEGER,
                    description="ID des zu taggenden Events"
                ),
                'tag_ids': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                    description="Liste der Tag-IDs"
                ),
            },
        ),
        responses={
            200: openapi.Response(
                description="Tags erfolgreich gespeichert",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                        'event_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'tags_applied': openapi.Schema(
                            type=openapi.TYPE_ARRAY,
                            items=openapi.Schema(
                                type=openapi.TYPE_OBJECT,
                                properties={
                                    'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                                    'name': openapi.Schema(type=openapi.TYPE_STRING),
                                }
                            )
                        ),
                    }
                )
            ),
            400: "Bad Request - event_id missing or invalid tag IDs",
            401: "Unauthorized - Invalid authorization format",
            403: "Forbidden - Invalid API key",
            404: "Not Found - Event not found",
        },
    )
    @action(detail=False, methods=['post'], url_path='submit-tags')
    def submit_tags(self, request):
        """
        Speichert die KI-generierten Tags für ein Event
        """
        # API-Key validieren
        is_valid, error_response = check_ai_api_key(request)
        if not is_valid:
            return error_response

        event_id = request.data.get('event_id')
        tag_ids = request.data.get('tag_ids', [])

        logger.info(f"AI tagging request for event {event_id} with tags {tag_ids}")

        if not event_id:
            logger.error("event_id is required but not provided")
            return Response(
                {"error": "event_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            event = EventV4.objects.get(id=event_id)
        except EventV4.DoesNotExist:
            logger.error(f"Event with id {event_id} not found")
            return Response(
                {"error": "Event not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        # Validiere Tags (nur Event-Tags erlauben)
        valid_tags = Tag.objects.filter(
            id__in=tag_ids,
            type='event'
        )

        if len(valid_tags) != len(tag_ids):
            invalid_ids = set(tag_ids) - set(valid_tags.values_list('id', flat=True))
            logger.error(f"Invalid tag IDs for event {event_id}: {invalid_ids}")
            return Response(
                {"error": "Some tag IDs are invalid or not event tags"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Tags zuweisen (per Regel vergebene Tags bleiben erhalten)
        event.tags.set(list(valid_tags) + list(event.tags.filter(assigned_by_rule=True)))
        event.ai_tagged = True
        event.save()

        logger.info(f"Successfully tagged event {event_id} with tags {list(valid_tags.values('id', 'name'))}")
        return Response({
            "message": "Tags successfully applied",
            "event_id": event.id,
            "tags_applied": list(valid_tags.values('id', 'name'))
        })

    @swagger_auto_schema(
        operation_description="Speichert die KI-generierten Tags für mehrere Events auf einmal",
        manual_parameters=[
            openapi.Parameter(
                "Authorization",
                openapi.IN_HEADER,
                description="API Key im Format: ApiKey <your_api_key>",
                type=openapi.TYPE_STRING,
                required=True
            ),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['events'],
            properties={
                'events': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(
                        type=openapi.TYPE_OBJECT,
                        required=['event_id', 'tag_ids'],
                        properties={
                            'event_id': openapi.Schema(
                                type=openapi.TYPE_INTEGER,
                                description="ID des zu taggenden Events"
                            ),
                            'tag_ids': openapi.Schema(
                                type=openapi.TYPE_ARRAY,
                                items=openapi.Schema(type=openapi.TYPE_INTEGER),
                                description="Liste der Tag-IDs"
                            ),
                        }
                    ),
                    description="Liste von Events mit ihren Tags"
                ),
            },
        ),
        responses={
            200: openapi.Response(
                description="Batch-Tagging abgeschlossen",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                        'total': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'successful': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'failed': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'results': openapi.Schema(
                            type=openapi.TYPE_ARRAY,
                            items=openapi.Schema(
                                type=openapi.TYPE_OBJECT,
                                properties={
                                    'event_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                                    'success': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                                    'tags_applied': openapi.Schema(
                                        type=openapi.TYPE_ARRAY,
                                        items=openapi.Schema(
                                            type=openapi.TYPE_OBJECT,
                                            properties={
                                                'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                                                'name': openapi.Schema(type=openapi.TYPE_STRING),
                                            }
                                        )
                                    ),
                                    'error': openapi.Schema(type=openapi.TYPE_STRING),
                                }
                            )
                        ),
                    }
                )
            ),
            400: "Bad Request - Invalid request format",
            401: "Unauthorized - Invalid authorization format",
            403: "Forbidden - Invalid API key",
        },
    )
    @action(detail=False, methods=['post'], url_path='submit-tags-batch')
    def submit_tags_batch(self, request):
        """
        Speichert die KI-generierten Tags für mehrere Events auf einmal
        """
        # API-Key validieren
        is_valid, error_response = check_ai_api_key(request)
        if not is_valid:
            return error_response

        events_data = request.data.get('events', [])

        if not events_data or not isinstance(events_data, list):
            logger.error("Invalid request: 'events' must be a non-empty list")
            return Response(
                {"error": "'events' must be a non-empty list"},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(f"AI batch tagging request for {len(events_data)} events")

        results = []
        successful = 0
        failed = 0

        for item in events_data:
            event_id = item.get('event_id')
            tag_ids = item.get('tag_ids', [])

            result = {
                'event_id': event_id,
                'success': False,
            }

            # Validierung
            if not event_id:
                result['error'] = 'event_id is required'
                failed += 1
                results.append(result)
                logger.warning(f"Batch item missing event_id: {item}")
                continue

            try:
                event = EventV4.objects.get(id=event_id)
            except EventV4.DoesNotExist:
                result['error'] = f'Event {event_id} not found'
                failed += 1
                results.append(result)
                logger.error(f"Event {event_id} not found in batch request")
                continue

            # Validiere Tags (nur Event-Tags erlauben)
            valid_tags = Tag.objects.filter(
                id__in=tag_ids,
                type='event'
            )

            if len(valid_tags) != len(tag_ids):
                invalid_ids = set(tag_ids) - set(valid_tags.values_list('id', flat=True))
                result['error'] = f'Invalid tag IDs: {list(invalid_ids)}'
                failed += 1
                results.append(result)
                logger.error(f"Invalid tag IDs for event {event_id}: {invalid_ids}")
                continue

            # Tags zuweisen
            try:
                # Per Regel vergebene Tags bleiben erhalten
                event.tags.set(list(valid_tags) + list(event.tags.filter(assigned_by_rule=True)))
                event.ai_tagged = True
                event.save()

                result['success'] = True
                result['tags_applied'] = list(valid_tags.values('id', 'name'))
                successful += 1
                logger.info(f"Successfully tagged event {event_id} with tags {list(valid_tags.values('id', 'name'))}")
            except Exception as e:
                result['error'] = f'Error saving tags: {str(e)}'
                failed += 1
                logger.error(f"Error tagging event {event_id}: {str(e)}")

            results.append(result)

        logger.info(f"Batch tagging completed: {successful} successful, {failed} failed out of {len(events_data)} total")

        return Response({
            "message": "Batch tagging completed",
            "total": len(events_data),
            "successful": successful,
            "failed": failed,
            "results": results
        })
