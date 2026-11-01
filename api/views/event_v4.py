from django.db.models import Count, Q, Exists, OuterRef, Subquery
from datetime import datetime, timedelta, time
import uuid
from django.utils.timezone import localtime
from rest_framework import viewsets, serializers, filters
from rest_framework.viewsets import GenericViewSet
from drf_yasg.utils import swagger_auto_schema, swagger_serializer_method
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from logging import getLogger
from django.shortcuts import get_object_or_404
from django.conf import settings
import jwt
from api.views.util import can_view_unpublished, decode_access_token, related_source
from content.permissions import can_publish, has_content_role
from content.utils.mail import send_html_mail
from content.utils.urls import is_safe_link
import pytz
from django.utils import timezone
from rest_framework.pagination import LimitOffsetPagination
from .util import (
    choices_parameter,
    integer_parameter,
    string_parameter,
    boolean_parameter,
    header_string_parameter,
    isodate_parameter,
)
from content.models import AppUser, EventV4, Tag, Organization, Event_Occurrence, User, TagCategories, Genre, FilmLanguage
from content.utils.area_creation import get_or_create_area
from content.choices import ORGANIZATION_TYPE_CHOICES
from .util import bad_request
from drf_yasg import openapi
from .article_event_shared import (
    BaseViewSet,
    AppUserBaseViewSet,
    SourceActiveFilter,
    get_source_default_image_url,
)
from django.utils.timezone import now
import re 
from django.db import models, transaction
from api.views.area import AreaSerializer
import copy
from threading import Thread

logger = getLogger("molonews")


# ==========================================
# RAG Search Helper Functions with Caching
# ==========================================
import hashlib
from django.core.cache import caches


def get_rag_cache_key(search_query, device_id, user_areas, date_start, date_end, base_filters, parsed_params=None, category_id=None):
    """
    Erstellt einen eindeutigen Cache-Key für RAG-Suche

    Key basiert auf:
    - Suchquery
    - User (device_id)
    - User Areas (falls keine Areas in Query)
    - Query Areas (falls in Query extrahiert) - NEU!
    - Datumsfilter (aus Query extrahiert) - NEU!
    - Content Keywords (aus Query extrahiert) - NEU!
    - Zusätzliche Filter (organization, town, subtags)

    Args:
        search_query: Der Suchstring
        device_id: Device ID des Users
        user_areas: Liste der User Area IDs
        date_start: Start-Datum Filter (aus Request)
        date_end: End-Datum Filter (aus Request)
        base_filters: Dict mit zusätzlichen Filtern
        parsed_params: Dict mit geparsten Parametern aus der Query (areas, dates, keywords)
        category_id: Optional - Kategorie-ID Filter (aus Request)

    Returns:
        String: Cache-Key z.B. "rag_search:abc123def456"
    """
    # REQUEST-PARAMETER HABEN PRIORITÄT!
    # Areas: Request > LLM-Extraktion > User-Default
    if parsed_params and parsed_params.get('areas'):
        areas_str = ','.join(sorted(parsed_params['areas']))
    else:
        areas_str = ','.join(map(str, sorted(user_areas)))

    # Dates: Request-Parameter haben IMMER Priorität über LLM-Extraktion
    # WICHTIG: Konvertiere datetime zu String für konsistenten Hash
    if date_start:
        date_start_str = date_start.strftime('%Y-%m-%d %H:%M:%S') if hasattr(date_start, 'strftime') else str(date_start)
    elif parsed_params and parsed_params.get('start_date'):
        date_start_str = str(parsed_params['start_date'])
    else:
        date_start_str = ''
    
    if date_end:
        date_end_str = date_end.strftime('%Y-%m-%d %H:%M:%S') if hasattr(date_end, 'strftime') else str(date_end)
    elif parsed_params and parsed_params.get('end_date'):
        date_end_str = str(parsed_params['end_date'])
    else:
        date_end_str = ''
    
    date_str = f"{date_start_str}|{date_end_str}"

    # Content Keywords (nur aus LLM-Extraktion)
    keywords_list = parsed_params.get('content_keywords', []) if parsed_params else []
    keywords_str = ','.join(sorted(keywords_list)) if keywords_list else ""

    # Base-Filters als String
    filters_str = str(sorted(base_filters.items())) if base_filters else ""

    # Kombiniere alles
    key_parts = [
        search_query.lower().strip(),
        device_id or 'anonymous',
        areas_str,
        date_str,
        keywords_str,
        filters_str,
        str(category_id) if category_id else ''
    ]

    # Hash erstellen
    key_string = '|'.join(key_parts)
    key_hash = hashlib.md5(key_string.encode('utf-8')).hexdigest()

    return f"rag_search:{key_hash}"


def _get_german_word_variants(word):
    """
    Generiert Singular/Plural-Varianten eines deutschen Wortes.
    Behandelt Umlaut-Pluralformen (Flohmarkt↔Flohmärkte) automatisch.
    """
    variants = {word.lower()}
    w = word.lower()
    for base, uml in [('a', 'ä'), ('o', 'ö'), ('u', 'ü')]:
        if uml in w:
            variants.add(w.replace(uml, base))
        if base in w:
            variants.add(w.replace(base, uml))
    for v in list(variants):
        if v.endswith('en'):
            variants.add(v[:-2])
        elif v.endswith('er'):
            variants.add(v[:-2])
        elif v.endswith('e'):
            variants.add(v[:-1])
        else:
            variants.add(v + 'e')
    return list(variants)


def build_fulltext_filter(search):
    """
    Baut einen Q-Filter für Volltextsuche.
    Jedes Wort wird mit deutschen Singular/Plural/Umlaut-Varianten gesucht (OR),
    alle Wörter müssen matchen (AND).
    So findet 'Flohmärkte' auch Events mit 'Flohmarkt', 'Bücherflohmarkt' etc.
    """
    words = search.strip().split()

    combined = Q()
    for word in words:
        word_filter = Q()
        for variant in _get_german_word_variants(word):
            word_filter |= (
                Q(title__icontains=variant) |
                Q(content__icontains=variant) |
                Q(tags__name__icontains=variant) |
                Q(event_location__icontains=variant) |
                Q(subtags__name__icontains=variant) |
                Q(source__name__icontains=variant)
            )
        combined &= word_filter

    return combined


def build_occurrence_querysets(date_start, date_end):
    """
    Baut (occurrence_qs, occurrence_subquery) fuer einen Zeitraum.

    Geliefert werden alle Occurrences, die den Zeitraum ueberlappen
    (start <= Zeitraum-Ende UND end >= Zeitraum-Start), sortiert nach Startzeit.
    """
    occurrence_qs = Event_Occurrence.objects.all()
    occurrence_subquery = Event_Occurrence.objects.filter(event=OuterRef('pk'))

    if date_start and date_end:
        window = Q(start_datetime__lte=date_end) & Q(end_datetime__gte=date_start)
        occurrence_qs = occurrence_qs.filter(window)
        occurrence_subquery = occurrence_subquery.filter(window)
    elif date_start:
        occurrence_qs = occurrence_qs.filter(end_datetime__gte=date_start)
        occurrence_subquery = occurrence_subquery.filter(end_datetime__gte=date_start)
    elif date_end:
        occurrence_qs = occurrence_qs.filter(start_datetime__lte=date_end)
        occurrence_subquery = occurrence_subquery.filter(start_datetime__lte=date_end)

    return occurrence_qs.order_by('start_datetime'), occurrence_subquery


def narrow_range_to_rag_window(date_start, date_end, rag_window):
    """
    Verschneidet den Request-Zeitraum mit dem Zeitraum, den die RAG-Suche aus der
    Anfrage gelesen hat ("am Wochenende", "morgen").

    Ohne das sucht RAG im Wochenende, der View zeigt und sortiert aber die
    Occurrences des gesamten Request-Zeitraums - eine Wochenend-Suche beginnt
    dann mit den heutigen Terminen derselben Events.

    Ueberlappen sich beide Zeitraeume nicht, bleibt der Request-Zeitraum stehen.
    """
    if not rag_window:
        return date_start, date_end

    rag_start = rag_window.get('start')
    rag_end = rag_window.get('end')

    new_start = max(date_start, rag_start) if (date_start and rag_start) else (date_start or rag_start)
    new_end = min(date_end, rag_end) if (date_end and rag_end) else (date_end or rag_end)

    if new_start and new_end and new_start > new_end:
        return date_start, date_end

    return new_start, new_end


def perform_rag_search_with_cache(search_query, request, logger,
                                   user_areas, date_start, date_end, base_filters=None, category_id=None, limit=None,
                                   date_window_out=None):
    """
    Führt RAG-basierte Suche durch mit Caching

    Zwei Cache-Ebenen:
    1. Parse-Cache: LLM-Parsing wird einmal pro Query gecached (gleich für alle Kategorien)
    2. Ergebnis-Cache: Endergebnis pro Query+Category gecached

    Args:
        search_query: Der Suchstring
        request: Django Request Object
        logger: Logger instance
        user_areas: Liste der User Area IDs
        date_start: Start-Datum Filter (aus Request)
        date_end: End-Datum Filter (aus Request)
        base_filters: Dict mit zusätzlichen Filtern (organization, town, etc.)
        category_id: Optional - Kategorie-ID Filter (aus Request)
        date_window_out: Optional - Dict, in das der aus der Anfrage gelesene
            Zeitraum als {'start': dt, 'end': dt} geschrieben wird (nur wenn die
            Anfrage selbst einen Zeitraum nennt). Der View braucht ihn, um
            Occurrence-Filter und Sortierung auf denselben Zeitraum zu stellen.

    Returns:
        List of Event IDs oder None bei Fehler
    """
    rag_window = None
    device_id = request.headers.get('X-Device-ID')

    try:
        from rag.rag_query import NaturalLanguageEventQuery, EventSearchService

        cache_enabled = getattr(settings, 'RAG_CACHE_ENABLED', True)
        cache_timeout = getattr(settings, 'RAG_CACHE_TIMEOUT', 120)

        # Schritt 1: Ergebnis-Cache prüfen (inkl. category_id)
        # Bei "future events by category" kommen 4 Requests nacheinander (je Kategorie).
        # Der Ergebnis-Cache ist pro category_id unterschiedlich.
        result_cache_key = get_rag_cache_key(
            search_query, device_id, user_areas, date_start, date_end,
            base_filters, None, category_id
        )

        if cache_enabled:
            try:
                cache = caches['rag_search']
                cached_result = cache.get(result_cache_key)
                if cached_result is not None:
                    if isinstance(cached_result, dict):
                        cached_ids = cached_result.get('ids') or []
                        rag_window = cached_result.get('window')
                    else:
                        # Alt-Format im Cache (nur IDs) - laeuft nach Cache-Timeout aus
                        cached_ids = cached_result
                    if date_window_out is not None and rag_window:
                        date_window_out.update(rag_window)
                    logger.info(f"RAG Search: Cache HIT - {len(cached_ids)} events")
                    return cached_ids
            except Exception as e:
                logger.warning(f"RAG Search: Cache read error - {e}")

        # Schritt 2: LLM-Parse (mit eigenem Parse-Cache)
        # Der Parse ist für alle 4 Kategorie-Requests identisch → nur 1 LLM-Call.
        parse_cache_key = f"rag_parse:{hashlib.md5(search_query.lower().strip().encode()).hexdigest()}"
        params = None

        if cache_enabled:
            try:
                cache = caches['rag_search']
                params = cache.get(parse_cache_key)
                if params is not None:
                    logger.info("RAG Search: Parse-Cache HIT (kein LLM-Call)")
            except Exception as e:
                logger.warning(f"RAG Search: Parse-Cache read error - {e}")

        if params is None:
            # LLM-Call nötig (erster Request oder Parse-Cache abgelaufen)
            query_parser = NaturalLanguageEventQuery()
            params, _ = query_parser.parse_natural_query(search_query, debug=False)
            logger.info(f"RAG Search: Query geparst - Areas: {params.to_dict().get('areas')}, Keywords: {params.to_dict().get('content_keywords')}, Intent: {params.to_dict().get('query_intent')}")

            # Parse im Cache speichern (gleicher Timeout wie Ergebnis-Cache)
            if cache_enabled:
                try:
                    cache = caches['rag_search']
                    cache.set(parse_cache_key, params, timeout=cache_timeout)
                    logger.info(f"RAG Search: Parse-Cache gespeichert für {cache_timeout}s")
                except Exception as e:
                    logger.warning(f"RAG Search: Parse-Cache write error - {e}")

        # Zeitraum, den die Anfrage selbst nennt ("am Wochenende"). Muss VOR dem
        # search()-Aufruf gelesen werden - dort werden fehlende Datumsangaben mit
        # Defaults (heute .. +6 Monate) aufgefuellt, die kein Wunsch des Users sind.
        if params.start_date and params.end_date:
            rag_window = {'start': params.start_date, 'end': params.end_date}
            if date_window_out is not None:
                date_window_out.update(rag_window)
            logger.info(f"RAG Search: Zeitraum aus der Anfrage: {params.start_date} bis {params.end_date}")

        # Schritt 3: Suche durchführen MIT bereits geparsten Params (kein zweiter LLM-Call)
        service = EventSearchService()
        # Organisationsfilter aus dem Request (?organization=1,2) schon in die RAG-Suche
        # geben, sonst fuellen fremde Organisationen das Ergebnis-Limit und der
        # nachgelagerte Filter im View laesst nur einen Rest uebrig.
        organization_ids = (base_filters or {}).get('organization') or None
        rag_result = service.search(
            natural_query=search_query,
            device_id=device_id,
            debug=getattr(settings, 'RAG_SEARCH_DEBUG', False),
            limit=limit,
            category_id=category_id,
            parsed_params=params,
            organization_ids=organization_ids
        )

        # Event-IDs extrahieren
        if 'events' in rag_result and rag_result['events']:
            result = [event['id'] for event in rag_result['events']]
            logger.info(f"RAG Search: Found {len(result)} relevant events")
        else:
            result = []
            logger.info("RAG Search: No events found")

        # Ergebnis im Cache speichern
        if cache_enabled:
            try:
                cache = caches['rag_search']
                cache.set(result_cache_key, {'ids': result, 'window': rag_window}, timeout=cache_timeout)
                logger.info(f"RAG Search: Cached result for {cache_timeout}s")
            except Exception as e:
                logger.warning(f"RAG Search: Cache write error - {e}")

        return result

    except ImportError as e:
        logger.error(f"RAG Search: RAG module not available - {e}")
        return None
    except Exception as e:
        logger.error(f"RAG Search failed: {e}", exc_info=True)
        return None


def update_event_request_counts_async(event_ids):
    """
    Asynchronously update request_count for given event IDs.
    Runs in a separate thread after response is sent to client.
    """
    def _update():
        try:
            from django.db import connections
            with connections['default'].cursor() as cursor:
                placeholders = ','.join(['%s'] * len(event_ids))
                cursor.execute(
                    f"UPDATE content_eventv4 SET request_count = request_count + 1 "
                    f"WHERE id IN ({placeholders})",
                    event_ids
                )
            connections['default'].close()
        except Exception as e:
            logger.warning(f"Async request_count update failed: {e}")

    Thread(target=_update, daemon=True).start()

SORT_START_ATTR = "_sort_start"
SORT_ONGOING_ATTR = "_sort_ongoing"


def occurrence_start_for_output(occ, now_dt):
    """
    Liefert (Anzeige-Start, Sortier-Start) einer Occurrence für die API-Ausgabe.

    Anzeige-Start ist immer der echte Beginn der Occurrence - eine bereits
    laufende Veranstaltung soll ihre tatsächliche Anfangszeit zeigen und nicht
    den Abrufzeitpunkt (frueher lieferte die Clamp-Logik hier now_dt, dadurch
    stand bei laufenden Terminen die Uhrzeit des Seitenaufrufs im Frontend).

    Für die Sortierung wird bei laufenden Occurrences weiterhin "jetzt"
    verwendet, damit die bisherige Reihenfolge der Listen unverändert bleibt.
    """
    occ_start = occ.start_datetime
    occ_end = occ.end_datetime
    if timezone.is_naive(occ_start):
        occ_start = timezone.make_aware(occ_start, timezone=pytz.UTC)
    if timezone.is_naive(occ_end):
        occ_end = timezone.make_aware(occ_end, timezone=pytz.UTC)
    if occ_start <= now_dt <= occ_end:
        return occ_start, now_dt
    return occ_start, occ_start


def set_event_start_dates(event, occ, now_dt, window_start=None):
    """
    Setzt Anzeige-Start und Sortier-Start eines Events aus einer Occurrence.

    Mit window_start (Beginn des gefragten Zeitraums) werden Termine, die schon
    davor begonnen haben, als laufend markiert. Sie stehen dann hinter den
    Terminen, die tatsächlich im gefragten Zeitraum beginnen - sonst eröffnet
    eine Wochenend-Suche mit der Dauerausstellung, die seit Freitag läuft.
    """
    display_start, sort_start = occurrence_start_for_output(occ, now_dt)
    event.start_date = display_start
    setattr(event, SORT_START_ATTR, sort_start)
    setattr(event, SORT_ONGOING_ATTR, bool(window_start and display_start < window_start))


def event_sort_key(event):
    """Sortierschlüssel: laufende Termine zuletzt, sonst nach Sortier-Start."""
    return (
        getattr(event, SORT_ONGOING_ATTR, False),
        getattr(event, SORT_START_ATTR, None) or event.start_date,
    )


BASE_LIST_FIELDS = (
    "id",
    "type",
    "language",
    "translations",
    "title",
    "content",
    "image_url",
    "image_source",
    "start_date",
    "end_date",
    "area",
    "request_count",
)

EVENT_LIST_FIELDS = BASE_LIST_FIELDS + (
    "created_at",
    "moddate",
    "event_location",
    "link",
    "tags",
    "organization",
    "organization_type",
    "organization_name",
    "organization_image",
    "genres",
    "film_languages",
    "subtitle_language",
)
EVENT_RETRIEVE_FIELDS = EVENT_LIST_FIELDS + (
    "source",
    "content",
)

FILTER_BASE_FIELDS = [
    "organization",
    "tags",
    "organization_all_tags",
]

def get_appuser(device_id):
    """Try to get an AppUser by device id

    Args:
        device_id (str): device id string

    Returns:
        AppUser or None
    """
    try:
        return AppUser.objects.get(device_id=device_id)
    except AppUser.DoesNotExist:
        return None


def hidden_regional_tags(app_user):
    """Gewählte regionale Tags, die der Nutzer in seinen Gebieten nicht sieht.

    Die App schickt beim Speichern nur Tags, die sie anzeigt. Damit eine Auswahl
    nach einem Umzug zurück ins Gebiet wieder greift, bleiben unsichtbare
    regionale Tags beim Speichern erhalten.
    """
    hidden = Tag.hidden_ids_for_areas(app_user.areas.values_list("id", flat=True))
    return list(app_user.tags.filter(id__in=hidden))


def clean_tag_name(text):
    """
    Entfernt alle Zeichen außer Buchstaben, Zahlen und Leerzeichen.
    """
    return re.sub(r'[^a-zA-Z0-9äöüÄÖÜß\s]', '', text)


