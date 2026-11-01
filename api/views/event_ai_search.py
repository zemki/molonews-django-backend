from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.conf import settings
from content.models import EventV4
from content.utils.ratelimit import seconds_until_allowed
import json
import os
import tempfile
import threading
from logging import getLogger

# Obergrenze für Sprachaufnahmen (wenige Sekunden Audio sind deutlich kleiner)
MAX_AUDIO_BYTES = 10 * 1024 * 1024

logger = getLogger("molonews")


def parse_organization_filter(data):
    """
    Liest den Organisationsfilter aus dem Request-Body.

    Rueckgabe:
        None  - kein Filter angefragt (organizations fehlt/leer)
        [ids] - zu durchsuchende Organisations-IDs

    Raises:
        ValueError bei ungueltigen Werten in organizations
    """
    ids = set()
    filter_requested = False

    raw = data.get('organizations')
    if raw not in (None, '', []):
        filter_requested = True
        if isinstance(raw, str):
            raw = [part for part in raw.split(',') if part.strip()]
        if not isinstance(raw, list):
            raise ValueError("organizations muss eine Liste von IDs oder ein kommaseparierter String sein")
        for value in raw:
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                raise ValueError(f"Ungueltige Organisations-ID: {value!r}")

    if not filter_requested:
        return None
    return sorted(ids)


class AIEventSearchView(View):
    """
    View für die AI Event-Suchseite
    """
    def get(self, request):
        """Rendert die Suchseite"""
        return render(request, 'events/aievent.html')


