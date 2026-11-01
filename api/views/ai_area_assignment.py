import secrets
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from content.models import Article, Area
from content.utils.area_creation import get_or_create_area
from django.conf import settings
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
import logging
from datetime import timedelta
from django.utils import timezone

logger = logging.getLogger('ai_area_assignment')


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


class AIArticleAreaAssignmentViewSet(viewsets.ViewSet):
    """
    ViewSet für KI-basierte Gebietszuweisung von Artikeln
    Verarbeitet nur Artikel von Quellen mit auto_detect_area_from_content=True
    """

    @swagger_auto_schema(
        operation_description="Holt einen Artikel ohne Gebietszuweisung (nur letzte 30 Tage, nur published, "
                            "nur von Quellen mit auto_detect_area_from_content=True).",
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
                description="Artikel ohne Gebietszuweisung gefunden",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'title': openapi.Schema(type=openapi.TYPE_STRING),
                        'abstract': openapi.Schema(type=openapi.TYPE_STRING),
                        'current_areas': openapi.Schema(
                            type=openapi.TYPE_ARRAY,
                            items=openapi.Schema(type=openapi.TYPE_INTEGER)
                        ),
                    }
                )
            ),
            401: "Unauthorized - Invalid authorization format",
            403: "Forbidden - Invalid API key",
            404: "Not Found - No articles without area assignment found",
        },
    )
    @action(detail=False, methods=['get'], url_path='get-unassigned')
    def get_unassigned(self, request):
        """
        Holt einen Artikel ohne Gebietszuweisung (nur letzte 30 Tage)
        Nur von Quellen mit auto_detect_area_from_content=True
        """
        # API-Key validieren
        is_valid, error_response = check_ai_api_key(request)
        if not is_valid:
            return error_response

        # Nur Artikel der letzten 30 Tage
        one_month_ago = timezone.now() - timedelta(days=30)

        article = Article.objects.filter(
            source__auto_detect_area_from_content=True,
            area_defined=False,
            published=True,
            date__gte=one_month_ago
        ).select_related('source').order_by('-date').first()

        if not article:
            logger.info("No articles without area assignment found")
            return Response(
                {"message": "No articles without area assignment found"},
                status=status.HTTP_404_NOT_FOUND
            )

        logger.info(f"Retrieved article without area: {article.id} - {article.title}")
        return Response({
            "id": article.id,
            "title": article.title,
            "abstract": article.abstract or "",
            "current_areas": list(article.area.values_list('id', flat=True))
        })

    @swagger_auto_schema(
        operation_description="Holt mehrere Artikel ohne Gebietszuweisung auf einmal (nur letzte 30 Tage, "
                            "nur von Quellen mit auto_detect_area_from_content=True). "
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
                description="Artikel ohne Gebietszuweisung gefunden",
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
    @action(detail=False, methods=['get'], url_path='get-unassigned-batch')
    def get_unassigned_batch(self, request):
        """
        Holt mehrere Artikel ohne Gebietszuweisung auf einmal (nur letzte 30 Tage)
        Nur von Quellen mit auto_detect_area_from_content=True

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
            source__auto_detect_area_from_content=True,
            area_defined=False,
            published=True,
            date__gte=one_month_ago
        ).select_related('source').order_by('-date')[:limit]

        logger.info(f"Retrieved {len(articles)} articles without area assignment (limit: {limit})")
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
        operation_description="Weist einem Artikel Gebiete zu",
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
            required=['article_id'],
            properties={
                'article_id': openapi.Schema(
                    type=openapi.TYPE_INTEGER,
                    description="ID des Artikels"
                ),
                'area_names': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_STRING),
                    description="Liste der Stadt-Namen (werden automatisch erstellt wenn nicht vorhanden). "
                               "Leere Liste [] erlaubt - markiert Artikel als verarbeitet ohne Areas zuzuweisen."
                ),
                'area_ids': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                    description="Liste der Gebiets-IDs (optional, alternativ zu area_names). "
                               "Leere Liste [] erlaubt - markiert Artikel als verarbeitet ohne Areas zuzuweisen."
                ),
            },
        ),
        responses={
            200: openapi.Response(
                description="Gebiete erfolgreich zugewiesen",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                        'article_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'areas_applied': openapi.Schema(
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
            400: "Bad Request - article_id missing or invalid area IDs",
            401: "Unauthorized - Invalid authorization format",
            403: "Forbidden - Invalid API key",
            404: "Not Found - Article not found",
        },
    )
    @action(detail=False, methods=['post'], url_path='submit-areas')
    def submit_areas(self, request):
        """
        Weist einem Artikel Gebiete zu.
        Akzeptiert entweder area_names (Stadt-Namen) oder area_ids.
        Area-Namen werden automatisch mit Geocodierung erstellt wenn nicht vorhanden.
        """
        # API-Key validieren
        is_valid, error_response = check_ai_api_key(request)
        if not is_valid:
            return error_response

        article_id = request.data.get('article_id')
        area_names = request.data.get('area_names', [])
        area_ids = request.data.get('area_ids', [])

        logger.info(f"AI area assignment request for article {article_id}")

        if not article_id:
            logger.error("article_id is required but not provided")
            return Response(
                {"error": "article_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Leere Listen sind erlaubt - markiert Artikel als verarbeitet ohne Areas zuzuweisen

        try:
            article = Article.objects.get(id=article_id)
        except Article.DoesNotExist:
            logger.error(f"Article with id {article_id} not found")
            return Response(
                {"error": "Article not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        valid_areas = []
        areas_info = []

        # Verarbeite area_names (automatische Erstellung)
        if area_names:
            logger.info(f"Processing area_names: {area_names}")
            for area_name in area_names:
                if not area_name or not isinstance(area_name, str):
                    continue

                area, created = get_or_create_area(town_name=area_name.strip(), create_if_not_found=False)

                if not area:
                    # Keine passende Area gefunden - überspringen (keine neuen Areas anlegen)
                    logger.warning(f"Area '{area_name}' nicht in DB gefunden für Artikel {article_id} - übersprungen")
                    continue

                valid_areas.append(area)
                areas_info.append({
                    'id': area.id,
                    'name': area.name,
                    'created': created
                })

        # Verarbeite area_ids (bestehende Areas)
        if area_ids:
            logger.info(f"Processing area_ids: {area_ids}")
            existing_areas = Area.objects.filter(id__in=area_ids)

            if len(existing_areas) != len(area_ids):
                invalid_ids = set(area_ids) - set(existing_areas.values_list('id', flat=True))
                logger.error(f"Invalid area IDs for article {article_id}: {invalid_ids}")
                return Response(
                    {"error": f"Some area IDs are invalid: {invalid_ids}"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            for area in existing_areas:
                valid_areas.append(area)
                areas_info.append({
                    'id': area.id,
                    'name': area.name,
                    'created': False
                })

        # Gebiete zuweisen
        article.area.set(valid_areas)
        article.area_defined = True
        article.save()

        if len(valid_areas) == 0:
            logger.info(f"Article {article_id} marked as processed with no areas (no location found in content)")
            return Response({
                "message": "Article marked as processed - no areas assigned (no location found)",
                "article_id": article.id,
                "areas_applied": []
            })
        else:
            logger.info(f"Successfully assigned {len(valid_areas)} areas to article {article_id}")
            return Response({
                "message": "Areas successfully assigned",
                "article_id": article.id,
                "areas_applied": areas_info
            })

    @swagger_auto_schema(
        operation_description="Weist mehreren Artikeln Gebiete zu",
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
                        required=['article_id'],
                        properties={
                            'article_id': openapi.Schema(
                                type=openapi.TYPE_INTEGER,
                                description="ID des Artikels"
                            ),
                            'area_names': openapi.Schema(
                                type=openapi.TYPE_ARRAY,
                                items=openapi.Schema(type=openapi.TYPE_STRING),
                                description="Liste der Stadt-Namen (werden automatisch erstellt wenn nicht vorhanden)"
                            ),
                            'area_ids': openapi.Schema(
                                type=openapi.TYPE_ARRAY,
                                items=openapi.Schema(type=openapi.TYPE_INTEGER),
                                description="Liste der Gebiets-IDs (optional, alternativ zu area_names)"
                            ),
                        }
                    ),
                    description="Liste von Artikeln mit ihren Gebieten"
                ),
            },
        ),
        responses={
            200: openapi.Response(
                description="Batch-Gebietszuweisung abgeschlossen",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                        'results': openapi.Schema(
                            type=openapi.TYPE_ARRAY,
                            items=openapi.Schema(
                                type=openapi.TYPE_OBJECT,
                                properties={
                                    'article_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                                    'success': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                                    'error': openapi.Schema(type=openapi.TYPE_STRING),
                                    'areas_applied': openapi.Schema(
                                        type=openapi.TYPE_ARRAY,
                                        items=openapi.Schema(
                                            type=openapi.TYPE_OBJECT,
                                            properties={
                                                'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                                                'name': openapi.Schema(type=openapi.TYPE_STRING),
                                                'created': openapi.Schema(type=openapi.TYPE_BOOLEAN, description="War die Area neu erstellt?"),
                                            }
                                        )
                                    ),
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
    @action(detail=False, methods=['post'], url_path='submit-areas-batch')
    def submit_areas_batch(self, request):
        """
        Weist mehreren Artikeln Gebiete zu (Batch-Operation).
        Akzeptiert entweder area_names (Stadt-Namen) oder area_ids pro Artikel.
        Area-Namen werden automatisch mit Geocodierung erstellt wenn nicht vorhanden.
        """
        # API-Key validieren
        is_valid, error_response = check_ai_api_key(request)
        if not is_valid:
            return error_response

        articles_data = request.data.get('articles', [])

        if not articles_data:
            logger.error("articles list is required but not provided")
            return Response(
                {"error": "articles list is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(f"Processing batch area assignment for {len(articles_data)} articles")

        results = []
        for article_data in articles_data:
            article_id = article_data.get('article_id')
            area_names = article_data.get('area_names', [])
            area_ids = article_data.get('area_ids', [])

            if not article_id:
                results.append({
                    'article_id': None,
                    'success': False,
                    'error': 'article_id is required'
                })
                continue

            # Leere Listen sind erlaubt - markiert Artikel als verarbeitet ohne Areas zuzuweisen

            try:
                article = Article.objects.get(id=article_id)

                valid_areas = []
                areas_info = []

                # Verarbeite area_names (automatische Erstellung)
                if area_names:
                    failed = False
                    for area_name in area_names:
                        if not area_name or not isinstance(area_name, str):
                            continue

                        area, created = get_or_create_area(town_name=area_name.strip(), create_if_not_found=False)

                        if not area:
                            # Keine passende Area gefunden - überspringen (keine neuen Areas anlegen)
                            logger.warning(f"Area '{area_name}' nicht in DB gefunden für Artikel {article_id} - übersprungen")
                            continue

                        valid_areas.append(area)
                        areas_info.append({
                            'id': area.id,
                            'name': area.name,
                            'created': created
                        })

                    if failed:
                        continue

                # Verarbeite area_ids (bestehende Areas)
                if area_ids:
                    existing_areas = Area.objects.filter(id__in=area_ids)

                    if len(existing_areas) != len(area_ids):
                        invalid_ids = set(area_ids) - set(existing_areas.values_list('id', flat=True))
                        results.append({
                            'article_id': article_id,
                            'success': False,
                            'error': f'Invalid area IDs: {invalid_ids}'
                        })
                        continue

                    for area in existing_areas:
                        valid_areas.append(area)
                        areas_info.append({
                            'id': area.id,
                            'name': area.name,
                            'created': False
                        })

                # Gebiete zuweisen
                article.area.set(valid_areas)
                article.area_defined = True
                article.save()

                if len(valid_areas) == 0:
                    logger.info(f"Article {article_id} marked as processed with no areas (no location found in content)")
                else:
                    logger.info(f"Successfully assigned {len(valid_areas)} areas to article {article_id}")

                results.append({
                    'article_id': article_id,
                    'success': True,
                    'areas_applied': areas_info
                })

            except Article.DoesNotExist:
                results.append({
                    'article_id': article_id,
                    'success': False,
                    'error': 'Article not found'
                })
                logger.error(f"Article with id {article_id} not found")
            except Exception as e:
                results.append({
                    'article_id': article_id,
                    'success': False,
                    'error': str(e)
                })
                logger.error(f"Error processing article {article_id}: {str(e)}")

        successful = sum(1 for r in results if r.get('success'))
        logger.info(f"Batch area assignment completed: {successful}/{len(results)} successful")

        return Response({
            "message": f"Batch area assignment completed: {successful}/{len(results)} successful",
            "results": results
        })