class InFilter:
    def __init__(self, field_name, lookup_expr="in"):
        """
        Initializes a new instance of the InFilter class.

        Args:
            field_name (str): The name of the field to filter on.
            lookup_expr (str, optional): The lookup expression to use for filtering. Defaults to "in".
        """
        self.field_name = field_name
        self.lookup_expr = lookup_expr


# Define a custom filter backend class named DeviceIdFilter
class DeviceIdFilter(filters.BaseFilterBackend):
    
    # Define the method to filter the queryset based on the device ID from the request headers
    def filter_queryset(self, request, queryset, view):
        # Retrieve the device ID from the request headers
        device_id = request.headers.get("X-Device-ID", None)
        
        # If a device ID is found in the headers
        if device_id is not None:
            try:
                # Retrieve the AppUser instance
                app_user = AppUser.objects.get(device_id=device_id)
                # Annotate the queryset with 'bookmarked' field
                queryset = queryset.annotate(
                    bookmarked=Exists(
                        app_user.bookmarked_events.filter(pk=OuterRef('pk'))
                    )
                )
                
                # Annotate the queryset with 'archived' field
                queryset = queryset.annotate(
                    archived=Exists(
                        app_user.archived_events.filter(pk=OuterRef('pk'))
                    )
                )
            except AppUser.DoesNotExist:
                # Handle the case where the AppUser does not exist
                pass
    
        # Return the modified queryset
        return queryset

    # Define the method to get schema fields, which is required by the BaseFilterBackend
    def get_schema_fields(self, view):
        # Return an empty list as this filter does not add any fields to the schema
        return []


class ISODateTimeNoMicrosecondsField(serializers.DateTimeField):
    def to_representation(self, value):
        value = super().to_representation(value)
        if value is None:
            return None
        # value ist jetzt entweder ein datetime oder ein String!
        if isinstance(value, str):
            # Django kann value als String zurückgeben, wenn parse nicht nötig war
            value = serializers.DateTimeField().to_internal_value(value)
        # Wert ohne Mikrosekunden und mit ISO8601-Doppelpunkt-Offset
        return value.replace(microsecond=0).isoformat(timespec='seconds')
    
# Define a serializer class named EventTagsSerializer inheriting from ModelSerializer
class EventTagsSerializer(serializers.ModelSerializer):
    # Define a custom serializer method field for the selected status
    selected = serializers.SerializerMethodField()
    
    class Meta:
        model = Tag
        fields = ("id", "name", "name_en", "color", "selected")

    # Define a method to get the selected status, using a boolean field for Swagger documentation
    @swagger_serializer_method(serializers.BooleanField)
    def get_selected(self, obj):
        app_user = self.context.get("app_user", None)
        if app_user:
            return app_user.tags.filter(id=obj.id).exists()
        return False


class TagCategoryShortSerializer(serializers.ModelSerializer):
    class Meta:
        model = TagCategories
        fields = ('id', 'name', 'order')

class TagShortSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ['id', 'name', 'name_en']

class TagCategorySerializer(serializers.ModelSerializer):
    tags = TagShortSerializer(many=True, read_only=True, source='tagcategory')

    class Meta:
        model = TagCategories
        fields = ['id', 'name', 'order', 'tags']

    def get_tags(self, obj):
        tags = obj.tagcategory.all()  # related_name="tagcategory" bei Tag.tagcategories
        return TagShortSerializer(tags, many=True).data


class GenreSerializer(serializers.ModelSerializer):
    class Meta:
        model = Genre
        fields = ('id', 'name')


class FilmLanguageSerializer(serializers.ModelSerializer):
    class Meta:
        model = FilmLanguage
        fields = ('id', 'name')


# Define a serializer class named EventBaseSerializer inheriting from ModelSerializer
class EventBaseSerializer(serializers.ModelSerializer):

    # Define a nested serializer for tags using EventTagsSerializer, set to read-only, and allowing multiple entries
    tags = EventTagsSerializer(read_only=True, many=True)    
    start_date = ISODateTimeNoMicrosecondsField()
    end_date = serializers.SerializerMethodField()
    created_at = ISODateTimeNoMicrosecondsField(read_only=True)
    moddate = ISODateTimeNoMicrosecondsField(read_only=True)
    # Define custom serializer method fields for organization-related attributes
    organization = serializers.SerializerMethodField()
    organization_type = serializers.SerializerMethodField()
    organization_name = serializers.SerializerMethodField()
    organization_image = serializers.SerializerMethodField()
    # Define a custom serializer method field for the image URL
    image_url = serializers.SerializerMethodField()
    # Define custom serializer method fields for archived and bookmarked status
    archived = serializers.SerializerMethodField()
    bookmarked = serializers.SerializerMethodField()
    area = serializers.SerializerMethodField()
    genres = GenreSerializer(read_only=True, many=True)
    film_languages = FilmLanguageSerializer(read_only=True, many=True)
    type = serializers.SerializerMethodField()
    translations = serializers.SerializerMethodField()

    @swagger_serializer_method(serializers.CharField)
    def get_type(self, instance):
        """Inhaltstyp für den Client, bei Events immer 'event'."""
        return "event"

    def get_translations(self, instance):
        """Verlinkte, veröffentlichte Sprachversionen des Events als [{id, language}]."""
        return [
            {"id": t.id, "language": t.language}
            for t in instance.translations.all()
            if t.published
        ]

    @swagger_serializer_method(serializers.DateTimeField)
    def get_end_date(self, instance):
        """
        Gibt das end_datetime der gleichen Occurrence zurück, 
        die für start_date verwendet wurde.
        """
        import pytz
        from django.utils import timezone
        
        # Deutsche/Europäische Timezone (+2 Stunden im Sommer, +1 im Winter)
        europe_tz = pytz.timezone('Europe/Berlin')
        now_dt = timezone.now()
        
        # Gleiche Logik wie für start_date
        for occ in instance.occurrences.all():
            occ_start = occ.start_datetime
            occ_end = occ.end_datetime
            
            # Timezone-aware machen
            if timezone.is_naive(occ_start):
                occ_start = timezone.make_aware(occ_start, timezone=pytz.UTC)
            if timezone.is_naive(occ_end):
                occ_end = timezone.make_aware(occ_end, timezone=pytz.UTC)

            # Läuft gerade? (gleiche Logik wie start_date)
            if occ_start <= now_dt <= occ_end:
                # Konvertiere zu europäischer Zeit vor der Rückgabe
                occ_end_local = occ_end.astimezone(europe_tz)
                return occ_end_local.replace(microsecond=0).isoformat(timespec='seconds')
            
            # Zukünftige Occurrence? (nimm die früheste)
            elif occ_start > now_dt:
                # Wenn start_date auf diese Occurrence gesetzt wurde, 
                # dann nehmen wir auch deren end_date
                if not hasattr(self, '_next_occurrence_end'):
                    self._next_occurrence_end = occ_end
                    self._next_occurrence_start = occ_start
                elif occ_start < self._next_occurrence_start:
                    self._next_occurrence_end = occ_end
                    self._next_occurrence_start = occ_start
        
        # Fallback: wenn wir eine zukünftige Occurrence gefunden haben
        if hasattr(self, '_next_occurrence_end'):
            # Konvertiere zu europäischer Zeit vor der Rückgabe
            end_local = self._next_occurrence_end.astimezone(europe_tz)
            result = end_local.replace(microsecond=0).isoformat(timespec='seconds')
            # Cleanup
            delattr(self, '_next_occurrence_end')
            delattr(self, '_next_occurrence_start')
            return result

        # Fallback für komplett vergangene Events (Abfrage mit vergangenem
        # date_start): end_date wurde von der List-Logik aus der passenden
        # Occurrence am Instance gesetzt (das Model hat kein end_date-Feld)
        assigned_end = getattr(instance, "end_date", None)
        if isinstance(assigned_end, datetime):
            if timezone.is_naive(assigned_end):
                assigned_end = timezone.make_aware(assigned_end, timezone=pytz.UTC)
            end_local = assigned_end.astimezone(europe_tz)
            return end_local.replace(microsecond=0).isoformat(timespec='seconds')

        return None

    # Define a method to get the archived status, using a boolean field for Swagger documentation
    @swagger_serializer_method(serializers.BooleanField)
    def get_archived(self, instance):
        # Get the archived attribute from the instance, defaulting to False if not present, and convert to boolean
        archived = bool(getattr(instance, "archived", False))
        return archived

    # Define a method to get the bookmarked status, using a boolean field for Swagger documentation
    @swagger_serializer_method(serializers.BooleanField)
    def get_bookmarked(self, instance):
        # get the appuser from the x device id in the header of request
        app_user = get_appuser(self.context.get("request").headers.get("X-Device-ID"))
        if app_user:
            return app_user.bookmarked_events_v4.filter(id=instance.id).exists()
        return False
    
    def get_organization_image(self, instance):
        org = instance.source.organization
        if org and org.image:
            request = self.context.get("request")
            return request.build_absolute_uri(org.image.url)
        return None

    # Define a method to get the image URL
    def get_image_url(self, instance):
        # If the instance has an image, return the absolute URL
        if instance.image:
            return self.context["request"].build_absolute_uri(instance.image.url)
        # If the instance has an image_url, return it
        elif instance.image_url:
            return instance.image_url
        # Otherwise, return the default image for the source
        else:
            return get_source_default_image_url(instance, self.context)

    # Define a method to get the organization ID, using an integer field for Swagger documentation
    @swagger_serializer_method(serializers.IntegerField)
    def get_organization(self, instance):
        return instance.source.organization.id

    # Define a method to get the organization type
    def get_organization_type(self, instance):
        return instance.source.organization.type

    # Define a method to get the organization name
    def get_organization_name(self, instance):
        return instance.source.organization.name
    
    @swagger_serializer_method(serializer_or_field=AreaSerializer(many=True))
    def get_area(self, instance):
        return AreaSerializer(instance.area.all(), many=True).data
    
    
class EventOccurrenceSerializer(serializers.ModelSerializer):
    start_datetime = ISODateTimeNoMicrosecondsField()
    end_datetime = ISODateTimeNoMicrosecondsField()
    class Meta:
        model = Event_Occurrence
        fields = (
            "start_datetime",
            "end_datetime",
        )
@swagger_serializer_method(serializers.IntegerField)
def get_bookmarked_count(self, instance):
    """
    This method returns the amount of bookmarks for the event.
    """
    return instance.events_bookmarked.count()

# Serializer for the event list, inheriting from EventBaseSerializer
class EventListSerializer(EventBaseSerializer):

    # Define a SerializerMethodField to handle custom serialization for the 'link' field
    link = serializers.SerializerMethodField()
    occurrences_list = serializers.SerializerMethodField()
    bookmarked_count = serializers.SerializerMethodField()
    ticket_url = serializers.SerializerMethodField()

    # Meta class to specify the model and fields to be included in the serialization
    class Meta:
        model = EventV4  # The model that this serializer is associated with
        fields = EVENT_LIST_FIELDS + (  # Include predefined fields and additional fields
            "archived",
            "bookmarked",
            "occurrences_list",
            "longitude",
            "latitude",
            "street",
            "town",
            "zip_code",
            "bookmarked_count",
            "ticket_url",
            "published",
        )

    def get_ticket_url(self, obj):
        # Gibt "" zurück, wenn ticket_url leer oder None ist
        return obj.ticket_url or ""

    # Custom method to get the link, decorated for swagger documentation
    @swagger_serializer_method(serializers.CharField)
    def get_link(self, instance):
        # If the instance is a child event, retrieve the parent event's link
        if instance.is_child:
            pass
            #_event_child = EventChild.objects.get(event=instance)  # Fetch the EventChild instance
            #return _event_child.parent.link  # Return the parent event's link
        # If the instance is not a child event, return its own link
        return instance.link
    
    @swagger_serializer_method(serializer_or_field=EventOccurrenceSerializer(many=True))
    def get_occurrences_list(self, obj):
        # This method will fetch the occurrences for the event
        event_occurrences = Event_Occurrence.objects.filter(event=obj)
        return EventOccurrenceSerializer(event_occurrences, many=True).data
    
    # get the bookmarked status of the event out of the m2m database
 

    @swagger_serializer_method(serializers.IntegerField)
    def get_bookmarked_count(self, instance):
        """
        This method returns the amount of bookmarks for the event.
        """
        return instance.events_bookmarked.count()

    # Define a method to get the bookmarked status, using a boolean field for Swagger documentation
    @swagger_serializer_method(serializers.BooleanField)
    def get_bookmarked(self, instance):
        # get the appuser from the x device id in the header of request
        app_user = get_appuser(self.context.get("request").headers.get("X-Device-ID"))
        if app_user:
            return app_user.bookmarked_events_v4.filter(id=instance.id).exists()
        return False


class EventBookmarkListSerializer(EventListSerializer):
    class Meta(EventListSerializer.Meta):
        fields = tuple(
            f for f in EventListSerializer.Meta.fields
            if f not in ('request_count', 'bookmarked_count')
        )


# Serializer for the event list, inheriting from EventBaseSerializer
class EventListSerializerOldArea(EventBaseSerializer):

    # Define a SerializerMethodField to handle custom serialization for the 'link' field
    link = serializers.SerializerMethodField()
    occurrences_list = serializers.SerializerMethodField()
    bookmarked_count = serializers.SerializerMethodField()
    ticket_url = serializers.SerializerMethodField()

    # Meta class to specify the model and fields to be included in the serialization
    class Meta:
        model = EventV4  # The model that this serializer is associated with
        fields = EVENT_LIST_FIELDS + (  # Include predefined fields and additional fields
            "archived",
            "bookmarked",
            "occurrences_list",
            "longitude",
            "latitude",
            "street",
            "town",
            "zip_code",
            "bookmarked_count",
            "ticket_url",
        )

    @swagger_serializer_method(serializers.ListField(child=serializers.IntegerField()))
    def get_area(self, instance):
        # Gibt die Area-IDs als Liste zurück
        return [area.id for area in instance.area.all()]

    def get_ticket_url(self, obj):
        # Gibt "" zurück, wenn ticket_url leer oder None ist
        return obj.ticket_url or ""

    # Custom method to get the link, decorated for swagger documentation
    @swagger_serializer_method(serializers.CharField)
    def get_link(self, instance):
        # If the instance is a child event, retrieve the parent event's link
        if instance.is_child:
            pass
            #_event_child = EventChild.objects.get(event=instance)  # Fetch the EventChild instance
            #return _event_child.parent.link  # Return the parent event's link
        # If the instance is not a child event, return its own link
        return instance.link
    
    @swagger_serializer_method(serializer_or_field=EventOccurrenceSerializer(many=True))
    def get_occurrences_list(self, obj):
        # This method will fetch the occurrences for the event
        event_occurrences = Event_Occurrence.objects.filter(event=obj)
        return EventOccurrenceSerializer(event_occurrences, many=True).data
    
    # get the bookmarked status of the event out of the m2m database
 

    @swagger_serializer_method(serializers.IntegerField)
    def get_bookmarked_count(self, instance):
        """
        This method returns the amount of bookmarks for the event.
        """
        return instance.events_bookmarked.count()

    # Define a method to get the bookmarked status, using a boolean field for Swagger documentation
    @swagger_serializer_method(serializers.BooleanField)
    def get_bookmarked(self, instance):
        # get the appuser from the x device id in the header of request
        app_user = get_appuser(self.context.get("request").headers.get("X-Device-ID"))
        if app_user:
            return app_user.bookmarked_events_v4.filter(id=instance.id).exists()
        return False

# Serializer for the event list, inheriting from EventBaseSerializer
class EventListSerializerShort(EventBaseSerializer):

    content = serializers.SerializerMethodField()

    # Meta class to specify the model and fields to be included in the serialization
    class Meta:
        model = EventV4  # The model that this serializer is associated with
        fields = (  # Include predefined fields and additional fields
            "id",
            "title",
            "content",
            "image_url",
            "start_date",
            "event_location",
            "organization",
            "organization_name",
            "organization_image",
            "street",
            "town",
            "zip_code",
        )

    def get_content(self, obj):
        """Kürzt den Content auf 150 Zeichen"""
        if obj.content and len(obj.content) > 150:
            return obj.content[:150] + "..."
        return obj.content or ""


# Serializer for paginated events with bookmarked and archived status
class PaginatedEventBookmarkedArchivedSerializer(serializers.Serializer):
    
    # Field to store the total count of events
    count = serializers.IntegerField()

    # Field to store the URL of the next page of results, if available
    next = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    
    # Field to store the URL of the previous page of results, if available
    previous = serializers.URLField(allow_blank=True, allow_null=True, required=False)

    # Field to store the list of event results, using EventListSerializer for each event
    results = EventListSerializer(many=True)

class EventRetrieveSerializer(EventBaseSerializer):
    # Serializer method field to dynamically get the 'link' attribute
    link = serializers.SerializerMethodField()
    occurrences_list = serializers.SerializerMethodField()
    bookmarked_count = serializers.SerializerMethodField()
    ticket_url = serializers.SerializerMethodField()

    class Meta:
        # Specify the model associated with this serializer
        model = EventV4
        # Define the fields to be included in the serialized output
        fields = EVENT_RETRIEVE_FIELDS + (
            "archived",
            "bookmarked",
            "occurrences_list", 
            "longitude",
            "latitude",
            "street",
            "town",
            "zip_code",
            "bookmarked_count",
            "ticket_url",
        )

    
    def get_ticket_url(self, obj):
        # Gibt "" zurück, wenn ticket_url leer oder None ist
        return obj.ticket_url or ""
    
    def get_occurrences_list(self, obj):
        # This method will fetch the occurrences for the event
        event_occurrences = Event_Occurrence.objects.filter(event=obj)
        return EventOccurrenceSerializer(event_occurrences, many=True).data
    
    @swagger_serializer_method(serializers.IntegerField)
    def get_bookmarked_count(self, instance):
        """
        This method returns the amount of bookmarks for the event.
        """
        return instance.events_bookmarked.count()
    
    @swagger_serializer_method(serializers.CharField)
    def get_link(self, instance):
        """
        Method to retrieve the 'link' field of the EventV4 instance.
        This method uses swagger_serializer_method decorator to specify
        the field type in the Swagger documentation.
        """
        return instance.link

    # TODO: go through List
    @swagger_serializer_method(serializers.DateTimeField)
    def get_event_end_date(self, instance):
        """
        Method to retrieve the 'event_end_date' field of the EventV4 instance.
        This method uses swagger_serializer_method decorator to specify
        the field type in the Swagger documentation.
        """
        return instance.event_end_date 
    
    # TODO: go through List
    @swagger_serializer_method(serializers.DateTimeField)
    def get_event_start_date(self, instance):
        """
        Method to retrieve the start_dates of the EventV4 instance.
        Adjusts the event start date if the event date has passed.
        This method uses swagger_serializer_method decorator to specify
        the field type in the Swagger documentation.
        """
        _now = localtime().replace(microsecond=0, second=0)
        if instance.event_date.date() < _now.date():
            return _now.replace(hour=0, minute=0)
        return instance.event_date