@method_decorator(csrf_exempt, name='dispatch')
class AIEventSearchAPIView(View):
    """
    API Endpoint für die natürlichsprachliche Event-Suche
    """

    def post(self, request):
        """
        Verarbeitet die Suchanfrage und gibt Events zurück

        Expected JSON body:
        {
            "query": "Konzerte morgen Abend",
            "debug": true/false,            (optional)
            "organizations": [68, 75],      (optional: nur diese Organisations-IDs durchsuchen;
                                             auch als String "68,75" erlaubt)
        }
        Der Organisationsfilter greift VOR der KI-Suche, nicht erst auf dem Ergebnis.

        Returns JSON:
        {
            "query": "...",
            "parsed_params": {...},
            "results_count": 10,
            "events": [...],                (jedes Event mit organization + organization_name)
            "organizations": [...],         (nur wenn gefiltert wurde: verwendete IDs)
            "debug_info": {...}  (only if debug=true)
        }
        """
        wait = seconds_until_allowed(request, "ai")
        if wait:
            response = JsonResponse({"error": "Too many requests."}, status=429)
            response["Retry-After"] = str(wait)
            return response

        try:
            # Parse request body
            data = json.loads(request.body)
            query = data.get('query', '').strip()
            # Debug-Ausgaben (Prompts, Modellantwort) nur, wenn die Instanz es erlaubt
            debug = bool(data.get('debug', False)) and getattr(settings, 'RAG_SEARCH_DEBUG', False)

            if not query:
                return JsonResponse({
                    'error': 'Keine Suchanfrage angegeben'
                }, status=400)

            # Organisationsfilter: explizite IDs
            organization_ids = None
            try:
                organization_ids = parse_organization_filter(data)
            except ValueError as e:
                return JsonResponse({'error': str(e)}, status=400)

            if organization_ids is not None and not organization_ids:
                # Filter angefragt, aber keine Organisation angegeben:
                # leeres Ergebnis statt ungefilterter Suche
                return JsonResponse({
                    'query': query,
                    'results_count': 0,
                    'events': [],
                    'organizations': [],
                })

            # Get device_id from header
            device_id = request.headers.get('X-Device-ID')

            # Import RAG module
            from rag.rag_query import EventSearchService

            # Initialize search service (lokales Sprachmodell, siehe molo/llm.py)
            service = EventSearchService()

            # Perform search
            result = service.search(query, device_id=device_id, debug=debug,
                                    organization_ids=organization_ids)
            if organization_ids:
                result['organizations'] = organization_ids

            # Format events for frontend
            if 'events' in result:
                import re as re_module

                # Bulk-load all events in one query instead of N+1 individual queries
                event_ids = [e['id'] for e in result['events']]
                events_qs = EventV4.objects.filter(id__in=event_ids).select_related(
                    'source', 'source__organization'
                )
                # Build lookup dict preserving original order
                events_by_id = {event.id: event for event in events_qs}

                formatted_events = []
                for event_data in result['events']:
                    event = events_by_id.get(event_data['id'])
                    if not event:
                        logger.warning(f"Event {event_data['id']} nicht gefunden")
                        continue

                    # Get image URL - same logic as in event_v4.py serializer
                    image_url = None
                    if event.image:
                        image_url = request.build_absolute_uri(event.image.url)
                    elif event.image_url:
                        image_url = event.image_url
                    else:
                        # Fallback to source default image or organization image
                        try:
                            if event.source.default_image:
                                image_url = request.build_absolute_uri(event.source.default_image.url)
                            elif event.source.default_image_url:
                                image_url = event.source.default_image_url
                            elif event.source.organization and event.source.organization.image:
                                image_url = request.build_absolute_uri(event.source.organization.image.url)
                        except Exception:
                            pass

                    # Get first 200 characters of content as abstract
                    abstract = ''
                    if event.content:
                        clean_content = re_module.sub('<[^<]+?>', '', event.content)
                        abstract = clean_content[:200] + '...' if len(clean_content) > 200 else clean_content

                    # Check if coordinates are valid
                    latitude = None
                    longitude = None
                    if event.latitude is not None and event.longitude is not None:
                        try:
                            lat = float(event.latitude)
                            lon = float(event.longitude)
                            if -90 <= lat <= 90 and -180 <= lon <= 180:
                                latitude = str(lat)
                                longitude = str(lon)
                        except (ValueError, TypeError):
                            pass

                    # Anzeige-Datum: der Termin, den die Suche im gefragten Zeitraum
                    # gefunden hat (rag_query setzt dafuer die passende Occurrence ein).
                    # event.start_date aus der DB ist bei Serien- und Dauerterminen der
                    # Basistermin - eine Wochenend-Suche zeigte damit Termine von heute.
                    start_date = event_data.get('start_date') or event.start_date

                    # Organisation mitliefern, damit Clients den Veranstalter
                    # anzeigen und nach ihm filtern koennen
                    organization = getattr(event.source, 'organization', None) if event.source_id else None

                    formatted_events.append({
                        'id': event.id,
                        'title': event.title,
                        'start_date': start_date.isoformat(),
                        'event_location': event.event_location or '',
                        'town': event.town or '',
                        'zip_code': event.zip_code or '',
                        'abstract': abstract,
                        'image_url': image_url,
                        'latitude': latitude,
                        'longitude': longitude,
                        'organization': organization.id if organization else None,
                        'organization_name': organization.name if organization else '',
                    })

                result['events'] = formatted_events

            return JsonResponse(result)

        except json.JSONDecodeError:
            return JsonResponse({
                'error': 'Ungültige JSON-Daten'
            }, status=400)
        except Exception as e:
            logger.error(f"Fehler bei Event-Suche: {e}", exc_info=True)
            return JsonResponse({
                'error': f'Interner Serverfehler: {str(e)}'
            }, status=500)


# Lokales Spracherkennungsmodell (faster-whisper), wird beim ersten Aufruf geladen
_speech_model = None
_speech_model_lock = threading.Lock()


def get_speech_model():
    """
    Lädt das lokale Whisper-Modell (faster-whisper) einmalig und cached es.

    Returns:
        WhisperModel oder None, wenn Spracheingabe deaktiviert bzw.
        faster-whisper nicht installiert ist.
    """
    global _speech_model
    if not getattr(settings, 'SPEECH_TO_TEXT_ENABLED', False):
        return None
    if _speech_model is not None:
        return _speech_model
    with _speech_model_lock:
        if _speech_model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                logger.error("Spracheingabe aktiviert, aber faster-whisper ist nicht installiert")
                return None
            model_name = getattr(settings, 'WHISPER_MODEL', 'small')
            device = getattr(settings, 'WHISPER_DEVICE', 'auto')
            logger.info(f"Lade Spracherkennungsmodell '{model_name}' (device={device})")
            _speech_model = WhisperModel(model_name, device=device, compute_type="int8")
    return _speech_model