class EventRetrieveSerializerOldArea(EventBaseSerializer):
    # Serializer method field to dynamically get the 'link' attribute
    link = serializers.SerializerMethodField()
    occurrences_list = serializers.SerializerMethodField()
    bookmarked_count = serializers.SerializerMethodField()
    ticket_url = serializers.SerializerMethodField()

    class Meta:
        # Specify the model associated with this serializer
        model = EventV4
        # Define the fields to be included in the serialized output
        fields = EVENT_RETRIEVE_FIELDS + (
            "archived",
            "bookmarked",
            "occurrences_list", 
            "longitude",
            "latitude",
            "street",
            "town",
            "zip_code",
            "bookmarked_count",
            "ticket_url",
        )

    @swagger_serializer_method(serializers.ListField(child=serializers.IntegerField()))
    def get_area(self, instance):
        # Gibt die Area-IDs als Liste zurück
        return [area.id for area in instance.area.all()]

    def get_ticket_url(self, obj):
        # Gibt "" zurück, wenn ticket_url leer oder None ist
        return obj.ticket_url or ""
    
    def get_occurrences_list(self, obj):
        # This method will fetch the occurrences for the event
        event_occurrences = Event_Occurrence.objects.filter(event=obj)
        return EventOccurrenceSerializer(event_occurrences, many=True).data
    
    @swagger_serializer_method(serializers.IntegerField)
    def get_bookmarked_count(self, instance):
        """
        This method returns the amount of bookmarks for the event.
        """
        return instance.events_bookmarked.count()
    
    @swagger_serializer_method(serializers.CharField)
    def get_link(self, instance):
        """
        Method to retrieve the 'link' field of the EventV4 instance.
        This method uses swagger_serializer_method decorator to specify
        the field type in the Swagger documentation.
        """
        return instance.link

    # TODO: go through List
    @swagger_serializer_method(serializers.DateTimeField)
    def get_event_end_date(self, instance):
        """
        Method to retrieve the 'event_end_date' field of the EventV4 instance.
        This method uses swagger_serializer_method decorator to specify
        the field type in the Swagger documentation.
        """
        return instance.event_end_date 
    
    # TODO: go through List
    @swagger_serializer_method(serializers.DateTimeField)
    def get_event_start_date(self, instance):
        """
        Method to retrieve the start_dates of the EventV4 instance.
        Adjusts the event start date if the event date has passed.
        This method uses swagger_serializer_method decorator to specify
        the field type in the Swagger documentation.
        """
        _now = localtime().replace(microsecond=0, second=0)
        if instance.event_date.date() < _now.date():
            return _now.replace(hour=0, minute=0)
        return instance.event_date


class PaginatedEventRetrieveSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    previous = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    results = EventListSerializer(many=True)

class PaginatedEventRetrieveSerializerShort(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    previous = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    results = EventListSerializerShort(many=True)

class EventPagination(LimitOffsetPagination):
    page_query_param = 'offset'
    page_size = 50  # Set the page size to your preference
    page_size_query_param = 'limit'  # Set the query parameter for page size
    max_page_size = 100  # Set the maximum page size
    offset_query_param = 'offset'  # Set the query parameter for offset
    default_limit = 20
    max_limit = 100

class EventViewSet(BaseViewSet):

    # Define the queryset to fetch only published EventV4 objects
    queryset = EventV4.objects.filter(published=True)
    # Specify the serializer class to be used
    serializer_class = EventListSerializer
    # Define the model class associated with this view set
    model_class = EventV4
    # Specify the filter backends to be used for filtering the queryset
    filter_backends = BaseViewSet.filter_backends + [
        DeviceIdFilter,
    ]
    # Uncomment to use a specific filter set class
    # filterset_class = EventBaseFilter
    # Define the time period to show events in advance (in days)
    time_period_to_show_in_advance = 30
    # Define the time period to show events retrospectively (in days)
    time_period_to_show_retrospectively = 1

    # Specify the fields that can be used for ordering the queryset
    ordering_fields = ["start_date"]
    # Set the default ordering for the queryset
    ordering = ["start_date"]

    def paginate_queryset(self, queryset):
        # Wenn 'limit' nicht übergeben wird, setze es standardmäßig auf 50
        if 'limit' not in self.request.query_params:    
            mutable_querydict = self.request.query_params.copy()
            mutable_querydict['limit'] = '20'
            self.request._request.GET = mutable_querydict

        return super().paginate_queryset(queryset)

    def get_limited_queryset(self, device_id, areas=None, exclude_ids=[], tag_filter=[], subtag_filter=[], organization_filer=[], town="", use_basefilter=True):
        """
        Get a query set limited by time and without bookmarked / archived events.
        Now uses efficient Django QuerySet filtering instead of manual loops.

        Args:
            device_id (str): Device ID
            areas: Area IDs (string, list, or int)
            exclude_ids (list): List of IDs to exclude from queryset
            tag_filter (list): List of tag IDs to filter by
            subtag_filter (list): List of subtag IDs to filter by
            organization_filer (list): List of organization IDs to filter by
            town (str): Town name to filter by
            use_basefilter (bool): Use all base filters or if set to False,
                only DeviceIdFilter and SourceActiveFilter

        Returns:
            queryset, excluded_articles_ids, newest_date
        """
        # If not using the base filter, redefine the filter backends
        if not use_basefilter:
            self.filter_backends = [DeviceIdFilter, SourceActiveFilter]

        # Calculate the current date
        current_date = datetime.now().date()
        # Calculate the oldest date based on the retrospective time period
        oldest_date = current_date - timedelta(
            days=self.time_period_to_show_retrospectively
        )

        # Current datetime (UTC)
        now = datetime.now(pytz.utc)

        # If subtags are selected, extend the filter period retroactively from 01.01.2025
        if subtag_filter:
            custom_start_date = datetime(2025, 1, 1, tzinfo=pytz.utc)
            logger.info(f"Custom start date set to: {custom_start_date}")
        else:
            custom_start_date = now

        # Build clean areas list
        areas_list = []
        if areas:
            if isinstance(areas, list):
                areas_list = [int(area) for area in areas]
            elif isinstance(areas, str):
                areas_list = [int(area) for area in areas.split(",") if area.strip()]
            else:
                areas_list = [int(areas)]

        # Start with base queryset
        base_queryset = self.get_queryset().filter(
            is_past=False,
            published=True
        ).distinct()
           

        # Apply area filter if areas are specified
        if areas_list:
            base_queryset = base_queryset.filter(area__in=areas_list)

        # Apply tag filter if specified
        if tag_filter:
            base_queryset = base_queryset.filter(tags__in=tag_filter)

        # Apply subtag filter if specified
        if subtag_filter:
            base_queryset = base_queryset.filter(tags__in=subtag_filter)

        # Apply organization filter if specified
        if organization_filer:
            base_queryset = base_queryset.filter(source__organization__in=organization_filer)

        # Apply town filter if specified
        if town:
            base_queryset = base_queryset.filter(town__iexact=town)

        # Ensure we get distinct results after all the filtering
        base_queryset = base_queryset.distinct()

        # Convert to list for compatibility with existing code that expects a list
        # and add occurrences_list to each event
        events_list = []
        for event in base_queryset:
            # Add occurrences to the event (same as original code)
            event_occurrences = Event_Occurrence.objects.filter(event=event)
            event.occurrences_list = list(event_occurrences)
            
            # Set start_date as next occurrence (same logic as list_future_events_by_category)
            now_dt = datetime.now(pytz.utc)
            next_occurrence_start = None
            next_occurrence_sort = None
            next_occurrence_end = None

            for occ in event.occurrences.all():
                occ_start = occ.start_datetime
                occ_end = occ.end_datetime
                if timezone.is_naive(occ_start):
                    occ_start = timezone.make_aware(occ_start, timezone=pytz.UTC)
                if timezone.is_naive(occ_end):
                    occ_end = timezone.make_aware(occ_end, timezone=pytz.UTC)

                # Currently running? Anzeige bleibt der echte Start,
                # sortiert wird weiterhin nach "jetzt"
                if occ_start <= now_dt <= occ_end:
                    next_occurrence_start = occ_start
                    next_occurrence_sort = now_dt
                    next_occurrence_end = occ_end
                    break
                # Future occurrence?
                elif occ_start > now_dt:
                    if not next_occurrence_start or occ_start < next_occurrence_start:
                        next_occurrence_start = occ_start
                        next_occurrence_sort = occ_start
                        next_occurrence_end = occ_end

            # Fallback: if no future occurrence, use original start_date
            if next_occurrence_start:
                event.start_date = next_occurrence_start
                setattr(event, SORT_START_ATTR, next_occurrence_sort)
            if next_occurrence_end:
                event.end_date = next_occurrence_end
            # Falls keine Occurrence gefunden, könnte man None setzen oder ein Fallback verwenden
            else:
                event.end_date = None
                
            events_list.append(event)

        # Return the filtered event list, excluded IDs, and the oldest date
        return events_list, exclude_ids, oldest_date


    @swagger_auto_schema(
        operation_description=(
            "Liefert alle veröffentlichten Events einer Organisation.\n"
        ),
        manual_parameters=[
            openapi.Parameter(
                "organization_id",
                openapi.IN_QUERY,
                description="ID der Organisation (Pflicht)",
                type=openapi.TYPE_INTEGER,
                required=True,
            ),
            # optionale Pagination wie in der normalen List-Methode
            integer_parameter("limit", "Ergebnis-Limit (optional)"),
            integer_parameter("offset", "Result-Offset (optional)"),
            header_string_parameter("X-Device-ID", "Device ID"),
        ],
        responses={
            200: PaginatedEventRetrieveSerializerShort,
            400: "Ungültiger oder fehlender Parameter",
            404: "Organisation nicht gefunden / keine Events",
        },
    )
    def list_events_by_organization(self, request, *args, **kwargs):
        """
        Gibt alle Events der angegebenen Organisation zurück –
        """
        org_id = request.query_params.get("organization_id")
        if not (org_id and org_id.isdigit()):
            return bad_request("organization_id missing or invalid")
        org_id = int(org_id)

        try:
            organization = Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist:
            return Response({"detail": "Organization not found."}, status=404)

        queryset = (
            EventV4.objects
            .filter(published=True, is_past=False, source__organization=organization)
            .order_by("start_date")
        )

        # Optional: Suche nach Titel/Abstract/Tags
        search = request.query_params.get("search")
        if search:
            queryset = queryset.filter(build_fulltext_filter(search)).distinct()
        
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = EventListSerializerShort(page, many=True, context={"request": request})
            return self.get_paginated_response(serializer.data)
        else:
            serializer = self.get_serializer(queryset, many=True)
            
        return Response(serializer.data, status=200)
    
    
    @swagger_auto_schema(
    manual_parameters=[
        header_string_parameter("X-Device-ID", "Device ID", required=False)
    ],
    operation_description="Retrieve a single article. Optional: X-Device-ID header to determine bookmarked state."
    )
    def retrieve(self, request, *args, **kwargs):
        """
        Retrieves a single event by its ID.
        """
        logger.info("Retrieve Event called in backend")
        # Get the device ID from the request headers


        pk = kwargs.get('pk')
        if not pk:
            logger.error("Event ID (pk) is missing in the request.")
            return Response({"detail": "Event ID is required."}, status=status.HTTP_400_BAD_REQUEST)
        
        # Try to get the event by its ID
        event = get_object_or_404(EventV4, pk=pk)
        if not event.published and not can_view_unpublished(request, event):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        user_agent = request.META.get('HTTP_USER_AGENT', '').lower()
    
        if 'molo/5.1.2' in user_agent or "okhttp/" in user_agent:
            #remove this and the Serializer once the update is finished
            logger.info("Using old area serializer")
            serializer = EventRetrieveSerializerOldArea(event, context={"request": request})
        else:
            logger.info("Using new area serializer")
            serializer = EventRetrieveSerializer(event, context={"request": request})

        return Response(serializer.data)

    @swagger_auto_schema(
        manual_parameters=[
            string_parameter(
                "search", "Search term - searches in title, abstract, content and tags"
            ),
            choices_parameter(
                "ordering",
                ("event_date", "-event_date"),
                'Ordering (default is "event_date")',
            ),
            isodate_parameter(
                "date_start",
                "ISO 8601 formatted - YYYY-MM-DD | defaults to today; "
                "past dates allowed to query past events",
            ),
            isodate_parameter(
                "date_end",
                "ISO 8601 formatted - YYYY-MM-DD | defaults to date_start + 365 days",
            ),
            string_parameter(
                "tags",
                "Tag ID(s) - comma separated",
            ),
            string_parameter(
                "subtags",
                "Tag ID(s) - comma separated",
            ),
            openapi.Parameter(
                "areas",
                openapi.IN_QUERY,
                description="List of area IDs - comma separated",
                type=openapi.TYPE_STRING,
                required = False 
            ),
            string_parameter(
                "town",
                "Town",
                required=False,
            ),
            string_parameter(
                "organization",
                "Organization ID(s) - comma separated | defaults to user settings, if supplied min date of events will be current date",
                required=False,
            ),
            string_parameter(
                "language",
                "Language(s) - comma separated ('de', 'en' or 'de,en'). "
                "Without this parameter only German events are returned "
                "(English additionally requires subtag 3 'Englisch' for legacy clients)",
                required=False,
            ),
        ]
        + [
            boolean_parameter(
                event_type[0],
                'Include events with type "{}", defaults to true'.format(event_type[0]),
                default=True,
            )
            for event_type in ORGANIZATION_TYPE_CHOICES
        ]
        + [
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],

        operation_description="""List published events.\n
         """,
        responses={
            200: PaginatedEventRetrieveSerializer,
            400: "Device ID missing."
            },
    )
    
    def list(self, *args, **kwargs):
        # Unique Request ID für Logging
        request_id = str(uuid.uuid4())[:8]

        # Adding a custom filter to the filter backends
        self.filter_backends = EventViewSet.filter_backends

        # Extracting the Device ID from the request headers
        device_id = self.request.headers.get("X-Device-ID")

        # If Device ID is not provided, return a 400 bad request response
        if not device_id:
            return bad_request("Device ID is missing")

        logger.info(f"[{request_id}] GET /events/ - Device ID: {device_id}")

        # Extract parameters
        tags = self.request.query_params.get("tags", "")
        subtags = self.request.query_params.get("subtags", "")
        organization = self.request.query_params.get("organization", "")
        town = self.request.query_params.get("town", "")
        search = self.request.query_params.get("search")
        category_id_param = self.request.query_params.get("category_id")
        # language: kommasepariert, z.B. "de", "en" oder "de,en"
        language_param = self.request.query_params.get("language", "").strip().lower()
        languages = [lang.strip() for lang in language_param.split(",") if lang.strip()]
        if any(lang not in ("de", "en") for lang in languages):
            return Response({"detail": "Invalid language. Use 'de', 'en' or 'de,en'."}, status=400)

        tags_list = [int(tag) for tag in tags.split(",") if tag.strip().isdigit()] if tags else []
        subtags_list = [int(tag) for tag in subtags.split(",") if tag.strip().isdigit()] if subtags else []
        organization_list = [int(org) for org in organization.split(",") if org.strip().isdigit()] if organization else []
        category_id = int(category_id_param) if category_id_param and category_id_param.strip().isdigit() else None

        logger.info(f"[{request_id}] Parameters - tags: {tags_list}, subtags: {subtags_list}, org: {organization_list}, town: {town}, search: {search}, category_id: {category_id}")

        # Get AppUser
        app_user = get_appuser(device_id)
        if not app_user:
            logger.warning(f"[{request_id}] App user not found for device_id: {device_id}")

        # Get areas - from parameter or user's configured areas
        areas_str = self.request.query_params.get("areas")
        user_areas = []

        if areas_str:
            if isinstance(areas_str, str):
                user_areas = [int(area) for area in areas_str.split(",") if area.strip().isdigit()]
            elif isinstance(areas_str, list):
                user_areas = [int(a) for a in areas_str if str(a).isdigit()]
            logger.info(f"[{request_id}] Using areas from parameter: {user_areas}")
        elif app_user and app_user.areas.exists():
            user_areas = list(app_user.areas.values_list("id", flat=True))
            logger.info(f"[{request_id}] Using user's configured areas: {user_areas}")
        elif app_user and app_user.area:
            # Fallback to single area field
            user_areas = [app_user.area.id]
            logger.info(f"[{request_id}] Using user's single area: {user_areas}")
        else:
            # Fallback: Standard-Gebiete der Instanz (leer = kein Gebietsfilter)
            user_areas = list(getattr(settings, 'DEFAULT_AREA_IDS', []) or [])
            logger.info(f"[{request_id}] No areas configured for user, using default areas: {user_areas}")

        # Date parsing helper function
        def parse_datetime(date_string, param_name):
            formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
            for fmt in formats:
                try:
                    dt = datetime.strptime(date_string, fmt)
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt, timezone=pytz.UTC)
                    return dt
                except ValueError:
                    continue
            return None

        # Parse date parameters
        date_start_param = self.request.query_params.get("date_start")
        date_end_param = self.request.query_params.get("date_end")
        now_dt = timezone.now()

        # Default: Use current date as start
        if not date_start_param:
            date_start_param = now_dt.date().strftime("%Y-%m-%d")

        date_start = parse_datetime(date_start_param, "date_start")
        if date_start is None:
            return Response({"detail": "Invalid date_start format. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS."}, status=400)

        # Vergangener Kalendertag als date_start => auch vergangene Events liefern
        # (date_start = heute behält bewusst das bisherige Verhalten)
        include_past = date_start.date() < now_dt.date()

        # Default: 12 months (365 days) after date_start if no end date
        if not date_end_param:
            default_end = (date_start + timedelta(days=365)).date()
            date_end_param = default_end.strftime("%Y-%m-%d")

        date_end = parse_datetime(date_end_param, "date_end")
        if date_end is None:
            return Response({"detail": "Invalid date_end format. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS."}, status=400)
        # If date_end is midnight, extend to end of day
        if date_end.time() == time.min:
            date_end = datetime.combine(date_end.date(), time.max)
            if timezone.is_naive(date_end):
                date_end = timezone.make_aware(date_end, timezone=pytz.UTC)

        real_date_end = date_end
        if date_start and date_end and date_start.date() == date_end.date() and (date_end.time() == time.min):
            real_date_end = datetime.combine(date_end.date(), time.max)
            if timezone.is_naive(real_date_end):
                real_date_end = timezone.make_aware(real_date_end, timezone=pytz.UTC)

        logger.info(f"[{request_id}] Date range: {date_start} to {real_date_end}")

        # Gesetzt, sobald der Zeitraum der Anfrage ("am Wochenende") uebernommen wird:
        # Termine, die vor diesem Zeitpunkt begonnen haben, sortieren nach hinten.
        sort_window_start = None

        # Build optimized QuerySet with Prefetch & Exists
        from django.db.models import Prefetch

        # Prefetch only relevant Occurrences (with Date-Filter)
        occurrence_qs = Event_Occurrence.objects.all()

        # Subquery for EXISTS check: Only events with at least one matching occurrence
        occurrence_subquery = Event_Occurrence.objects.filter(event=OuterRef('pk'))

        if date_start and real_date_end:
            logger.info(f"[{request_id}] Occurrence filter: start_datetime <= {real_date_end} AND end_datetime >= {date_start}")
            occurrence_qs = occurrence_qs.filter(
                Q(start_datetime__lte=real_date_end) & Q(end_datetime__gte=date_start)
            )
            occurrence_subquery = occurrence_subquery.filter(
                Q(start_datetime__lte=real_date_end) & Q(end_datetime__gte=date_start)
            )
        elif date_start:
            logger.info(f"[{request_id}] Occurrence filter: end_datetime >= {date_start}")
            occurrence_qs = occurrence_qs.filter(end_datetime__gte=date_start)
            occurrence_subquery = occurrence_subquery.filter(end_datetime__gte=date_start)
        elif real_date_end:
            logger.info(f"[{request_id}] Occurrence filter: start_datetime <= {real_date_end}")
            occurrence_qs = occurrence_qs.filter(start_datetime__lte=real_date_end)
            occurrence_subquery = occurrence_subquery.filter(start_datetime__lte=real_date_end)

        occurrence_qs = occurrence_qs.order_by('start_datetime')

        # Build main QuerySet
        # Bei vergangenem date_start kein is_past-Filter: die Eingrenzung
        # übernimmt dann allein der Occurrence-Datumsfilter
        base_event_filter = {"published": True}
        if not include_past:
            base_event_filter["is_past"] = False

        qs = (
            EventV4.objects.filter(**base_event_filter)
            .annotate(has_occurrence=Exists(occurrence_subquery))
            .filter(has_occurrence=True)  # Only events with matching occurrences
            .select_related('source', 'source__organization')
            .prefetch_related(
                Prefetch('occurrences', queryset=occurrence_qs, to_attr='filtered_occurrences'),
                'tags',
                'subtags',
                'translations'
            )
        )

        # Apply filters
        if user_areas:
            qs = qs.filter(area__in=user_areas)
        if tags_list:
            qs = qs.filter(tags__in=tags_list)
        if subtags_list:
            qs = qs.filter(subtags__in=subtags_list)

        if organization_list:
            qs = qs.filter(source__organization__in=organization_list)
        if town:
            qs = qs.filter(town__iexact=town)
        # Englische Events nur liefern, wenn explizit angefragt: per language
        # (z.B. "en" oder "de,en") oder (Alt-Clients) über den Subtag "Englisch" (ID 3)
        ENGLISCH_SUBTAG_ID = 3
        if languages:
            qs = qs.filter(language__in=languages)
        elif ENGLISCH_SUBTAG_ID not in subtags_list:
            qs = qs.exclude(language="en")
            logger.info(f"[{request_id}] Excluding English events (no language param, subtag Englisch not requested)")

        # ==========================================
        # SEARCH LOGIC - Traditional vs RAG (without area filter)
        # Bei 1-3 Wörtern: Volltextsuche (schnell, zuverlässig)
        # Ab 4 Wörtern: RAG-Suche (semantisch, LLM-basiert)
        # ==========================================
        if search:
            word_count = len(search.strip().split())
            use_rag = getattr(settings, 'USE_RAG_EVENT_SEARCH', False) and word_count >= 3

            if use_rag:
                # ===== RAG-BASED SEARCH WITH CACHE (ab 4 Wörtern) =====
                logger.info(f"[{request_id}] Using RAG search (cached) for query: '{search}' ({word_count} words)")

                # Base filters for cache key
                base_filters = {}
                if organization_list:
                    base_filters['organization'] = organization_list
                if town:
                    base_filters['town'] = town
                if tags_list:
                    base_filters['tags'] = tags_list
                if subtags_list:
                    base_filters['subtags'] = subtags_list

                # For RAG: QuerySet WITHOUT area filter (to find events in other regions)
                rag_queryset = (
                    EventV4.objects.filter(
                        **base_event_filter
                        # NO area__in=user_areas filter!
                    )
                    .annotate(has_occurrence=Exists(occurrence_subquery))
                    .filter(has_occurrence=True)
                )

                # Apply other filters
                if tags_list:
                    rag_queryset = rag_queryset.filter(tags__in=tags_list)
                if subtags_list:
                    rag_queryset = rag_queryset.filter(subtags__in=subtags_list)
                if organization_list:
                    rag_queryset = rag_queryset.filter(source__organization__in=organization_list)
                if town:
                    rag_queryset = rag_queryset.filter(town__iexact=town)
                if languages:
                    rag_queryset = rag_queryset.filter(language__in=languages)
                elif ENGLISCH_SUBTAG_ID not in subtags_list:
                    rag_queryset = rag_queryset.exclude(language="en")

                # Perform RAG search with cache
                # NOTE: Do NOT pass pagination limit to RAG - RAG should return max results (200)
                # and pagination happens afterwards in the normal DRF pagination flow
                rag_window = {}
                rag_event_ids = perform_rag_search_with_cache(
                    search_query=search,
                    request=self.request,
                    logger=logger,
                    user_areas=user_areas,
                    date_start=date_start,
                    date_end=real_date_end,
                    base_filters=base_filters,
                    category_id=category_id,
                    limit=200,
                    date_window_out=rag_window
                )

                if rag_event_ids is not None:
                    # RAG successful
                    if rag_event_ids:
                        logger.info(f"[{request_id}] RAG search returned {len(rag_event_ids)} event IDs")
                        # Occurrence-Filter und Sortierung auf denselben Zeitraum stellen,
                        # den die RAG-Suche aus der Anfrage gelesen hat. Sonst zeigt eine
                        # Wochenend-Suche die heutigen Termine derselben Events zuerst.
                        if rag_window:
                            date_start, real_date_end = narrow_range_to_rag_window(date_start, real_date_end, rag_window)
                            occurrence_qs, occurrence_subquery = build_occurrence_querysets(date_start, real_date_end)
                            sort_window_start = date_start
                            logger.info(f"[{request_id}] Zeitraum aus der Anfrage uebernommen: {date_start} bis {real_date_end}")

                        # Use rag_queryset (WITHOUT area filter) for result fetching
                        qs = rag_queryset.filter(id__in=rag_event_ids).select_related('source', 'source__organization').prefetch_related(
                            Prefetch('occurrences', queryset=occurrence_qs, to_attr='filtered_occurrences'),
                            'tags',
                            'subtags',
                            'translations'
                        )
                        qs_count = qs.count()
                        logger.info(f"[{request_id}] After filtering by RAG IDs: {qs_count} events found in DB")
                    else:
                        # RAG found nothing
                        qs = EventV4.objects.none()
                        logger.info(f"[{request_id}] RAG search found no matching events")
                else:
                    # RAG failed - Fallback
                    logger.warning(f"[{request_id}] RAG search failed, falling back to traditional search")

                    if getattr(settings, 'RAG_FALLBACK_TO_TRADITIONAL', True):
                        # Traditional search as fallback
                        qs = qs.filter(build_fulltext_filter(search))
                    else:
                        qs = EventV4.objects.none()
            else:
                # ===== TRADITIONAL SEARCH (1-3 Wörter oder RAG disabled) =====
                logger.info(f"[{request_id}] Using traditional search for query: '{search}' ({word_count} words)")
                qs = qs.filter(build_fulltext_filter(search))

                # Fallback zu RAG bei 2-3 Wörtern wenn Volltext nichts findet
                if word_count >= 2 and getattr(settings, 'USE_RAG_EVENT_SEARCH', False):
                    fulltext_count = qs.distinct().count()
                    if fulltext_count == 0:
                        logger.info(f"[{request_id}] Traditional search found 0 results for '{search}' ({word_count} words), falling back to RAG")

                        # Base filters for cache key
                        base_filters = {}
                        if organization_list:
                            base_filters['organization'] = organization_list
                        if town:
                            base_filters['town'] = town
                        if tags_list:
                            base_filters['tags'] = tags_list
                        if subtags_list:
                            base_filters['subtags'] = subtags_list

                        # RAG QuerySet WITHOUT area filter
                        rag_queryset = (
                            EventV4.objects.filter(**base_event_filter)
                            .annotate(has_occurrence=Exists(occurrence_subquery))
                            .filter(has_occurrence=True)
                        )
                        if tags_list:
                            rag_queryset = rag_queryset.filter(tags__in=tags_list)
                        if subtags_list:
                            rag_queryset = rag_queryset.filter(subtags__in=subtags_list)
                        if organization_list:
                            rag_queryset = rag_queryset.filter(source__organization__in=organization_list)
                        if town:
                            rag_queryset = rag_queryset.filter(town__iexact=town)
                        if languages:
                            rag_queryset = rag_queryset.filter(language__in=languages)
                        elif ENGLISCH_SUBTAG_ID not in subtags_list:
                            rag_queryset = rag_queryset.exclude(language="en")

                        rag_window = {}
                        rag_event_ids = perform_rag_search_with_cache(
                            search_query=search,
                            request=self.request,
                            logger=logger,
                            user_areas=user_areas,
                            date_start=date_start,
                            date_end=real_date_end,
                            base_filters=base_filters,
                            category_id=category_id,
                            limit=200,
                            date_window_out=rag_window
                        )

                        if rag_event_ids is not None and rag_event_ids:
                            logger.info(f"[{request_id}] RAG fallback returned {len(rag_event_ids)} event IDs")
                            # Occurrence-Filter und Sortierung auf denselben Zeitraum stellen,
                            # den die RAG-Suche aus der Anfrage gelesen hat. Sonst zeigt eine
                            # Wochenend-Suche die heutigen Termine derselben Events zuerst.
                            if rag_window:
                                date_start, real_date_end = narrow_range_to_rag_window(date_start, real_date_end, rag_window)
                                occurrence_qs, occurrence_subquery = build_occurrence_querysets(date_start, real_date_end)
                                sort_window_start = date_start
                                logger.info(f"[{request_id}] Zeitraum aus der Anfrage uebernommen: {date_start} bis {real_date_end}")

                            qs = rag_queryset.filter(id__in=rag_event_ids).select_related('source', 'source__organization').prefetch_related(
                                Prefetch('occurrences', queryset=occurrence_qs, to_attr='filtered_occurrences'),
                                'tags',
                                'subtags',
                                'translations'
                            )
                            qs_count = qs.count()
                            logger.info(f"[{request_id}] RAG fallback: {qs_count} events found in DB")
                        else:
                            logger.info(f"[{request_id}] RAG fallback also found no results")

        # Apply distinct after all filters
        qs = qs.distinct()

        qs_distinct_count = qs.count()
        logger.info(f"[{request_id}] After distinct: {qs_distinct_count} events")

        # Two-stage query: Get top 200 events based on next occurrence
        from django.db.models import Min

        # Min nur über Occurrences im Abfragezeitraum, sonst sortieren Events
        # mit lange vergangenen ersten Occurrences fälschlich nach vorn und
        # verdrängen zeitnahe Events aus dem 400er-Fenster
        occurrence_range_q = Q(occurrences__start_datetime__lte=real_date_end) & Q(
            occurrences__end_datetime__gte=date_start
        )
        event_basics = list(
            qs.annotate(min_occurrence=Min('occurrences__start_datetime', filter=occurrence_range_q))
            .values('id', 'source__type', 'min_occurrence')
            .order_by('min_occurrence')[:400]  # Top 400 based on next occurrence
        )

        logger.info(f"[{request_id}] After occurrence filtering (Top 400): {len(event_basics)} events")

        if not event_basics:
            events_with_occurrences = []
        else:
            event_ids = [e['id'] for e in event_basics]

            # Full fetch with select_related/prefetch_related
            events_full = (
                EventV4.objects.filter(id__in=event_ids)
                .select_related('source', 'source__organization')
                .prefetch_related(
                    Prefetch('occurrences', queryset=occurrence_qs, to_attr='filtered_occurrences'),
                    'tags',
                    'subtags',
                    'translations'
                )
            )

            # Dict for fast lookup
            events_dict = {e.id: e for e in events_full}

            # Events in correct order (as from stage 1)
            events_with_occurrences = [events_dict[e['id']] for e in event_basics if e['id'] in events_dict]

        # Event expansion by source type
        expanded_events = []
        events_without_occurrences = 0

        for event in events_with_occurrences:
            is_local_source = event.source.type == "local"
            occurrences = event.filtered_occurrences if hasattr(event, 'filtered_occurrences') else []

            if not occurrences:
                # Event has no filtered_occurrences → not shown
                events_without_occurrences += 1
                logger.debug(f"[{request_id}] Event {event.id} ({event.title[:50]}) has no filtered_occurrences in date range")
                continue

            if is_local_source and occurrences:
                # Create separate entry for each occurrence
                for occ in occurrences:
                    event_copy = copy.copy(event)
                    set_event_start_dates(event_copy, occ, now_dt, window_start=sort_window_start)
                    event_copy.end_date = occ.end_datetime
                    expanded_events.append(event_copy)
            else:
                # For non-local events: next occurrence
                if occurrences:
                    next_occ = occurrences[0]  # Already sorted by order_by
                    set_event_start_dates(event, next_occ, now_dt, window_start=sort_window_start)
                    event.end_date = next_occ.end_datetime
                    expanded_events.append(event)

        logger.info(f"[{request_id}] After expansion: {len(expanded_events)} events (dropped {events_without_occurrences} events without occurrences in date range)")

        # Sort by start_date (laufende Occurrences sortieren nach "jetzt")
        expanded_events.sort(key=event_sort_key)

        # Pagination
        page = self.paginate_queryset(expanded_events)

        if page is not None:
            logger.info(f"[{request_id}] After pagination: returning {len(page)} events")
            # Increment request_count async
            event_ids = sorted([event.id for event in page])
            if event_ids:
                update_event_request_counts_async(event_ids)

            serializer = EventBookmarkListSerializer(page, many=True, context={"request": self.request})
            return self.get_paginated_response(serializer.data)
        else:
            logger.info(f"[{request_id}] No pagination: returning all {len(expanded_events)} events")
            serializer = EventBookmarkListSerializer(expanded_events, many=True, context={"request": self.request})
            return Response(serializer.data)
    

    def get_serializer_class(self):
        """
        Overrides the default get_serializer_class method to provide a custom 
        serializer class based on the action.

        If the action is "list", it assigns EventListSerializer to self.serializer_class.

        Returns:
            The serializer class to be used.
        """
        # Convert the action to lowercase for case-insensitive comparison
        action = self.action.lower()
        
        # Check if the action is "list"
        if action == "list":
            # Assign EventListSerializer to self.serializer_class
            self.serializer_class = EventListSerializer
        
        # Call the parent class's get_serializer_class method and return its result
        return super().get_serializer_class()
    

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter('category_id', openapi.IN_QUERY, description="ID of the category", type=openapi.TYPE_INTEGER, required=True),
            openapi.Parameter('ignore_user_selection', openapi.IN_QUERY, description="ignore user tag selection", type=openapi.TYPE_BOOLEAN, required=False, default=False),
            string_parameter("organization", "Organization ID(s) - comma separated", required=False),
            string_parameter("town", "Town", required=False),
            string_parameter("date_start", "start date (YYYY-MM-DD)", required=False),
            string_parameter("date_end", "end date (YYYY-MM-DD)", required=False),
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={
            200: PaginatedEventRetrieveSerializer,
            400: "Missing or invalid parameters",
            404: "Category not found",
        },
        operation_description="Returns all events based on the given filters."
    )
    def list_events_by_category(self, request):

        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return bad_request("Device ID is missing")

        category_id = request.query_params.get('category_id')
        if not category_id:
            return Response({"detail": "category_id missing"}, status=400)

        try:
            category = TagCategories.objects.get(id=category_id)
        except TagCategories.DoesNotExist:
            return Response({"detail": "Category not found"}, status=404)

        category_tag_ids = list(category.tagcategory.values_list("id", flat=True))
        ignore_user_selection = request.query_params.get("ignore_user_selection", "false").lower() in ("1", "true", "yes")
        app_user = get_appuser(device_id)


        if ignore_user_selection or not app_user:
            relevant_tag_ids = category_tag_ids
        else:
            user_tag_ids = set(app_user.tags.values_list("id", flat=True))
            relevant_tag_ids = list(set(category_tag_ids) & user_tag_ids)

        if not relevant_tag_ids:
            empty_queryset = EventV4.objects.none()
            page = self.paginate_queryset(empty_queryset)
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)
            return Response([])

        areas = app_user.areas.all() if app_user else []
        areas = ",".join([str(area.id) for area in areas]) if areas else ""

        organization = request.query_params.get("organization", "")
        organization_list = [int(org) for org in organization.split(",")] if organization else []
        town = request.query_params.get("town", "")
        subtags = request.query_params.get("subtags", "")
        subtags_list = [int(tag) for tag in subtags.split(",")] if subtags else []
        exclude_ids = []

        try:
            queryset_list, exclude_ids, oldest_date = self.get_limited_queryset(
                device_id,
                areas=areas,
                tag_filter=relevant_tag_ids,
                subtag_filter=subtags_list,
                town=town,
                organization_filer=organization_list,
            )
        except ValueError as e:
            return Response(str(e), status=400)

        date_start = request.query_params.get("date_start")
        date_end = request.query_params.get("date_end")
        now = datetime.now(pytz.utc)

        if not date_start:
            date_start = now.date().strftime("%Y-%m-%d")
        try:
            if date_start:
                date_start = datetime.strptime(date_start, "%Y-%m-%d").replace(tzinfo=pytz.utc)
            if date_end:
                date_end = datetime.strptime(date_end, "%Y-%m-%d").replace(tzinfo=pytz.utc)
        except ValueError as e:
            return bad_request(f"Invalid date format: {e}")

        if date_start or date_end:
            filtered_queryset = []
            for event in queryset_list:
                for occurrence in event.occurrences.all():
                    occurrence_start = occurrence.start_datetime.replace(tzinfo=pytz.utc)
                    occurrence_end = occurrence.end_datetime.replace(tzinfo=pytz.utc)
                    if date_start and date_end:
                        if date_start.date() == date_end.date() and date_end.time() == datetime.min.time():
                            date_end = datetime.combine(date_end.date(), datetime.max.time()).replace(tzinfo=pytz.utc)
                        if date_start <= occurrence_end and date_end >= occurrence_start:
                            filtered_queryset.append(event)
                            break
                    elif date_start:
                        if date_start <= occurrence_end:
                            filtered_queryset.append(event)
                            break
                    elif date_end:
                        if date_end >= occurrence_start:
                            filtered_queryset.append(event)
                            break
            queryset_list = filtered_queryset

        queryset_list = sorted(queryset_list, key=event_sort_key)

        # Pagination
        page = self.paginate_queryset(queryset_list)

        if page is not None:
            # request_count asynchron für Events auf dieser Seite erhöhen
            event_ids = sorted([event.id for event in page])
            if event_ids:
                update_event_request_counts_async(event_ids)

            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset_list, many=True)
        return Response(serializer.data)
    

    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
            openapi.Parameter('category_id', openapi.IN_QUERY, description="ID der Kategorie", type=openapi.TYPE_INTEGER, required=True),
            string_parameter("organization", "Organization ID(s) - comma separated", required=False),
            string_parameter("town", "Town", required=False),
            string_parameter("subtags", "Subtag ID(s) - comma separated", required=False),
            string_parameter("date_start", "start date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)", required=False),
            string_parameter("date_end", "end date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)", required=False),
            integer_parameter("limit", "Number of results to return per page."),
            integer_parameter("offset", "The initial index from which to return the results."),
        ],
        responses={
            200: PaginatedEventRetrieveSerializerShort,
            400: "Device ID oder category_id fehlt",
            404: "Kategorie oder AppUser nicht gefunden",
        },
        operation_description="Liefert alle zukünftigen Events"
    )
    def list_future_events_by_category(self, request):
        # Unique Request ID für Logging
        request_id = str(uuid.uuid4())[:8]

        # --- Parameter prüfen ---
        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return Response({"detail": "Device ID is missing."}, status=400)

        category_id = request.query_params.get('category_id')
        if not category_id:
            return Response({"detail": "category_id missing"}, status=400)


        if not TagCategories.objects.filter(id=category_id).exists():
            return Response({"detail": "Category not found"}, status=404)

        app_user = get_appuser(device_id)
        if not app_user:
            return Response({"detail": "App user not found."}, status=404)


        # Areas des Nutzers
        user_areas = list(app_user.areas.values_list("id", flat=True))
        if not user_areas:
            return Response({"detail": "No areas configured for user."}, status=404)
        hidden_tag_ids = Tag.hidden_ids_for_areas(user_areas)

        # Optionalfilter (brauchen wir früher für search-Check)
        organization = request.query_params.get("organization", "")
        organization_list = [int(org) for org in organization.split(",") if org.strip().isdigit()] if organization else []

        town = request.query_params.get("town", "")
        subtags = request.query_params.get("subtags", "")
        subtags_list = [int(tag) for tag in subtags.split(",") if tag.strip().isdigit()] if subtags else []

        # WICHTIG: Bei Suche verwende ALLE Category-Tags, sonst nur User-Tags
        search = request.query_params.get("search")
        if search:
            # MIT Suche: Verwende ALLE Event-Tags der TagCategory
            all_category_tag_ids = list(Tag.objects.filter(
                tagcategories_id=category_id,
                type='event'
            ).exclude(id__in=hidden_tag_ids).values_list('id', flat=True))
            relevant_tag_ids = all_category_tag_ids
            logger.info(f"[{request_id}] Search active: Using ALL {len(relevant_tag_ids)} tags from tagcategory {category_id}")
        else:
            # OHNE Suche: Bevorzuge User-Tags, fallback auf alle Kategorie-Tags
            category_tag_ids = list(Tag.objects.filter(
                tagcategories_id=category_id,
                type='event'
            ).exclude(id__in=hidden_tag_ids).values_list('id', flat=True))
            user_tag_ids = set(app_user.tags.values_list("id", flat=True)) - hidden_tag_ids
            user_selected_in_cat = [t for t in category_tag_ids if t in user_tag_ids]
            # relevant_tag_ids = user_selected_in_cat if user_selected_in_cat else category_tag_ids
            relevant_tag_ids = user_selected_in_cat
            logger.info(f"[{request_id}] No search: Using {len(relevant_tag_ids)} tags from tagcategory {category_id} (user-selected: {len(user_selected_in_cat)}, category total: {len(category_tag_ids)})")


        if not relevant_tag_ids and not search:
            # Ohne Tags UND ohne Suche → leere Antwort
            # Bei aktiver Suche: RAG filtert selbst, braucht keine Tags
            empty_queryset = EventV4.objects.none()
            page = self.paginate_queryset(empty_queryset)
            if page is not None:
                serializer = EventListSerializerShort(page, many=True, context={"request": request})
                return self.get_paginated_response(serializer.data)
            return Response([])

        # Zeitfilter
        date_start_param = request.query_params.get("date_start")
        date_end_param = request.query_params.get("date_end")
        now_dt = timezone.now()

        # Default: Nächste 12 Monate wenn keine Datumsangabe
        if not date_start_param:
            date_start_param = now_dt.date().strftime("%Y-%m-%d")

        if not date_end_param:
            # Setze date_end auf 12 Monate (365 Tage) ab heute
            default_end = (now_dt + timedelta(days=365)).date()
            date_end_param = default_end.strftime("%Y-%m-%d")

        date_start = None
        date_end = None
        
        def parse_datetime(date_string, param_name):
            formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
            for fmt in formats:
                try:
                    dt = datetime.strptime(date_string, fmt)
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt, timezone=pytz.UTC)
                    return dt
                except ValueError:
                    continue
            return None
        
        if date_start_param:
            date_start = parse_datetime(date_start_param, "date_start")
            if date_start is None:
                return Response({"detail": "Invalid date_start format. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS."}, status=400)
        

        if date_end_param:
            date_end = parse_datetime(date_end_param, "date_end")
            if date_end is None:
                return Response({"detail": "Invalid date_end format. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS."}, status=400)
            if date_end.time() == time.min:
                date_end = datetime.combine(date_end.date(), time.max)
                if timezone.is_naive(date_end):
                    date_end = timezone.make_aware(date_end, timezone=pytz.UTC)

        real_date_end = date_end
        if date_start and date_end and date_start.date() == date_end.date() and (date_end.time() == time.min):
            real_date_end = datetime.combine(date_end.date(), time.max)
            if timezone.is_naive(real_date_end):
                real_date_end = timezone.make_aware(real_date_end, timezone=pytz.UTC)


        # Gesetzt, sobald der Zeitraum der Anfrage ("am Wochenende") uebernommen wird:
        # Termine, die vor diesem Zeitpunkt begonnen haben, sortieren nach hinten.
        sort_window_start = None

        # QuerySet aufbauen mit optimiertem Prefetch
        from django.db.models import Prefetch

        # Prefetch nur relevante Occurrences (mit Date-Filter falls vorhanden)
        occurrence_qs = Event_Occurrence.objects.all()

        # Subquery für EXISTS-Check: Nur Events mit mindestens einer passenden Occurrence
        occurrence_subquery = Event_Occurrence.objects.filter(event=OuterRef('pk'))

        if date_start and real_date_end:
            logger.info(f"[{request_id}] Occurrence filter: start_datetime <= {real_date_end} AND end_datetime >= {date_start}")
            occurrence_qs = occurrence_qs.filter(
                Q(start_datetime__lte=real_date_end) & Q(end_datetime__gte=date_start)
            )
            occurrence_subquery = occurrence_subquery.filter(
                Q(start_datetime__lte=real_date_end) & Q(end_datetime__gte=date_start)
            )
        elif date_start:
            logger.info(f"[{request_id}] Occurrence filter: end_datetime >= {date_start}")
            occurrence_qs = occurrence_qs.filter(end_datetime__gte=date_start)
            occurrence_subquery = occurrence_subquery.filter(end_datetime__gte=date_start)
        elif real_date_end:
            logger.info(f"[{request_id}] Occurrence filter: start_datetime <= {real_date_end}")
            occurrence_qs = occurrence_qs.filter(start_datetime__lte=real_date_end)
            occurrence_subquery = occurrence_subquery.filter(start_datetime__lte=real_date_end)
        else:
            logger.info(f"[{request_id}] Occurrence filter: NONE (all occurrences)")

        occurrence_qs = occurrence_qs.order_by('start_datetime')

        qs = (
            EventV4.objects.filter(
                published=True,
                is_past=False,
                tags__in=relevant_tag_ids,
                area__in=user_areas,
            )
            .annotate(has_occurrence=Exists(occurrence_subquery))
            .filter(has_occurrence=True)  # Nur Events mit passenden Occurrences
            .select_related('source', 'source__organization')
            .prefetch_related(
                Prefetch('occurrences', queryset=occurrence_qs, to_attr='filtered_occurrences'),
                'tags',
                'subtags',
                'translations'
            )
        )

        if organization_list:
            qs = qs.filter(source__organization__in=organization_list)
        if subtags_list:
            qs = qs.filter(tags__in=subtags_list)
        if town:
            qs = qs.filter(town__iexact=town)

        # Exclude English events for mobile app requests (Android: ktor-client, iOS: moloApp/molonews)
        user_agent = request.META.get('HTTP_USER_AGENT', '').lower()
        if 'ktor-client' in user_agent or 'moloapp/' in user_agent or 'molonews/' in user_agent:
            qs = qs.exclude(subtags__name='English')
            logger.info(f"[{request_id}] Mobile app detected ({user_agent[:50]}) - excluding English events")

        # ==========================================
        # SEARCH LOGIC - Traditional vs RAG (with Caching)
        # Bei 1-3 Wörtern: Volltextsuche (schnell, zuverlässig)
        # Ab 4 Wörtern: RAG-Suche (semantisch, LLM-basiert)
        # ==========================================
        search = request.query_params.get("search")

        # Ohne Suche: Event erscheint nur in seiner "ersten" TagCategory.
        # "Erste" = niedrigster TagCategories.order unter den Event-Tags,
        # die *dieser User* abonniert hat — sonst würde ein Event, dessen
        # niedrigere-order-Kategorie der User gar nicht abonniert hat,
        # für ihn komplett unsichtbar.
        if not search:
            primary_tagcategory_sq = (
                Tag.objects.filter(
                    eventv4=OuterRef('pk'),
                    type='event',
                    tagcategories__isnull=False,
                    id__in=user_tag_ids,
                )
                .order_by('tagcategories__order', 'tagcategories_id')
                .values('tagcategories_id')[:1]
            )
            qs = qs.annotate(
                primary_tagcategory_id=Subquery(primary_tagcategory_sq)
            ).filter(primary_tagcategory_id=int(category_id))

        if search:
            # Feature Flag prüfen + Wortanzahl
            word_count = len(search.strip().split())
            use_rag = getattr(settings, 'USE_RAG_EVENT_SEARCH', False) and word_count >= 3

            if use_rag:
                # ===== RAG-BASIERTE SUCHE MIT CACHE (ab 4 Wörtern) =====
                logger.info(f"[{request_id}] Using RAG search (cached) for query: '{search}' ({word_count} words)")

                # Base-Filters für Cache-Key sammeln
                base_filters = {}
                if organization_list:
                    base_filters['organization'] = organization_list
                if town:
                    base_filters['town'] = town
                if subtags_list:
                    base_filters['subtags'] = subtags_list

                # Für RAG: Queryset OHNE Area- und Tag-Filter
                # RAG filtert intern nach category_id → kein tags__in Vorfilter nötig
                rag_queryset = (
                    EventV4.objects.filter(
                        published=True,
                        is_past=False,
                    )
                    .annotate(has_occurrence=Exists(occurrence_subquery))
                    .filter(has_occurrence=True)
                )

                if organization_list:
                    rag_queryset = rag_queryset.filter(source__organization__in=organization_list)
                if town:
                    rag_queryset = rag_queryset.filter(town__iexact=town)

                # category_id an RAG übergeben - jede TagCategory hat eigene Event-Tags
                category_id_int = int(category_id)
                logger.info(f"[{request_id}] RAG search with category_id={category_id_int}")

                rag_window = {}
                rag_event_ids = perform_rag_search_with_cache(
                    search_query=search,
                    request=request,
                    logger=logger,
                    user_areas=user_areas,
                    date_start=date_start,
                    date_end=real_date_end,
                    base_filters=base_filters,
                    category_id=category_id_int,
                    limit=200,
                    date_window_out=rag_window
                )

                if rag_event_ids is not None:
                    # RAG erfolgreich
                    if rag_event_ids:
                        # WICHTIG: rag_queryset verwenden (OHNE Area-Filter), nicht qs!
                        # Damit werden auch Events aus Orten gefunden, die in der Anfrage
                        # genannt sind, aber nicht zu den Gebieten des Users gehören
                        logger.info(f"[{request_id}] RAG search returned {len(rag_event_ids)} event IDs: {rag_event_ids[:10]}")

                        # Occurrence-Filter und Sortierung auf denselben Zeitraum stellen,
                        # den die RAG-Suche aus der Anfrage gelesen hat. Sonst zeigt eine
                        # Wochenend-Suche die heutigen Termine derselben Events zuerst.
                        if rag_window:
                            date_start, real_date_end = narrow_range_to_rag_window(date_start, real_date_end, rag_window)
                            occurrence_qs, occurrence_subquery = build_occurrence_querysets(date_start, real_date_end)
                            sort_window_start = date_start
                            logger.info(f"[{request_id}] Zeitraum aus der Anfrage uebernommen: {date_start} bis {real_date_end}")

                        qs = rag_queryset.filter(id__in=rag_event_ids).select_related('source', 'source__organization').prefetch_related(
                            Prefetch('occurrences', queryset=occurrence_qs, to_attr='filtered_occurrences'),
                            'tags',
                            'subtags',
                            'translations'
                        )

                        # Log: Wie viele Events wurden tatsächlich aus DB geladen?
                        qs_count = qs.count()
                        logger.info(f"[{request_id}] After filtering by RAG IDs: {qs_count} events found in DB")
                        if qs_count == 0 and rag_event_ids:
                            # Debug: Why are these IDs not found?
                            test_qs = EventV4.objects.filter(id__in=rag_event_ids[:3], published=True)
                            logger.warning(f"[{request_id}] DEBUG: Test query found {test_qs.count()} of first 3 RAG IDs in EventV4")
                    else:
                        # RAG fand nichts
                        qs = EventV4.objects.none()
                        logger.info(f"[{request_id}] RAG search found no matching events")
                else:
                    # RAG fehlgeschlagen - Fallback
                    logger.warning(f"[{request_id}] RAG search failed, falling back to traditional search")

                    if getattr(settings, 'RAG_FALLBACK_TO_TRADITIONAL', True):
                        # Traditional Search als Fallback
                        qs = qs.filter(build_fulltext_filter(search))
                    else:
                        qs = EventV4.objects.none()
            else:
                # ===== TRADITIONAL SEARCH (1-3 Wörter oder RAG disabled) =====
                logger.info(f"[{request_id}] Using traditional search for query: '{search}' ({word_count} words)")
                qs = qs.filter(build_fulltext_filter(search))

                # Fallback zu RAG bei 2-3 Wörtern wenn Volltext nichts findet
                if word_count >= 2 and getattr(settings, 'USE_RAG_EVENT_SEARCH', False):
                    fulltext_count = qs.distinct().count()
                    if fulltext_count == 0:
                        logger.info(f"[{request_id}] Traditional search found 0 results for '{search}' ({word_count} words), falling back to RAG")

                        base_filters = {}
                        if organization_list:
                            base_filters['organization'] = organization_list
                        if town:
                            base_filters['town'] = town
                        if subtags_list:
                            base_filters['subtags'] = subtags_list

                        rag_queryset = (
                            EventV4.objects.filter(
                                published=True,
                                is_past=False,
                            )
                            .annotate(has_occurrence=Exists(occurrence_subquery))
                            .filter(has_occurrence=True)
                        )
                        if organization_list:
                            rag_queryset = rag_queryset.filter(source__organization__in=organization_list)
                        if town:
                            rag_queryset = rag_queryset.filter(town__iexact=town)

                        category_id_int = int(category_id)
                        rag_window = {}
                        rag_event_ids = perform_rag_search_with_cache(
                            search_query=search,
                            request=request,
                            logger=logger,
                            user_areas=user_areas,
                            date_start=date_start,
                            date_end=real_date_end,
                            base_filters=base_filters,
                            category_id=category_id_int,
                            limit=200,
                            date_window_out=rag_window
                        )

                        if rag_event_ids is not None and rag_event_ids:
                            logger.info(f"[{request_id}] RAG fallback returned {len(rag_event_ids)} event IDs")
                            # Occurrence-Filter und Sortierung auf denselben Zeitraum stellen,
                            # den die RAG-Suche aus der Anfrage gelesen hat. Sonst zeigt eine
                            # Wochenend-Suche die heutigen Termine derselben Events zuerst.
                            if rag_window:
                                date_start, real_date_end = narrow_range_to_rag_window(date_start, real_date_end, rag_window)
                                occurrence_qs, occurrence_subquery = build_occurrence_querysets(date_start, real_date_end)
                                sort_window_start = date_start
                                logger.info(f"[{request_id}] Zeitraum aus der Anfrage uebernommen: {date_start} bis {real_date_end}")

                            qs = rag_queryset.filter(id__in=rag_event_ids).select_related('source', 'source__organization').prefetch_related(
                                Prefetch('occurrences', queryset=occurrence_qs, to_attr='filtered_occurrences'),
                                'tags',
                                'subtags',
                                'translations'
                            )
                            qs_count = qs.count()
                            logger.info(f"[{request_id}] RAG fallback: {qs_count} events found in DB")
                        else:
                            logger.info(f"[{request_id}] RAG fallback also found no results")

        # Nur Events mit passenden Occurrences (schon in SQL gefiltert via EXISTS)
        qs = qs.distinct()

        # Log: Wie viele Events nach distinct?
        qs_distinct_count = qs.count()
        logger.info(f"[{request_id}] After distinct: {qs_distinct_count} events")

        from django.db.models import Min

        # Min nur über Occurrences im Abfragezeitraum, sonst sortieren Events
        # mit lange vergangenen ersten Occurrences fälschlich nach vorn und
        # verdrängen zeitnahe Events aus dem 400er-Fenster
        occurrence_range_q = Q(occurrences__start_datetime__lte=real_date_end) & Q(
            occurrences__end_datetime__gte=date_start
        )
        event_basics = list(
            qs.annotate(min_occurrence=Min('occurrences__start_datetime', filter=occurrence_range_q))
            .values('id', 'source__type', 'min_occurrence')
            .order_by('min_occurrence')[:400]  # Top 400 Events basierend auf nächster Occurrence
        )

        # Log: Wie viele Events haben Occurrences und sind in Top 400?
        logger.info(f"[{request_id}] After occurrence filtering (Top 400): {len(event_basics)} events")
        if not event_basics:
            events_with_occurrences = []
        else:
            event_ids = [e['id'] for e in event_basics]

            events_full = (
                EventV4.objects.filter(id__in=event_ids)
                .select_related('source', 'source__organization')
                .prefetch_related(
                    Prefetch('occurrences', queryset=occurrence_qs, to_attr='filtered_occurrences'),
                    'tags',
                    'subtags',
                    'translations'
                )
            )

            # Dict für schnellen Lookup
            events_dict = {e.id: e for e in events_full}

            # Events in richtiger Reihenfolge (wie stage 1)
            events_with_occurrences = [events_dict[e['id']] for e in event_basics if e['id'] in events_dict]

        # **Expandiere Events - jetzt ohne zusätzliche DB-Queries**
        expanded_events = []
        events_without_occurrences = 0

        for event in events_with_occurrences:
            is_local_source = event.source.type == "local"
            occurrences = event.filtered_occurrences if hasattr(event, 'filtered_occurrences') else []

            if not occurrences:
                # Event hat keine filtered_occurrences → wird nicht angezeigt
                events_without_occurrences += 1
                logger.debug(f"[{request_id}] Event {event.id} ({event.title[:50]}) has no filtered_occurrences in date range")
                continue

            if is_local_source and occurrences:
                # Erstelle für jede Occurrence einen separaten Eintrag
                for occ in occurrences:
                    event_copy = copy.copy(event)
                    set_event_start_dates(event_copy, occ, now_dt, window_start=sort_window_start)
                    event_copy.end_date = occ.end_datetime
                    expanded_events.append(event_copy)
            else:
                # Für Nicht-Local-Events: nächste Occurrence
                if occurrences:
                    next_occ = occurrences[0]  # Schon sortiert durch order_by
                    set_event_start_dates(event, next_occ, now_dt, window_start=sort_window_start)
                    event.end_date = next_occ.end_datetime
                    expanded_events.append(event)

        # Log: Wie viele Events wurden expandiert vs. verworfen?
        logger.info(f"[{request_id}] After expansion: {len(expanded_events)} events (dropped {events_without_occurrences} events without occurrences in date range)")

        # Sortiere nach start_date (sollte schon fast sortiert sein durch Stage 1)
        expanded_events.sort(key=event_sort_key)

        # Pagination
        page = self.paginate_queryset(expanded_events)

        # Log: Pagination-Info
        if page is not None:
            logger.info(f"[{request_id}] After pagination: returning {len(page)} events (page size from paginator)")
        else:
            logger.info(f"[{request_id}] No pagination: returning all {len(expanded_events)} events")

        if page is not None:
            serializer = EventListSerializerShort(page, many=True, context={"request": request})
            return self.get_paginated_response(serializer.data)
        else:
            serializer = EventListSerializerShort(expanded_events, many=True, context={"request": request})
            return Response(serializer.data)
            

    @swagger_auto_schema(
        manual_parameters=[
            # Define a required header parameter for "X-Device-ID"
            header_string_parameter("X-Device-ID", "Device ID", required=True),
            # Define an optional query parameter for "limit"
            integer_parameter("limit", "Number of results to return per page."),
            # Define an optional query parameter for "offset"
            integer_parameter(
                "offset", "The initial index from which to return the results."
            ),
        ],
        responses={
            # Specify the response schema for HTTP 200 status
            200: PaginatedEventRetrieveSerializer,
            # Define response message for HTTP 400 status
            400: "Device ID missing.",
            # Define response message for HTTP 404 status
            404: "No such event.",
        },
    )
    @action(detail=True)
    def similar(self, request, *args, pk=None, **kwargs):
        """
        Returns a list of similar events.
        
        This method handles GET requests to retrieve a list of events that are similar to a specific event identified by its primary key (pk).
        
        The method uses Swagger to document the API, including:
        - A required header parameter for the device ID (X-Device-ID).
        - Optional query parameters for pagination (limit and offset).
        - Possible response schemas and messages for various HTTP status codes (200, 400, 404).
        """
        # Call the parent class's similar method to handle the request and return a response
        return super().similar(request, *args, pk, **kwargs)


     # define a swagger auto schema for a delete method and also the delete method   
    @swagger_auto_schema(
        operation_description="Delete an Event",
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        responses={
            204: "Deleted",
            400: "Invalid Device ID",
            404: "Event not found",
        },
    )
    def destroy(self, request, *args, **kwargs):
        """
        Delete an event.

        Args:
            request (Request): The request object.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            Response: The response object.
        """
         # Handle DELETE request logic here
        # Get the id from the last section of the URL in the request metapass
        url_parts = self.request.META['PATH_INFO'].split('/')
        event_id = url_parts[-2]
        # Retrieve the event
        event = get_object_or_404(EventV4, id=event_id)
        # if event does not exist return 404
        if not event:
            return Response(status=status.HTTP_404_NOT_FOUND)
        
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

        # Check if the user is allowed to update the event
        # Inhalte anlegen/ändern/löschen nur mit Redaktionsrolle (content.permissions)
        if not has_content_role(user):
            return Response({"detail": "Account has no content role."}, status=status.HTTP_403_FORBIDDEN)
        source = related_source(user)
        if event.source != source:
            return Response({"detail": "User not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)

        if user.is_authenticated:
            # Delete the event
            event.delete()
        else:
            return Response({"detail": "User not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)

        # Return a response with status code 204
        return Response(status=status.HTTP_204_NO_CONTENT)

    
    @swagger_auto_schema(
        operation_description="Create a new Event",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'title': openapi.Schema(type=openapi.TYPE_STRING),
                'content': openapi.Schema(type=openapi.TYPE_STRING),
                'link': openapi.Schema(type=openapi.TYPE_STRING),
                'ticket_url': openapi.Schema(type=openapi.TYPE_STRING),
                'street': openapi.Schema(type=openapi.TYPE_STRING),
                'town': openapi.Schema(type=openapi.TYPE_STRING),
                'zip_code': openapi.Schema(type=openapi.TYPE_STRING),
                'image_url': openapi.Schema(type=openapi.TYPE_STRING),
                'event_location': openapi.Schema(type=openapi.TYPE_STRING),
                'longitude': openapi.Schema(type=openapi.TYPE_NUMBER),
                'latitude': openapi.Schema(type=openapi.TYPE_NUMBER),
                'image_source': openapi.Schema(type=openapi.TYPE_STRING),
                'published': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                'occurrences': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(
                        type=openapi.TYPE_OBJECT,
                        properties={
                            'event_start_date': openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_DATETIME),
                            'event_end_date': openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_DATETIME),
                        }
                    )
                ),
                'event_tags': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER)
                ),
            },
            required=['title', 'content', 'event_tags']
        ),
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        responses={
            201: openapi.Response(
                description="Event created",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'id': openapi.Schema(type=openapi.TYPE_INTEGER)
                    }
                )
            ),
            400: "Bad Request",
            409: "Event already exists (duplicate)"
        }
    )
    def create(self, request, *args, **kwargs):
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

        if user.is_authenticated:
            # *** DUPLIKAT-SCHUTZ: Prüfung auf bereits existierendes Event ***
            title = request.data.get('title', '').strip()
            content = request.data.get('content', '').strip()
            street = request.data.get('street', '').strip()
            
            if not title or not content:
                return Response({"detail": "Title and content are required."}, status=status.HTTP_400_BAD_REQUEST)

            # Inhalte anlegen/ändern/löschen nur mit Redaktionsrolle (content.permissions)
            if not has_content_role(user):
                return Response({"detail": "Account has no content role."}, status=status.HTTP_403_FORBIDDEN)
            source = related_source(user)
            if source is None:
                return Response({"detail": "No source is linked to this account."}, status=status.HTTP_400_BAD_REQUEST)

            # Prüfe auf Duplikat basierend auf Titel, Inhalt, Straße und Quelle
            # Zeitfenster: Letzten 10 Minuten (um schnelle Doppelklicks zu verhindern)
            ten_minutes_ago = timezone.now() - timedelta(minutes=10)
            
            existing_event = EventV4.objects.filter(
                title__iexact=title,
                content=content,
                street__iexact=street,
                source=source,
                created_at__gte=ten_minutes_ago  # Annahme: created_at Feld existiert
            ).first()
            
            # Falls kein created_at Feld vorhanden ist, alternative Prüfung:
            if not hasattr(EventV4, 'created_at'):
                existing_event = EventV4.objects.filter(
                    title__iexact=title,
                    content=content,
                    street__iexact=street,
                    source=source
                ).order_by('-id').first()
                
                # Zusätzliche Prüfung: Wenn Event in den letzten paar IDs gefunden wurde
                if existing_event and existing_event.id > (EventV4.objects.aggregate(max_id=models.Max('id'))['max_id'] - 50):
                    logger.warning(f"Potential duplicate event detected: {title} by user {user_id}")
                    return Response(
                        {"detail": "Event already exists. Please check your recent events.", "existing_event_id": existing_event.id}, 
                        status=status.HTTP_409_CONFLICT
                    )

            if existing_event:
                logger.warning(f"Duplicate event prevented: {title} by user {user_id}")
                return Response(
                    {"detail": "Event already exists. Please check your recent events.", "existing_event_id": existing_event.id}, 
                    status=status.HTTP_409_CONFLICT
                )

            # *** ENDE DUPLIKAT-SCHUTZ ***

            for link_field in ('link', 'ticket_url'):
                if not is_safe_link(request.data.get(link_field)):
                    return Response({"detail": f"Invalid {link_field}."}, status=status.HTTP_400_BAD_REQUEST)

            # Handle POST request logic here
            event = EventV4()
            event.image_source = user.username
            event.link = ""

            event.title = title
            event.content = content

            if 'ticket_url' in request.data:
                event.ticket_url = request.data.get('ticket_url', '').strip()

            if 'link' in request.data:
                event.link = request.data['link']

            event.street = street
            
            if 'town' in request.data:
                event.town = request.data['town']
            if 'zip_code' in request.data:
                event.zip_code = request.data['zip_code']
            if 'image_url' in request.data:
                event.image_url = request.data['image_url']
            if 'event_location' in request.data:
                event.event_location = request.data['event_location']
            if 'longitude' in request.data:
                event.longitude = float(request.data['longitude'])
            if 'latitude' in request.data:
                event.latitude = float(request.data['latitude'])
            if 'image_source' in request.data:
                event.image_source = request.data['image_source']

            event.source = source
            # Veröffentlichen nur mit Veröffentlichungsrecht, sonst zur Prüfung einreichen
            event.published = bool(request.data.get('published', True)) and can_publish(user)
            event.up_for_review = not event.published

            # Termine und Themen vor dem Speichern prüfen, damit keine halben Events entstehen
            occurrences = request.data.get('occurrences')
            if not occurrences or not isinstance(occurrences, list) or not all(
                isinstance(o, dict) and o.get('event_start_date') and o.get('event_end_date')
                for o in occurrences
            ):
                return Response({"detail": "Occurrences are required."}, status=status.HTTP_400_BAD_REQUEST)
            tag_ids = request.data.get('event_tags')
            if not isinstance(tag_ids, list):
                return Response({"detail": "event_tags is required."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                tags = list(Tag.objects.filter(pk__in=tag_ids))
            except (TypeError, ValueError):
                return Response({"detail": "Invalid event_tags."}, status=status.HTTP_400_BAD_REQUEST)
            if len(tags) != len(set(tag_ids)):
                return Response({"detail": "Unknown event_tags ID."}, status=status.HTTP_400_BAD_REQUEST)

            with transaction.atomic():
                event.save()

                for occurrence in occurrences:
                    event_occurrence = Event_Occurrence()
                    event_occurrence.event = event
                    event_occurrence.start_datetime = occurrence['event_start_date']
                    event_occurrence.end_datetime = occurrence['event_end_date']
                    event_occurrence.save()

                # Startdatum des Events = frühester Termin
                first_occurrence = min(occurrences, key=lambda x: x['event_start_date'])
                event.start_date = first_occurrence['event_start_date']
                event.save()

            event.area.clear()

            # Area via Town/PLZ/Koord suchen (mit Distanz-Plausibilisierung bei vorhandenen Koord)
            area, _ = get_or_create_area(
                event.town,
                zip_code=event.zip_code,
                latitude=event.latitude,
                longitude=event.longitude,
                create_if_not_found=False,
            )
            if area:
                event.area.add(area)
            else:
                # Fallback: use source areas if no match for town
                for a in source.area.all():
                    event.area.add(a)

            event.tags.add(*tags)

            # Return a 201 Created response with the event id
            return Response({"id": event.id}, status=status.HTTP_201_CREATED)
        else:
            return Response({"detail": "User not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)

    @swagger_auto_schema(
        operation_description="Update an event",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'title': openapi.Schema(type=openapi.TYPE_STRING),
                'content': openapi.Schema(type=openapi.TYPE_STRING),
                'link': openapi.Schema(type=openapi.TYPE_STRING),
                'ticket_url': openapi.Schema(type=openapi.TYPE_STRING),
                "date": openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE),
                'street': openapi.Schema(type=openapi.TYPE_STRING),
                'town': openapi.Schema(type=openapi.TYPE_STRING),
                'zip_code': openapi.Schema(type=openapi.TYPE_STRING),
                'image_url': openapi.Schema(type=openapi.TYPE_STRING),
                'event_location': openapi.Schema(type=openapi.TYPE_STRING),
                'longitude': openapi.Schema(type=openapi.TYPE_NUMBER),
                'latitude': openapi.Schema(type=openapi.TYPE_NUMBER),
                'image_source': openapi.Schema(type=openapi.TYPE_STRING),
                'published': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                'occurrences': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(
                        type=openapi.TYPE_OBJECT,
                        properties={
                            'event_start_date': openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_DATETIME),
                            'event_end_date': openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_DATETIME),
                        }
                    )
                ),
                'area': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER)
                ),
                'event_tags': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER)
                ),
            },
            #required=['title', 'content', 'link', 'date', 'zip_code', 'event_location', 'area', 'event_tags']
        ),
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        responses={
            204: "Event updated",
            400: "Bad Request",
            404: "Event not found"
        }
    )
    def update(self, request, *args, **kwargs):
        """
        Update an event.

        Args:
            request (Request): The request object.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            Response: The response object.
        """

        # Get the id from the last section of the URL in the request metapass
        url_parts = self.request.META['PATH_INFO'].split('/')
        event_id = url_parts[-2]

        # Retrieve the event
        event = get_object_or_404(EventV4, id=event_id)
        
        # Check if event exists
        if not event:
            return Response(status=status.HTTP_404_NOT_FOUND)

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

        # Check if the user is allowed to update the event
        # Inhalte anlegen/ändern/löschen nur mit Redaktionsrolle (content.permissions)
        if not has_content_role(user):
            return Response({"detail": "Account has no content role."}, status=status.HTTP_403_FORBIDDEN)
        source = related_source(user)
        if event.source != source:
            return Response({"detail": "User not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)

        if user.is_authenticated:

            for link_field in ('link', 'ticket_url'):
                if not is_safe_link(request.data.get(link_field)):
                    return Response({"detail": f"Invalid {link_field}."}, status=status.HTTP_400_BAD_REQUEST)

            # Ohne Veröffentlichungsrecht gehen Änderungen zur Prüfung
            # (auch bei bereits veröffentlichten Events)
            if not can_publish(user):
                event.published = False
                event.up_for_review = True

            # Update the event
            event.link = ""
            for field, value in request.data.items():
                if field == 'title' and value:
                    event.title = value
                elif field == 'content' and value:
                    event.content = value
                elif field == 'link' and value:
                    event.link = value
                elif field == 'ticket_url' and value:
                    event.ticket_url = value
                elif field == 'date' and value:
                    event.date = value
                elif field == 'street' and value:
                    event.street = value
                elif field == 'town' and value:
                    event.town = value
                elif field == 'zip_code' and value:
                    event.zip_code = value
                elif field == 'image_url' and value:
                    event.image_url = value
                elif field == 'event_location' and value:
                    event.event_location = value
                elif field == 'longitude' and value:
                    event.longitude = float(value)
                elif field == 'latitude' and value:
                    event.latitude = float(value)
                elif field == 'image_source' and value:
                    event.image_source = value
                elif field == 'published' and value is not None:
                    event.published = bool(value) and can_publish(user)
                elif field == 'occurrences' and value:
                    # Get the occurrences information for the event
                    occurrences = value
                    # Remove all occurrences from the event
                    event.occurrences.all().delete()
                    # Add the new occurrences to the event
                    for occurrence in occurrences:
                        event_occurrence = Event_Occurrence()
                        event_occurrence.event = event
                        event_occurrence.start_datetime = occurrence['event_start_date']
                        event_occurrence.end_datetime = occurrence['event_end_date']
                        event_occurrence.save()

                    # Get the first occurrence from the occurrences list
                    first_occurrence = min(occurrences, key=lambda x: x['event_start_date'])
                    # Set the start date of the event to the start date of the first occurrence
                    event.start_date = first_occurrence['event_start_date']
                elif field == 'area':
                    # Ignore this field
                    pass
                    # Remove all areas from the event
                    # event.area.clear()
                    # Add the new areas to the event
                    # for area in value:
                    #    event.area.add(area)               
                elif field == 'event_tags':
                    # Remove all tags from the event
                    event.tags.clear()
                    # Add the new tags to the event
                    for tag in value:
                        event.tags.add(tag)
                else:
                    logger.error(f"Unknown field: {field}")


                # add the area
                # clear the area old list
                event.area.clear()

                # Area via Town/PLZ/Koord suchen (mit Distanz-Plausibilisierung bei vorhandenen Koord)
                area, _ = get_or_create_area(
                    event.town,
                    zip_code=event.zip_code,
                    latitude=event.latitude,
                    longitude=event.longitude,
                    create_if_not_found=False,
                )
                if area:
                    event.area.add(area)

                # save the event
                event.save()
        else: 
            return Response({"detail": "User not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)
        
        # Return a 204 No Content response
        return Response(status=status.HTTP_204_NO_CONTENT)


    @swagger_auto_schema(
        manual_parameters=[
            # Define a required header parameter for "X-Device-ID"
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={
            # Specify the response schema for HTTP 200 status
            200: EventRetrieveSerializer,
            # Define response message for HTTP 400 status
            400: "Device ID missing.",
            # Define response message for HTTP 404 status
            404: "No such event.",
        },
    )

    @action(detail=True, methods=["get"], url_path="retrieve")
    def retrieve_event(self, request, pk=None):
        """
        Retrieves a single event by its ID.
        """
        logger.info("Retrieve Event called")
        # Get the device ID from the request headers
        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return Response({"detail": "Device ID is required."}, status=status.HTTP_400_BAD_REQUEST)
        
        # Try to get the event by its ID
        event = get_object_or_404(EventV4, pk=pk, published=True)
        
        # Get the app user based on the device ID
        app_user = get_appuser(device_id)
        
        # Check if app_user exists
        if not app_user:
            return Response("Bad Request", status=status.HTTP_400_BAD_REQUEST)
        
        # Annotate the event with bookmarked and archived status
        event.bookmarked = app_user.bookmarked_events_v4.filter(pk=event.pk).exists()
        event.archived = app_user.archived_events_v4.filter(pk=event.pk).exists()

        serializer = EventRetrieveSerializer(event, context={"request": request})

        user_agent = request.META.get('HTTP_USER_AGENT', '').lower()
        logger.debug(f"User-Agent: {user_agent}")
        if 'molo/5.1.2' in user_agent:
            serializer = EventRetrieveSerializerOldArea(event, context={"request": request})

        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description=(
            "Preview-Endpoint für die Web-Einbindung. Liefert NICHT veröffentlichte Events "
            "OHNE Tags (published=False, keine Tags). Diese Events erscheinen NICHT im normalen "
            "App-Feed (der nur published=True ausliefert) und lassen sich so exklusiv auf einer "
            "Webseite einbinden. Es wird KEIN X-Device-ID benötigt. "
            "Sobald ein Event veröffentlicht wird (published=True) oder Tags bekommt, fällt es "
            "aus diesem Endpoint heraus und erscheint im normalen Feed."
        ),
        manual_parameters=[
            openapi.Parameter("ids", openapi.IN_QUERY, description="Komma-getrennte Event-IDs (optional)", type=openapi.TYPE_STRING),
            openapi.Parameter("source", openapi.IN_QUERY, description="Komma-getrennte Source-IDs (optional)", type=openapi.TYPE_STRING),
            openapi.Parameter("town", openapi.IN_QUERY, description="Ort, exakte Übereinstimmung (optional)", type=openapi.TYPE_STRING),
            openapi.Parameter("date_start", openapi.IN_QUERY, description="Frühestes Occurrence-Datum YYYY-MM-DD (optional)", type=openapi.TYPE_STRING),
            openapi.Parameter("date_end", openapi.IN_QUERY, description="Spätestes Occurrence-Datum YYYY-MM-DD (optional)", type=openapi.TYPE_STRING),
            openapi.Parameter("include_past", openapi.IN_QUERY, description="Vergangene Events einschließen (default: false)", type=openapi.TYPE_BOOLEAN),
            openapi.Parameter("limit", openapi.IN_QUERY, description="Max. Anzahl (Pagination, default 20)", type=openapi.TYPE_INTEGER),
            openapi.Parameter("offset", openapi.IN_QUERY, description="Offset (Pagination)", type=openapi.TYPE_INTEGER),
        ],
        responses={200: EventListSerializer(many=True)},
    )
    @action(detail=False, methods=["get"], url_path="preview")
    def preview(self, request):
        """
        Liefert nicht veröffentlichte Events ohne Tags für die exklusive Web-Einbindung.
        """
        request_id = str(uuid.uuid4())[:8]
        logger.info(f"[{request_id}] GET /events/preview/")

        # Nur unveröffentlichte, eingereichte Events ohne Tags aus Quellen, die
        # ausdrücklich für die Vorschau freigegeben sind (EVENT_PREVIEW_SOURCE_IDS).
        # Entwürfe erscheinen nie.
        qs = (
            EventV4.objects.filter(
                published=False,
                draft=False,
                tags__isnull=True,
                source_id__in=settings.EVENT_PREVIEW_SOURCE_IDS,
            )
            .select_related("source", "source__organization")
            .prefetch_related("occurrences", "tags", "subtags")
        )

        # Optionale Filter
        ids = request.query_params.get("ids", "")
        id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()] if ids else []
        if id_list:
            qs = qs.filter(id__in=id_list)

        source = request.query_params.get("source", "")
        source_list = [int(s) for s in source.split(",") if s.strip().isdigit()] if source else []
        if source_list:
            qs = qs.filter(source__in=source_list)

        town = request.query_params.get("town", "")
        if town:
            qs = qs.filter(town__iexact=town)

        include_past = request.query_params.get("include_past", "false").lower() == "true"
        if not include_past:
            qs = qs.filter(is_past=False)

        # Datumsbereich über die Occurrences
        def parse_preview_date(value):
            try:
                return datetime.strptime(value, "%Y-%m-%d")
            except (ValueError, TypeError):
                return None

        date_start = parse_preview_date(request.query_params.get("date_start"))
        date_end = parse_preview_date(request.query_params.get("date_end"))
        if date_start:
            qs = qs.filter(occurrences__start_datetime__gte=date_start)
        if date_end:
            qs = qs.filter(occurrences__start_datetime__lte=date_end.replace(hour=23, minute=59, second=59))

        qs = qs.distinct().order_by("start_date")

        logger.info(f"[{request_id}] preview events found: {qs.count()}")

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = EventListSerializer(page, many=True, context={"request": request})
            return self.get_paginated_response(serializer.data)

        serializer = EventListSerializer(qs, many=True, context={"request": request})
        return Response(serializer.data)


class CombinedViewSetEvent(BaseViewSet):
    # Define the queryset to fetch only published EventV4 objects
    queryset = EventV4.objects.filter(published=True)
    # Specify the serializer class to be used
    serializer_class = EventListSerializer
    # Define the model class associated with this view set
    model_class = EventV4
    # Specify the filter backends to be used for filtering the queryset
    filter_backends = BaseViewSet.filter_backends + [
        DeviceIdFilter,
    ]

    def get_user_events(self, user):
        """
        Get events for a specific user.

        Args:
            user (User): User object

        Returns:
            queryset: Filtered events for the user
        """
        # Filter the events based on the user's areas and annotate the count of bookmarked events
        events = self.queryset.annotate(
            bookmarked_count=Count('events_bookmarked')
        )
       
        # Initialize an empty list to store filtered events
        event_list = []

        # Iterate over each event in the events list
        for event in events:
            # check if the source belongs to the user
            if event.source.related_user != user:
                continue
            # Add all occurrences to the event
            event_occurrences = Event_Occurrence.objects.filter(event=event)   
            # Add a new field to the event object
            event.occurrences_list = list(event_occurrences)
            # Add the event to the event list
            event_list.append(event)

            # Sort the event list by start date using a lambda function
            event_list.sort(key=lambda event: event.start_date, reverse=True)

        return event_list

    @swagger_auto_schema(
        operation_description="List events for a specific user.",
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        responses={
            200: PaginatedEventRetrieveSerializer,  # Successful response
            400: "Bad Request",  # Bad request error
            401: "Unauthorized",  # Unauthorized error
            404: "No events found for the user",  # No events found error
        },
    )
    @action(detail=False, methods=['get'], url_path='user-events')
    def list_user_events(self, request, *args, **kwargs):
        """
        List events for a specific user.
        """
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return Response({"detail": "Authorization header missing."}, status=status.HTTP_401_UNAUTHORIZED)

        token = auth_header

        try:
            # Decode the JWT token
            payload = decode_access_token(token)
            user_id = payload.get('user_id')
            if not user_id:
                return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)
            user = get_object_or_404(User, id=user_id)
        except jwt.ExpiredSignatureError:
            return Response({"detail": "Token has expired."}, status=status.HTTP_401_UNAUTHORIZED)
        except jwt.InvalidTokenError:
            return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)

        # Extract search parameter
        search = request.query_params.get("search")

        if user.is_authenticated:
            # Optimierte Queryset mit allen notwendigen select_related und prefetch_related
            queryset = EventV4.objects.filter(
                source__related_user=user  # Filter direkt anwenden
            ).select_related(
                'source',  # N+1 Problem für Source lösen
                'source__organization'  # Organization auch vorladen
            ).prefetch_related(
                'area',  # M2M Feld für Areas
                'tags',  # M2M Feld für Tags
                'events_bookmarked',  # Bookmarks vorladen
                'occurrences'  # Event Occurrences vorladen
            ).annotate(
                bookmarked_count=Count('events_bookmarked')
            ).order_by('-start_date')

            # ==========================================
            # SEARCH LOGIC - Traditional vs RAG
            # Bei 1-3 Wörtern: Volltextsuche (schnell, zuverlässig)
            # Ab 4 Wörtern: RAG-Suche (semantisch, LLM-basiert)
            # ==========================================
            if search:
                # Feature Flag check + Wortanzahl
                word_count = len(search.strip().split())
                use_rag = getattr(settings, 'USE_RAG_EVENT_SEARCH', False) and word_count >= 3

                if use_rag:
                    # ===== RAG-BASED SEARCH WITH CACHE (ab 4 Wörtern) =====
                    request_id = str(uuid.uuid4())[:8]
                    logger.info(f"[{request_id}] Using RAG search for user events: '{search}' ({word_count} words)")

                    # Base filters for cache key
                    base_filters = {'user_id': user.id}

                    # User areas
                    user_areas = list(queryset.values_list('area__id', flat=True).distinct())

                    # Create RAG queryset (similar to base but for RAG)
                    rag_queryset = EventV4.objects.filter(
                        source__related_user=user,
                        published=True,
                        is_past=False
                    )

                    # Perform RAG search with cache
                    rag_event_ids = perform_rag_search_with_cache(
                        search_query=search,
                        request=request,
                        logger=logger,
                        user_areas=user_areas,
                        date_start=None,
                        date_end=None,
                        base_filters=base_filters,
                        category_id=None,
                        limit=None
                    )

                    if rag_event_ids is not None:
                        # RAG successful
                        if rag_event_ids:
                            logger.info(f"[{request_id}] RAG returned {len(rag_event_ids)} event IDs")
                            queryset = queryset.filter(id__in=rag_event_ids)
                        else:
                            # RAG found nothing
                            queryset = EventV4.objects.none()
                            logger.info(f"[{request_id}] RAG found no matching events")
                    else:
                        # RAG failed - Fallback
                        logger.warning(f"[{request_id}] RAG failed, checking fallback setting")

                        if getattr(settings, 'RAG_FALLBACK_TO_TRADITIONAL', True):
                            logger.info(f"[{request_id}] Using traditional search fallback")
                            queryset = queryset.filter(build_fulltext_filter(search))
                        else:
                            queryset = EventV4.objects.none()
                else:
                    # ===== TRADITIONAL SEARCH (1-3 Wörter oder RAG disabled) =====
                    logger.info(f"Using traditional search for query: '{search}' ({word_count} words)")
                    queryset = queryset.filter(build_fulltext_filter(search))

                    # Fallback zu RAG bei 2-3 Wörtern wenn Volltext nichts findet
                    if word_count >= 2 and getattr(settings, 'USE_RAG_EVENT_SEARCH', False):
                        fulltext_count = queryset.distinct().count()
                        if fulltext_count == 0:
                            logger.info(f"[{request_id}] Traditional search found 0 results for '{search}' ({word_count} words), falling back to RAG")

                            base_filters = {'user_id': user.id}
                            user_areas_fallback = list(queryset.values_list('area__id', flat=True).distinct()) if queryset.exists() else []

                            rag_queryset = EventV4.objects.filter(
                                source__related_user=user,
                                published=True,
                                is_past=False
                            )

                            rag_event_ids = perform_rag_search_with_cache(
                                search_query=search,
                                request=request,
                                logger=logger,
                                user_areas=user_areas_fallback,
                                date_start=None,
                                date_end=None,
                                base_filters=base_filters,
                                category_id=None,
                                limit=None
                            )

                            if rag_event_ids is not None and rag_event_ids:
                                logger.info(f"[{request_id}] RAG fallback returned {len(rag_event_ids)} event IDs")
                                queryset = rag_queryset.filter(id__in=rag_event_ids)
                                logger.info(f"[{request_id}] RAG fallback: {queryset.count()} events found in DB")
                            else:
                                logger.info(f"[{request_id}] RAG fallback also found no results")

                # Make distinct after filters
                queryset = queryset.distinct()

            # Paginate the queryset if pagination is set up
            page = self.paginate_queryset(queryset)
            if page is not None:
                # If pagination is applied, serialize the paginated data
                serializer = self.get_serializer(page, many=True)
                # Return the paginated response
                return self.get_paginated_response(serializer.data)
            
            # If no pagination is applied, serialize the full queryset
            serializer = self.get_serializer(queryset, many=True)
            # Return the full response
            return Response(serializer.data)
        else:
            return Response({"detail": "User not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)



# Define a class `EventAppUserBaseViewSet` that inherits from `AppUserBaseViewSet`
class EventAppUserBaseViewSet(AppUserBaseViewSet):

    # Set the queryset to retrieve all objects of `EventV4`
    queryset = EventV4.objects.all()
    
    # Set the serializer class to `EventListSerializer` for serializing the retrieved objects
    serializer_class = EventListSerializer
    
    # Set the model class to `EventV4`, indicating the type of model being used
    model_class = EventV4
    
    # Extend the filter backends from `AppUserBaseViewSet` with an additional filter `DeviceIdFilter`
    filter_backends = AppUserBaseViewSet.filter_backends + [
        DeviceIdFilter,
    ]


class EventArchiveViewSet(GenericViewSet):
    queryset = EventV4.objects.filter(published=True)
    serializer_class = EventListSerializer
    model_class = EventV4
    filter_backends = [DeviceIdFilter, SourceActiveFilter]
    pagination_class = EventPagination

    # Name of the many-to-many field for archived events
    m2m_field_name = "archived_events"

 
    @swagger_auto_schema(
        operation_description="List Archived events",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={
            200: PaginatedEventRetrieveSerializer,
            400: "Invalid Device ID",
        },
    )
    def list(self, request, *args, **kwargs):
        """
        Lists the events archived by the user.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
        Returns:
            A paginated list of archived events.
        """

        device_id = request.headers.get('X-Device-ID')
        if not device_id:
            return Response({"detail": "Device ID is required."}, status=status.HTTP_400_BAD_REQUEST)

        app_user = get_appuser(device_id)
        if not app_user:
            return Response({"detail": "App user not found."}, status=status.HTTP_400_BAD_REQUEST)

        # Retrieve archived events
        archived_events = app_user.archived_events_v4.all()

        # Make a queryset out of the archived events
        queryset = EventV4.objects.filter(id__in=[event.id for event in archived_events])

        # Paginate the queryset
        page = self.paginate_queryset(queryset)
        if page is not None:
            # If pagination is applied, serialize the paginated data
            serializer = self.get_serializer(page, many=True)
            # Return the paginated response
            return self.get_paginated_response(serializer.data)

        # If no pagination is applied, serialize the full queryset
        serializer = self.get_serializer(queryset, many=True)
        # Return the full response
        return Response(serializer.data)
        

    @swagger_auto_schema(
        operation_description="Add events to archive",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={},
            required=[],
            description='No data is required in the request body'
        ),
        responses={
            204: "Added to archive",
            400: "Invalid Device ID",
            404: "Event not found",
        },
        required=False  # Set the request body as not required
    )
    def update(self, request, *args, **kwargs):
        """
        Adds an event to the user's archive.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
            **kwargs: Additional keyword arguments.
        
        Returns:
            A response indicating the event was added to archive.
        """
        event_id = kwargs.get('pk')
        app_user = get_appuser(request.META.get('HTTP_X_DEVICE_ID'))
        if not app_user:
            return Response("Invalid Device ID", status=status.HTTP_400_BAD_REQUEST)
        
        try:
            event = EventV4.objects.get(id=event_id)
        except EventV4.DoesNotExist:
            return Response("Event not found", status=status.HTTP_404_NOT_FOUND)

        # if event not found return 404
        if not event:
            return Response(status=status.HTTP_404_NOT_FOUND)
        app_user.archived_events_v4.add(event)
        return Response(status=status.HTTP_204_NO_CONTENT)
    
    @swagger_auto_schema(
        operation_description="Remove event from archive",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        responses={
            204: "Removed from archive",
            400: "Invalid Device ID",
            404: "Event not found",
        },
    )
    def destroy(self, request, *args, **kwargs):
        """
        Removes an event from the user's archive.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
            **kwargs: Additional keyword arguments.
        
        Returns:
            A response indicating the event was removed from archive.
        """
        event_id = kwargs.get('pk')
        event = get_object_or_404(EventV4, id=event_id)
        device_id = request.META.get("HTTP_X_DEVICE_ID")
        app_user = get_appuser(device_id)
        
        if not app_user:
            return Response("Invalid Device ID", status=status.HTTP_400_BAD_REQUEST)
        
        if event not in app_user.archived_events_v4.all():
            return Response("Event not found", status=status.HTTP_404_NOT_FOUND)
        
        app_user.archived_events_v4.remove(event)
        return Response(status=status.HTTP_204_NO_CONTENT)
        
# Define an empty serializer
class EmptySerializer(serializers.Serializer):
    pass

class EventsBookmarksViewSet(GenericViewSet):
    queryset = EventV4.objects.filter(published=True)
    serializer_class = EventBookmarkListSerializer
    model_class = EventV4
    filter_backends = [DeviceIdFilter, SourceActiveFilter]
    pagination_class = EventPagination
    m2m_field_name = "bookmarked_events_v4"

    def get_serializer_class(self):
        # Check if the view is being called for Swagger schema generation
        if self.action == 'list':
            return EventBookmarkListSerializer
        if self.action == 'update':
            return None

        if getattr(self, 'swagger_fake_view', False):
            # If the request is made by Swagger for schema generation, return the default serializer class
            return EventBookmarkListSerializer
       
        return super().get_serializer_class()


    @swagger_auto_schema(
        operation_description="List bookmarked events",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        
        responses={
            200: PaginatedEventRetrieveSerializer,
            400: "Invalid Device ID",
        },
    )
    def list(self, request, *args, **kwargs):
        """
        Lists the events bookmarked by the user.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
        Returns:
            A paginated list of bookmarked events.
        """

        device_id = request.headers.get('X-Device-ID')
        if not device_id:
            return Response({"detail": "Device ID is required."}, status=status.HTTP_400_BAD_REQUEST)

        app_user = get_appuser(device_id)
        if not app_user:
            return Response({"detail": "App user not found."}, status=status.HTTP_400_BAD_REQUEST)

        # Retrieve bookmarked events
        bookmarked_events = app_user.bookmarked_events_v4.all()

        #logger.error(f"Bookmarked events: {bookmarked_events}")

        # Make a queryset out of the bookmarked events
        queryset = EventV4.objects.filter(id__in=[event.id for event in bookmarked_events], is_past=False).order_by('start_date')

        # Paginate the queryset
        page = self.paginate_queryset(queryset)
        if page is not None:
            # If pagination is applied, serialize the paginated data
            serializer = self.get_serializer(page, many=True)
            # Return the paginated response
            return self.get_paginated_response(serializer.data)

        # If no pagination is applied, serialize the full queryset
        serializer = self.get_serializer(queryset, many=True)
        # Return the full response
        return Response(serializer.data)
        

    @swagger_auto_schema(
        operation_description="Add events to bookmarks",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        request_body=None,
        responses={
            201: "Added to bookmarks",
            400: "Invalid Device ID",
            404: "Event not found",
        },
    )
    def update(self, request, *args, **kwargs):
        """
        Adds an event to the user's bookmarks.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
            **kwargs: Additional keyword arguments.
        
        Returns:
            A response indicating the event was added to bookmarks.
        """
        
        event_id = kwargs.get('pk')
        app_user = get_appuser(request.META.get('HTTP_X_DEVICE_ID'))
        if not app_user:
            return Response("Invalid Device ID", status=status.HTTP_400_BAD_REQUEST)
        
        event = EventV4.objects.get(id=event_id)

        # if event not found return 404
        if not event:
            return Response(status=status.HTTP_404_NOT_FOUND)
        app_user.bookmarked_events_v4.add(event)

        logger.error(f"Event {event_id} added to bookmarks of user {app_user.id}")

        return Response(status=status.HTTP_201_CREATED)
    
    @swagger_auto_schema(
        operation_description="Remove event from bookmarks",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        request_body=None, 
        responses={
            204: "Removed from bookmarks",
            400: "Invalid Device ID",
            404: "Event not found",
        },
    )
    def destroy(self, request, *args, **kwargs):
        """
        Removes an event from the user's bookmarks.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
            **kwargs: Additional keyword arguments.
        
        Returns:
            A response indicating the event was removed from bookmarks.
        """
        event_id = kwargs.get('pk')
        event = get_object_or_404(EventV4, id=event_id)
        device_id = request.META.get("HTTP_X_DEVICE_ID")
        app_user = get_appuser(device_id)
        
        if not app_user:
            return Response("Invalid Device ID", status=status.HTTP_400_BAD_REQUEST)
        
        if event not in app_user.bookmarked_events_v4.all():
            return Response("Event not found", status=status.HTTP_404_NOT_FOUND)
        
        app_user.bookmarked_events_v4.remove(event)
        return Response(status=status.HTTP_204_NO_CONTENT)
    
    @swagger_auto_schema(
        operation_description="Add event to bookmarks",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        responses={
            201: "Added to bookmarks",
            400: "Invalid Device ID",
            404: "Event not found",
        },
    )
    def create(self, request, *args, **kwargs):
        """
        Adds an event to the user's bookmarks.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
            **kwargs: Additional keyword arguments.
        
        Returns:
            A response indicating the event was added to bookmarks.
        """
        event_id = kwargs.get('pk')
        app_user = get_appuser(request.META.get('HTTP_X_DEVICE_ID'))
        if not app_user:
            return Response("Invalid Device ID", status=status.HTTP_400_BAD_REQUEST)
        
        event = EventV4.objects.get(id=event_id)

        # if event not found return 404
        if not event:
            return Response(status=status.HTTP_404_NOT_FOUND)
        app_user.bookmarked_events_v4.add(event)

        logger.error(f"Event {event_id} added to bookmarks of user {app_user.id}")

        return Response(status=status.HTTP_201_CREATED)

class TagCategoryReceiveViewSetFull(viewsets.ViewSet):  

    @swagger_auto_schema(
        responses={
            200: TagCategorySerializer(many=True),
            400: "Error retrieving categories."
        },
    )
    def get_categories_full(self, request, *args, **kwargs):
        try:
            categories = TagCategories.objects.all().order_by('order')
            serializer = TagCategorySerializer(categories, many=True)
        except Exception as e:
            return Response(
                data={"message": f"Error retrieving categories: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response(serializer.data, status=status.HTTP_200_OK)

class TagCategoryReceiveViewSet(viewsets.ViewSet):
    queryset = TagCategories.objects.all()
    
    @swagger_auto_schema(
        responses={
            200: TagCategoryShortSerializer(many=True),
            400: "Error retrieving categories."
        },
    )
    def get_categories(self, request, *args, **kwargs):
        try:
            categories = TagCategories.objects.all().order_by('order')
            serializer = TagCategoryShortSerializer(categories, many=True)
        except Exception as e:
            return Response(data={"message": f"Error retrieving categories: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        return Response(serializer.data)
    

class TagPreferenceTagSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    color = serializers.CharField()
    selected = serializers.BooleanField()

class TagPreferenceCategorySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    order = serializers.IntegerField()
    event_count = serializers.IntegerField()
    tags = TagPreferenceTagSerializer(many=True)

class TagPreferenceArticleCategorySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    order = serializers.IntegerField()
    tags = TagPreferenceTagSerializer(many=True)

def parse_date_param(value, default_to_min_time=True):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(
                    hour=0 if default_to_min_time else 23,
                    minute=0 if default_to_min_time else 59,
                    second=0 if default_to_min_time else 59,
                    microsecond=0
                )
            return dt.replace(tzinfo=pytz.UTC)
        except ValueError:
            continue
    raise ValueError("Invalid date format, must be YYYY-MM-DD or YYYY-MM-DD HH:MM:SS")
    
class TagPreferenceViewSet(viewsets.ViewSet):
    """
    Ermöglicht das Abrufen und Setzen der bevorzugten Tags eines Users.
    """

    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
            string_parameter("date_start", "Startdatum im Format YYYY-MM-DD", required=False),
            string_parameter("date_end", "Enddatum im Format YYYY-MM-DD", required=False),
            string_parameter("search", "A search term. Searches in title, content and tag name", required=False),
        ],
        responses={200: TagPreferenceCategorySerializer(many=True), 400: "Device ID missing."},
    )


    
    def list_tag_preferences(self, request, *args, **kwargs):
        """
        Gibt alle Tag-Kategorien mit allen Tags zurück,
        markiert die vom User ausgewählten Tags als selected
        und liefert einen Counter, wie viele zukünftige Events pro Kategorie für den Nutzer passen.
        Optional: Filter nach date_start/date_end analog zur Events-List.
        """

        import pytz
        from datetime import datetime

        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return Response({"detail": "Device ID missing."}, status=status.HTTP_400_BAD_REQUEST)

        app_user = get_appuser(device_id)
        if not app_user:
            return Response({"detail": "App user not found."}, status=status.HTTP_400_BAD_REQUEST)

        selected_tag_ids = set(app_user.tags.values_list("id", flat=True))
        categories = TagCategories.objects.all().order_by("order").prefetch_related("tagcategory")

        # Areas des Nutzers
        user_areas = list(app_user.areas.values_list("id", flat=True)) if hasattr(app_user, "areas") else []
        hidden_tag_ids = Tag.hidden_ids_for_areas(user_areas)

        # --- Date-Parameter wie in Event-List ---
        date_start = request.query_params.get("date_start")
        date_end = request.query_params.get("date_end")

        now_dt = now()

        if not date_start:
            date_start = now_dt.date().strftime("%Y-%m-%d")
        try:
            if date_start:
                try:
                    date_start = datetime.strptime(date_start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
                except ValueError:
                    date_start = datetime.strptime(date_start, "%Y-%m-%d").replace(tzinfo=pytz.utc)
            if date_end:
                try:
                    date_end = datetime.strptime(date_end, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
                except ValueError:
                    date_end = datetime.strptime(date_end, "%Y-%m-%d").replace(tzinfo=pytz.utc)
        except ValueError as e:
            return Response({"detail": f"Invalid date format: {e}"}, status=status.HTTP_400_BAD_REQUEST)

        output = []

        for category in categories:
            tags = [tag for tag in category.tagcategory.all().order_by('name') if tag.id not in hidden_tag_ids]
            tag_ids_in_category = set(tag.id for tag in tags)
            user_selected_tag_ids_in_cat = tag_ids_in_category & selected_tag_ids

            # Counter: Events mit mindestens einem ausgewählten Tag, published, area, occurrences im gewünschten Zeitraum
            if user_selected_tag_ids_in_cat and user_areas:
                # Berechne real_date_end vor Subquery (Sonderlogik für gleiches Datum)
                real_date_end = date_end
                if date_start and date_end and date_start.date() == date_end.date() and date_end.time() == datetime.min.time():
                    real_date_end = datetime.combine(date_end.date(), datetime.max.time()).replace(tzinfo=pytz.utc)

                # EXISTS-Subquery für Occurrences (wie in list_future_events_by_category)
                occurrence_subquery = Event_Occurrence.objects.filter(event=OuterRef('pk'))

                if date_start or date_end:
                    if date_start and real_date_end:
                        # Range-Filter: start <= end AND end >= start
                        occurrence_subquery = occurrence_subquery.filter(
                            Q(start_datetime__lte=real_date_end) & Q(end_datetime__gte=date_start)
                        )
                    elif date_start:
                        occurrence_subquery = occurrence_subquery.filter(end_datetime__gte=date_start)
                    elif real_date_end:
                        occurrence_subquery = occurrence_subquery.filter(start_datetime__lte=real_date_end)
                else:
                    # Standard: zukünftige Events
                    occurrence_subquery = occurrence_subquery.filter(end_datetime__gte=now_dt)

                # Query mit EXISTS-Annotation
                qs = EventV4.objects.filter(
                    tags__in=user_selected_tag_ids_in_cat,
                    published=True,
                    is_past=False,
                    area__in=user_areas,
                )

                search = request.query_params.get("search")
                if search:
                    qs = qs.filter(build_fulltext_filter(search))

                # EXISTS-Filter: Nur Events mit passenden Occurrences
                qs = qs.annotate(has_occurrence=Exists(occurrence_subquery)).filter(has_occurrence=True)
                event_count = qs.distinct().count()
            else:
                event_count = 0

            tag_list = []
            for tag in tags:
                tag_list.append({
                    "id": tag.id,
                    "name": tag.name,
                    "color": tag.color,
                    "selected": tag.id in selected_tag_ids,
                })
            if tag_list:
                output.append({
                    "id": category.id,
                    "name": category.name,
                    "order": category.order,
                    "event_count": event_count,
                    "tags": tag_list,
                })

        return Response(output, status=status.HTTP_200_OK)
    
    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={200: TagPreferenceArticleCategorySerializer(many=True), 400: "Device ID missing."},
    )
    def list_tag_preferences_articles(self, request, *args, **kwargs):
        """
        Gibt alle Tag-Kategorien mit allen Tags vom Typ "article" zurück
        """
        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return Response({"detail": "Device ID missing."}, status=status.HTTP_400_BAD_REQUEST)

        app_user = get_appuser(device_id)
        if not app_user:
            return Response({"detail": "App user not found."}, status=status.HTTP_400_BAD_REQUEST)

        selected_tag_ids = set(app_user.tags.filter(type="article").values_list("id", flat=True))

        # Alle Kategorien mit Prefetch nur "article"-Tags
        from django.db import models
        categories = TagCategories.objects.all().order_by("order").prefetch_related(
            models.Prefetch("tagcategory", queryset=Tag.objects.filter(type="article").order_by("name"))
        )

        output = []
        for category in categories:
            tags = category.tagcategory.all()
            tag_list = []
            for tag in tags:
                tag_list.append({
                    "id": tag.id,
                    "name": tag.name,
                    "color": tag.color,
                    "selected": tag.id in selected_tag_ids,
                })
            if tag_list:
                output.append({
                    "id": category.id,
                    "name": category.name,
                    "order": category.order,
                    "tags": tag_list,
                })
        return Response(output, status=status.HTTP_200_OK)

    

    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
            string_parameter("date_start", "Startdatum im Format YYYY-MM-DD", required=False),
            string_parameter("date_end", "Enddatum im Format YYYY-MM-DD", required=False),
            string_parameter("search", "A search term. Searches in title, content and tag name", required=False),
        ],
        responses={200: TagPreferenceCategorySerializer(many=True), 400: "Device ID missing."},
    )
    def list_tag_preferences_events(self, request, *args, **kwargs):
        """
        Gibt alle Tag-Kategorien mit allen Tags vom Typ "event" zurück
        """
        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return Response({"detail": "Device ID missing."}, status=status.HTTP_400_BAD_REQUEST)

        app_user = get_appuser(device_id)
        if not app_user:
            return Response({"detail": "App user not found."}, status=status.HTTP_400_BAD_REQUEST)

        selected_tag_ids = set(app_user.tags.filter(type="event").values_list("id", flat=True))
        categories = TagCategories.objects.all().order_by("order").prefetch_related(
            models.Prefetch("tagcategory", queryset=Tag.objects.filter(type="event"))
        )

        user_areas = list(app_user.areas.values_list("id", flat=True)) if hasattr(app_user, "areas") else []
        hidden_tag_ids = Tag.hidden_ids_for_areas(user_areas)

        date_start_param = request.query_params.get("date_start")
        date_end_param = request.query_params.get("date_end")
        now_dt = now()

        try:
            date_start = parse_date_param(date_start_param, default_to_min_time=True) if date_start_param else now_dt
            date_end = parse_date_param(date_end_param, default_to_min_time=False) if date_end_param else None
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        real_date_end = date_end
        if date_start and date_end and date_start.date() == date_end.date() and (date_end.time() == time.min):
            real_date_end = datetime.combine(date_end.date(), time.max)
            if timezone.is_naive(real_date_end):
                real_date_end = timezone.make_aware(real_date_end, timezone=pytz.UTC)

        output = []

        for category in categories:
            tags = [tag for tag in category.tagcategory.all().order_by('name') if tag.id not in hidden_tag_ids]
            tag_ids_in_category = set(tag.id for tag in tags)
            # Fixe Zahl >0 wenn Kategorie Event-Tags hat, 0 nur wenn wirklich keine Tags
            # Das Frontend braucht nur >0 um die Kategorie anzuzeigen
            # Spart 4 teure DB-Count-Queries pro Request
            if tag_ids_in_category:
                event_count = 1
            else:
                event_count = 0

            tag_list = []
            for tag in tags:
                tag_list.append({
                    "id": tag.id,
                    "name": tag.name,
                    "color": tag.color,
                    "selected": tag.id in selected_tag_ids,
                })
            if tag_list:
                output.append({
                    "id": category.id,
                    "name": category.name,
                    "order": category.order,
                    "event_count": event_count,
                    "tags": tag_list,
                })

        return Response(output, status=status.HTTP_200_OK)


    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "tag_ids": openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                    description="Liste der gewählten Tag-IDs"
                )
            },
            required=["tag_ids"]
        ),
        responses={204: "Tags gespeichert", 400: "Fehler"},
    )
    def set_tag_preferences(self, request, *args, **kwargs):
        """
        Setzt die angegebenen Tag-IDs auf "selected" (alle anderen auf "deselected").
        Ungültige IDs werden ignoriert.
        """
        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return Response({"detail": "Device ID missing."}, status=status.HTTP_400_BAD_REQUEST)
        app_user = get_appuser(device_id)
        if not app_user:
            return Response({"detail": "App user not found."}, status=status.HTTP_400_BAD_REQUEST)

        tag_ids = request.data.get("tag_ids")
        if not isinstance(tag_ids, list):
            return Response({"detail": "tag_ids must be a list of integers."}, status=status.HTTP_400_BAD_REQUEST)

        # Filter only existing tags
        valid_tags = Tag.objects.filter(id__in=tag_ids)
        app_user.tags.set(list(valid_tags) + hidden_regional_tags(app_user))  # All others will be deselected

        return Response(status=status.HTTP_204_NO_CONTENT)

    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "tag_ids": openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                    description="Liste der gewählten Tag-IDs (nur Typ 'article')"
                )
            },
            required=["tag_ids"]
        ),
        responses={204: "Tags gespeichert", 400: "Fehler"},
    )
    def set_tag_preferences_articles(self, request, *args, **kwargs):
        """
        Speichert ausgewählte Tags vom Typ 'article' als Präferenz des Users.
        Andere Tag-Typen werden ignoriert.
        """
        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return Response({"detail": "Device ID missing."}, status=status.HTTP_400_BAD_REQUEST)
        app_user = get_appuser(device_id)
        if not app_user:
            return Response({"detail": "App user not found."}, status=status.HTTP_400_BAD_REQUEST)

        tag_ids = request.data.get("tag_ids")
        if not isinstance(tag_ids, list):
            return Response({"detail": "tag_ids must be a list of integers."}, status=status.HTTP_400_BAD_REQUEST)

        # Filter: Nur Tags vom Typ "article"
        valid_tags = Tag.objects.filter(id__in=tag_ids, type="article")
        # Der User darf nur "article"-Tags als Präferenz setzen (vorherige "event"-Tags bleiben erhalten)
        other_types = app_user.tags.exclude(type="article")
        app_user.tags.set(list(valid_tags) + list(other_types))
        return Response(status=status.HTTP_204_NO_CONTENT)


    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "tag_ids": openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                    description="Liste der gewählten Tag-IDs (nur Typ 'event')"
                )
            },
            required=["tag_ids"]
        ),
        responses={204: "Tags gespeichert", 400: "Fehler"},
    )
    def set_tag_preferences_events(self, request, *args, **kwargs):
        """
        Speichert ausgewählte Tags vom Typ 'event' als Präferenz des Users.
        Andere Tag-Typen werden ignoriert.
        """
        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return Response({"detail": "Device ID missing."}, status=status.HTTP_400_BAD_REQUEST)
        app_user = get_appuser(device_id)
        if not app_user:
            return Response({"detail": "App user not found."}, status=status.HTTP_400_BAD_REQUEST)

        tag_ids = request.data.get("tag_ids")
        if not isinstance(tag_ids, list):
            return Response({"detail": "tag_ids must be a list of integers."}, status=status.HTTP_400_BAD_REQUEST)

        # Filter: Nur Tags vom Typ "event"
        valid_tags = Tag.objects.filter(id__in=tag_ids, type="event")
        # Der User darf nur "event"-Tags als Präferenz setzen (vorherige "article"-Tags bleiben erhalten)
        other_types = app_user.tags.exclude(type="event")
        app_user.tags.set(list(valid_tags) + list(other_types) + hidden_regional_tags(app_user))
        return Response(status=status.HTTP_204_NO_CONTENT)