@method_decorator(csrf_exempt, name='dispatch')
class SpeechTranscribeView(View):
    """
    API Endpoint für die Spracheingabe der KI-Suche.
    Transkribiert Audio lokal mit faster-whisper (optional, siehe
    SPEECH_TO_TEXT_ENABLED / WHISPER_MODEL in den Settings).
    """

    def post(self, request):
        """
        Expected: multipart/form-data with audio file ("audio")

        Returns JSON:
        {
            "text": "transkribierter Text"
        }
        """
        wait = seconds_until_allowed(request, "ai")
        if wait:
            response = JsonResponse({"error": "Too many requests."}, status=429)
            response["Retry-After"] = str(wait)
            return response

        model = get_speech_model()
        if model is None:
            return JsonResponse({
                'error': 'Spracheingabe ist auf diesem Server nicht aktiviert'
            }, status=503)

        if 'audio' not in request.FILES:
            return JsonResponse({
                'error': 'Keine Audio-Datei gefunden'
            }, status=400)

        audio_file = request.FILES['audio']

        # Sehr kleine Dateien sind leer bzw. ohne verwertbares Audio
        if audio_file.size > MAX_AUDIO_BYTES:
            return JsonResponse({'error': 'Audio-Datei zu groß'}, status=413)
        if audio_file.size < 1000:
            logger.warning(f"Audio-Datei zu klein: {audio_file.size} bytes")
            return JsonResponse({'text': ''})

        content_type = audio_file.content_type or ''
        suffix = '.webm'
        if 'ogg' in content_type:
            suffix = '.ogg'
        elif 'wav' in content_type:
            suffix = '.wav'
        elif 'mp4' in content_type or 'm4a' in content_type:
            suffix = '.m4a'

        temp_audio_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_audio:
                for chunk in audio_file.chunks():
                    temp_audio.write(chunk)
                temp_audio_path = temp_audio.name

            segments, _info = model.transcribe(temp_audio_path, language="de")
            text = " ".join(segment.text.strip() for segment in segments).strip()
            return JsonResponse({'text': text})

        except Exception as e:
            logger.error(f"Fehler bei der Transkription: {e}", exc_info=True)
            # Leerer Text statt Fehler für eine freundliche UX in der App
            return JsonResponse({'text': ''})

        finally:
            if temp_audio_path and os.path.exists(temp_audio_path):
                os.unlink(temp_audio_path)


class EventDetailView(View):
    """
    View für die Event-Detailseite
    """
    def get(self, request, event_id):
        """Rendert die Event-Detailseite"""
        event = get_object_or_404(EventV4, id=event_id, published=True)

        # Get image URL - same logic as in event_v4.py serializer
        image_url = None
        if event.image:
            image_url = request.build_absolute_uri(event.image.url)
        elif event.image_url:
            image_url = event.image_url
        else:
            # Fallback to source default image or organization image
            try:
                if event.source.default_image:
                    image_url = request.build_absolute_uri(event.source.default_image.url)
                elif event.source.default_image_url:
                    image_url = event.source.default_image_url
                elif event.source.organization and event.source.organization.image:
                    image_url = request.build_absolute_uri(event.source.organization.image.url)
            except:
                pass

        # Get tags
        tags = event.tags.all()

        # Check if coordinates are valid
        latitude = None
        longitude = None
        if event.latitude is not None and event.longitude is not None:
            try:
                lat = float(event.latitude)
                lon = float(event.longitude)
                # Check if coordinates are in valid range
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    latitude = lat
                    longitude = lon
            except (ValueError, TypeError):
                pass

        context = {
            'event': event,
            'image_url': image_url,
            'tags': tags,
            'latitude': latitude,
            'longitude': longitude,
        }

        return render(request, 'events/event_detail.html', context)