class EventTagReceiveViewSet(viewsets.ViewSet):
    queryset = Tag.objects.all()

    # route that returns all possible tags for an article
    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={
            200: EventTagsSerializer(many=True),
            400: "Device ID missing.",
        },
    )
    def get_all_event_tags(self, request, *args, **kwargs):
        try:
            # Ermitteln des AppUser anhand der device_id
            device_id = request.headers.get("X-Device-ID")
            app_user = get_appuser(device_id)

            # Tags abrufen
            tags = Tag.objects.exclude(color="")
            if app_user:
                tags = tags.exclude(id__in=Tag.hidden_ids_for_areas(app_user.areas.values_list("id", flat=True)))
            tags = tags.order_by('name')

            # Serialisierung der Tags mit dem zusätzlichen Feld "selected"
            serializer = EventTagsSerializer(tags, many=True, context={'app_user': app_user})

        except Exception as e:
            return Response(data={"message": f"Error retrieving tags: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        return Response(serializer.data)


class EventFlagViewSet(viewsets.ViewSet):
    queryset = EventV4.objects.all()

    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'event_id': openapi.Schema(type=openapi.TYPE_INTEGER),
            }
        ),
        responses={
            204: "Event flagged.",
            400: "Device ID missing.",
            404: "No such event.",
            503: "Reporting not configured.",
        },
    )
    def flag_event(self, request, *args, **kwargs):

        event_id =  request.data['event_id']

        if event_id is None:
            return Response(data={"message": "Event ID is missing"}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Send confirmation email
            sender_email = settings.MAIL_SENDING['DEFAULT_FROM_EMAIL']
            receiver_email = settings.FEEDBACK_MAIL_TARGET or settings.MAIL_SENDING.get('ERROR_NOTIFICATION_EMAIL')
            if not receiver_email:
                logger.warning(f"Event {event_id} gemeldet, aber kein Empfänger konfiguriert "
                               "(FEEDBACK_MAIL_TARGET / ERROR_NOTIFICATION_EMAIL)")
                return Response(data={"message": "Reporting is not configured on this server"},
                                status=status.HTTP_503_SERVICE_UNAVAILABLE)
            subject = 'Problematic Event Reported'
            
            message = f'<html><body>Event ID: {event_id}<br>This event has been flagged as problematic.</br>'
           
            try:
                 # add the event title and description to the message
                event = EventV4.objects.get(pk=event_id)
                message += f'Title: {event.title}<br>Description: {event.content}</br>'
                # add the event start date to the message
                message += f'Start Date: {event.start_date}</br>'
            except EventV4.DoesNotExist:
                pass
            message += '</body></html>'
            
            send_html_mail(subject, message, receiver_email, sender_email)

        except Exception as e:
            return Response(data={"message": f"Error sending confirmation email: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        
        return Response(status=status.HTTP_204_NO_CONTENT)