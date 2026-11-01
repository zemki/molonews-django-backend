"""
KI-Event-Suche: übersetzt natürlichsprachliche Anfragen in Datenbank-Abfragen.

Das Sprachmodell läuft lokal über Ollama (siehe molo/llm.py, Standard
gpt-oss:20b). Beschreibung des Ablaufs: rag/README.md
"""

import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

if __name__ == "__main__":
    # Direkter Aufruf als Script: Django vor den Model-Imports initialisieren
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'molo.settings.dev')
    import django
    django.setup()

from django.conf import settings
from django.db.models import Q

from content.models import Area, Tag, EventV4, AppUser, Event_Occurrence
from molo import llm

# Logger "rag_search" wird über die LOGGING-Konfiguration in den Settings gesteuert
logger = logging.getLogger("rag_search")


import re as _re

# Modell: Parsing und Relevanz-Filter nutzen das in den Settings konfigurierte
# lokale Modell (LLM_MODEL, Standard gpt-oss:20b; auf starker Hardware gpt-oss:120b).

# Gestufte Suche: Liefert die Keyword-Suche weniger als so viele Treffer,
# wird zusätzlich auf das allgemeinere tag_request verbreitert.
MIN_KEYWORD_RESULTS = 5


def default_area_names() -> List[str]:
    """
    Namen der Standard-Gebiete (Setting DEFAULT_AREA_IDS), die genutzt werden,
    wenn weder die Anfrage noch das Nutzerprofil einen Ort enthält.
    Leer = keine Einschränkung nach Gebiet.
    """
    area_ids = getattr(settings, 'DEFAULT_AREA_IDS', []) or []
    if not area_ids:
        return []
    try:
        return list(Area.objects.filter(id__in=area_ids).values_list('name', flat=True))
    except Exception as e:
        logger.error(f"RAG Query: Fehler beim Laden der Standard-Gebiete: {e}")
        return []


def strip_html(text: str) -> str:
    """Entfernt HTML-Tags und normalisiert Whitespace (für LLM-Filter-Prompts)."""
    if not text:
        return ''
    cleaned = _re.sub(r'<[^>]+>', ' ', text)
    return ' '.join(cleaned.split())


def keyword_to_regex(variant: str) -> str:
    r"""
    Baut ein Wortgrenzen-Regex (PostgreSQL ~* / Django __iregex) für ein Volltext-Keyword.

    Hintergrund: Reines ``icontains`` liefert bei kurzen Keywords katastrophale Präzision,
    weil das Keyword als Teilstring in unzusammenhängenden Wörtern steckt
    (z.B. "punk" → "Höhepunkt", "Treffpunkt", "Punkt"; "rave" → "Trave", "brave";
    "techno" → "Technologie"). Gemessen: 97 % der "punk"-Treffer waren Rauschen.

    Strategie (Deutsch ist kopf-final, das Suchwort steht am Komposita-ENDE):
    - Keywords >= 5 Zeichen → rechts verankert  ``{kw}\y``
      Fängt Komposita-Köpfe ("Bücherflohmarkt", "Sinfoniekonzert", "Cyberpunk",
      "Weihnachtsmarkt") und blockiert Präfix-Rauschen ("Technologie" != techno).
    - Keywords <= 4 Zeichen → beidseitig  ``\y{kw}\y``
      Maximale Präzision, weil sehr kurze Wörter sonst als Suffix anderer Wörter
      auftreten ("rave" → "Trave", "ska" → "Maske").

    ``re.escape`` neutralisiert Regex-Metazeichen im Keyword; das resultierende
    ``\<nicht-alphanumerisch>`` wird von PostgreSQL als Literal behandelt.
    """
    cleaned = variant.strip()
    esc = _re.escape(cleaned)
    if len(cleaned) <= 4:
        return r'\y' + esc + r'\y'
    return esc + r'\y'


# Datenklasse für strukturierte Query-Parameter
@dataclass
class EventQueryParams:
    """Strukturierte Parameter für Event-Queries"""
    areas: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    subtags: Optional[List[str]] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    location: Optional[str] = None
    zip_code: Optional[str] = None
    town: Optional[str] = None
    organization: Optional[str] = None
    search_text: Optional[str] = None
    content_keywords: Optional[List[str]] = None  # Inhaltliche Suchbegriffe für Volltextsuche (spezifische Begriffe wie Künstler, Genre)
    keyword_variants: Optional[List[List[str]]] = None  # [[singular, plural, ...], ...] – OR innerhalb, AND zwischen Gruppen
    tag_request: Optional[List[str]] = None  # Allgemeine Themensuche (z.B. "musik", "sport") - wird mit Fuzzy-Matching auf Tags gemapped
    is_past: Optional[bool] = None
    published: Optional[bool] = True
    limit: Optional[int] = 50

    # Intent-Gate: True wenn die Anfrage KEINE Event-Suche ist (Supportfrage, News, Off-Topic)
    not_event_search: Optional[bool] = None

    # Neue Felder für semantisches Verständnis (nur bei >4 Wörtern)
    query_intent: Optional[str] = None  # "direct", "recommendation", "contextual"
    companion_context: Optional[str] = None  # "elderly", "children", "friends", etc.
    suggested_tags: Optional[List[str]] = None  # LLM-vorgeschlagene Tags basierend auf Kontext
    accessibility_required: Optional[bool] = None  # Barrierefreiheit
    time_preference: Optional[str] = None  # "morning", "afternoon", "evening"
    
    def to_dict(self):
        """Konvertiert zu Dictionary und filtert None-Werte"""
        result = {}
        for k, v in asdict(self).items():
            if v is not None:
                if isinstance(v, datetime):
                    result[k] = v.isoformat()
                else:
                    result[k] = v
        return result


# JSON-Schema für Structured Outputs (Ollama-Parameter "format").
# Garantiert valides JSON mit exakt diesen Feldern → kein Code-Fence-Strippen.
# Alle Felder sind "required", Optionalität über nullable Typen (["...", "null"]).
EVENT_QUERY_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "not_event_search": {
            "type": ["boolean", "null"],
            "description": "true wenn die Anfrage KEINE Event-Suche ist (Supportfrage, News/Wetter, Smalltalk, Off-Topic)",
        },
        "areas": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "Genannte Städte/Regionen, Großschreibung",
        },
        "start_date": {
            "type": ["string", "null"],
            "description": "ISO-Format YYYY-MM-DDTHH:MM:SS ohne Timezone, nur wenn Zeitraum genannt",
        },
        "end_date": {
            "type": ["string", "null"],
            "description": "ISO-Format YYYY-MM-DDTHH:MM:SS ohne Timezone, nur wenn Zeitraum genannt",
        },
        "location": {
            "type": ["string", "null"],
            "description": "Konkreter Veranstaltungsort (Venue), falls genannt",
        },
        "zip_code": {
            "type": ["string", "null"],
            "description": "Postleitzahl, falls genannt",
        },
        "tag_request": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "Allgemeine Themen-Begriffe in Kleinschreibung, z.B. musik, sport, kunst",
        },
        "content_keywords": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "Spezifische Suchbegriffe in Kleinschreibung (Genre, Künstler, Aktivität)",
        },
        "keyword_variants": {
            "type": ["array", "null"],
            "items": {"type": "array", "items": {"type": "string"}},
            "description": "Pro content_keyword eine Gruppe: Singular, Plural und Synonyme",
        },
        "tags": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "NUR wenn der User einen Tag woertlich nennt und keine content_keywords gesetzt sind. Exakte Tag-Namen aus der Tag-Liste inkl. Emoji. Im Zweifel leer lassen und tag_request nutzen.",
        },
        "query_intent": {
            "type": ["string", "null"],
            "description": "direct | recommendation | contextual",
        },
        "companion_context": {
            "type": ["string", "null"],
            "description": "elderly | children | family | friends | partner | alone",
        },
        "suggested_tags": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "Max 2-3 kontextpassende Tags, EXAKT aus der Tag-Liste kopiert",
        },
        "accessibility_required": {
            "type": ["boolean", "null"],
            "description": "true bei Barrierefreiheit/Rollstuhl/Senioren",
        },
        "time_preference": {
            "type": ["string", "null"],
            "description": "morning | afternoon | evening | night",
        },
    },
    "required": [
        "not_event_search", "areas", "start_date", "end_date", "location",
        "zip_code", "tag_request", "content_keywords", "keyword_variants",
        "tags", "query_intent", "companion_context", "suggested_tags",
        "accessibility_required", "time_preference",
    ],
}

# Schema für die Relevanz-Filter: Liste der passenden Event-IDs (leer = keine)
EVENT_IDS_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["ids"],
}


class NaturalLanguageEventQuery:
    """
    Klasse zur Verarbeitung natürlichsprachlicher Anfragen
    und Übersetzung in Django ORM Queries
    """
    
    def __init__(self):
        # Lade verfügbare Areas und Tags aus der Datenbank
        self.available_areas = self._load_areas()
        self.available_tags = self._load_tags()

        # Area-Status der letzten build_django_query() (für transparente Response).
        # Parser wird pro Request neu instanziiert → request-sicher.
        self.last_area_source = None
        self.last_areas_filtered = []

        logger.info(f"RAG Query Init: Geladen {len(self.available_areas)} Areas und {len(self.available_tags)} Tags")
    
    def _load_areas(self) -> List[str]:
        """Lädt alle verfügbaren Areas aus der Datenbank"""
        try:
            areas = Area.objects.filter(visible=True).values_list('name', flat=True)
            return list(areas)
        except Exception as e:
            logger.error(f"RAG Query: Fehler beim Laden der Areas: {e}")
            return []

    def _load_tags(self) -> List[str]:
        """Lädt alle verfügbaren Tags aus der Datenbank"""
        try:
            tags = Tag.objects.filter(type='event').values_list('name', flat=True)
            return list(tags)
        except Exception as e:
            logger.error(f"RAG Query: Fehler beim Laden der Tags: {e}")
            return []

    def _match_tag_requests(self, tag_requests: List[str]) -> List[str]:
        """
        Macht Fuzzy-Matching von allgemeinen Themen-Begriffen auf vorhandene Tags

        Args:
            tag_requests: Liste von allgemeinen Begriffen (z.B. ["musik", "sport", "kultur"])

        Returns:
            Liste von matched Tag-Namen aus der Datenbank

        Beispiele:
            ["musik"] → ["Musik & Konzerte 🎵"]
            ["sport"] → ["Sport & Fitness 💪", "Sport & Bewegung 🏃"]
            ["kultur"] → ["Kultur & Gemeinschaft 🌍"]
        """
        if not tag_requests:
            return []

        matched_tags = []

        for request in tag_requests:
            request_lower = request.lower().strip()

            # Suche nach Tags die den Begriff enthalten (case-insensitive)
            for tag in self.available_tags:
                tag_lower = tag.lower()

                # Fuzzy-Match: Tag enthält den Suchbegriff
                if request_lower in tag_lower:
                    matched_tags.append(tag)
                    logger.info(f"RAG Query: Tag-Request '{request}' matched Tag '{tag}'")

        # Duplikate entfernen (falls mehrere Matches)
        matched_tags = list(set(matched_tags))

        if not matched_tags:
            logger.warning(f"RAG Query: Keine Tags gefunden für tag_requests: {tag_requests}")
        else:
            logger.info(f"RAG Query: {len(matched_tags)} Tags gefunden für tag_requests {tag_requests}: {matched_tags}")

        return matched_tags

    def create_enhanced_system_prompt(self) -> str:
        """Erstellt den System-Prompt für das LLM mit echten Daten aus der DB"""

        tags_str = ', '.join([f'"{tag}"' for tag in self.available_tags])

        now = datetime.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)
        weekday_names = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag']

        days_until_saturday = (5 - today.weekday()) % 7
        if days_until_saturday == 0 and now.hour >= 12:
            days_until_saturday = 7
        next_saturday = today + timedelta(days=days_until_saturday if days_until_saturday > 0 else 7)
        next_sunday = next_saturday + timedelta(days=1)

        # Berechne Wochen-Grenzen für "diese Woche" und "nächste Woche"
        this_week_monday = today - timedelta(days=today.weekday())  # Montag dieser Woche
        this_week_sunday = this_week_monday + timedelta(days=6)
        next_week_monday = this_week_monday + timedelta(days=7)
        next_week_sunday = next_week_monday + timedelta(days=6)

        return f"""Du bist ein Query-Parser für eine Event-Datenbank. Extrahiere strukturierte JSON-Parameter.

VERFÜGBARE TAGS: [{tags_str}]

DATUM (heute={today.strftime('%Y-%m-%d')} {weekday_names[today.weekday()]}, morgen={tomorrow.strftime('%Y-%m-%d')}):
- start_date/end_date: ISO Format YYYY-MM-DDTHH:MM:SS OHNE Timezone
- Nur setzen wenn explizit genannt, sonst weglassen
- Einzelne Tage: start+end vom SELBEN Tag, end immer T23:59:59
- "heute" → {today.strftime('%Y-%m-%d')}T00:00:00 bis {today.strftime('%Y-%m-%d')}T23:59:59
- "morgen" → {tomorrow.strftime('%Y-%m-%d')}T00:00:00 bis {tomorrow.strftime('%Y-%m-%d')}T23:59:59
- "am Wochenende" → {next_saturday.strftime('%Y-%m-%d')}T00:00:00 bis {next_sunday.strftime('%Y-%m-%d')}T23:59:59
- "diese Woche" → {this_week_monday.strftime('%Y-%m-%d')}T00:00:00 bis {this_week_sunday.strftime('%Y-%m-%d')}T23:59:59
- "nächste Woche" → {next_week_monday.strftime('%Y-%m-%d')}T00:00:00 bis {next_week_sunday.strftime('%Y-%m-%d')}T23:59:59
- "ab 18 Uhr" → T18:00:00 bis T23:59:59
- "um/gegen 14 Uhr", "um 14:00" → ENGES Fenster ±1 Stunde: T13:00:00 bis T15:00:00 (NICHT bis T23:59:59!)
- Woche = Montag bis Sonntag (Kalenderwoche). Berechne andere Daten selbst.

AREAS:
- areas: Liste genannter Städte/Regionen, Großschreibung ("Hamburg", "Bremen")
- Wenn keine Stadt genannt → weglassen

THEMEN - tag_request vs content_keywords:
- tag_request: ALLGEMEINE Begriffe ("musik","sport","kultur","kunst","theater","party"), Kleinschreibung. System macht Fuzzy-Match.
  "konzert"/"konzerte" → tag_request: ["musik"]
- content_keywords: SPEZIFISCHE Begriffe (Genre: "punk","jazz"; Künstler: "Beyoncé"; Aktivität: "yoga"), Kleinschreibung
  NICHT für allgemeine Begriffe wie "musik","sport","konzert" → dafür tag_request!
- Bei spezifischen Begriffen BEIDES ausgeben: content_keywords + das passende allgemeine tag_request als
  Oberkategorie. Beispiel: "punk konzert" → content_keywords:["punk"] UND tag_request:["musik"].
  Das System sucht zuerst mit den Keywords und verbreitert nur bei zu wenigen Treffern auf die Oberkategorie.
  Gibt es keine sinnvolle Oberkategorie (z.B. Künstlername, Venue), tag_request weglassen.
- keyword_variants: IMMER zusammen mit content_keywords angeben! Für jeden Begriff in content_keywords ein
  Sub-Array mit Singular, Plural UND 2-3 deutschen Synonymen/eng verwandten Begriffen, die in Event-Texten
  stehen könnten. Reihenfolge identisch zu content_keywords. OR innerhalb jeder Gruppe, AND zwischen Gruppen.
  Beispiele:
  "flohmarkt" oder "flohmärkte" → content_keywords:["flohmarkt"], keyword_variants:[["flohmarkt","flohmärkte","trödelmarkt","trödelmärkte"]]
  "techno party"                → content_keywords:["techno"], keyword_variants:[["techno","elektro","electronic","rave"]]
  "yoga workshop"               → content_keywords:["yoga","workshop"], keyword_variants:[["yoga"],["workshop","workshops","kurs","kurse"]]
  "punk konzert"                → content_keywords:["punk"], keyword_variants:[["punk","punkrock","hardcore"]]
- tags: NUR wenn der User einen Tag WÖRTLICH aus der Tag-Liste nennt, inklusive Emoji EXAKT kopieren.
  NIEMALS zusammen mit content_keywords ausgeben - tags ist ein harter Filter und würde die Keyword-Suche
  UND-verknüpft einschränken. Eine Oberkategorie zu einem Keyword gehört in tag_request, NICHT in tags.
  Beispiel: "flohmärkte in hamburg" → content_keywords:["flohmarkt"] + tag_request:["märkte"], tags: LEER.
  Im Zweifel tags weglassen.

KEINE EVENT-SUCHE:
- not_event_search: true setzen, wenn die Anfrage KEINE Suche nach Veranstaltungen ist:
  Bedienungs-/Supportfragen ("Wie gebe ich eine Adresse ein?"), Nachrichten/Wetter ("Hitzewarnung in Krefeld"),
  Smalltalk oder themenfremde Anfragen. Dann alle anderen Filterfelder auf null lassen.
- Bei jeder plausiblen Event-Suche (auch vage wie "was geht heute") → not_event_search: false/null.

SEMANTIK (nur bei komplexen Anfragen):
- query_intent: "direct" | "recommendation" | "contextual"
- companion_context: "elderly" (Oma/Senioren) | "children" | "family" | "friends" | "partner" (Freund/Freundin/romantisch/Date) | "alone"
- suggested_tags: MAX 2-3 Tags (EXAKT aus Tag-Liste kopieren!), nur die PASSENDSTEN wählen!
  elderly → Bühne & Performance, Kultur & Gemeinschaft (NICHT alles!)
  children → Familienfreundlich, Bildung & Wissen
  partner → Bühne & Performance, Kunst & Ausstellung, Musik & Konzerte
  friends/Party → Musik & Konzerte, Party & Nachtleben
- accessibility_required: true bei elderly/Rollstuhl/barrierefrei
- time_preference: "morning"|"afternoon"|"evening"|"night"

BEISPIELE (null-Felder weggelassen):
"musik in bremen" → {{"areas":["Bremen"],"tag_request":["musik"]}}
"punk konzert hamburg morgen" → {{"areas":["Hamburg"],"content_keywords":["punk"],"keyword_variants":[["punk","punkrock","hardcore"]],"tag_request":["musik"],"start_date":"{tomorrow.strftime('%Y-%m-%d')}T00:00:00","end_date":"{tomorrow.strftime('%Y-%m-%d')}T23:59:59"}}
"was ist los in leipzig" → {{"areas":["Leipzig"]}}
"hitzewarnung in krefeld" → {{"not_event_search":true}}
"wie gebe ich eine adresse ein?" → {{"not_event_search":true}}
"was geht in bremen am sonntag um 14:00" → {{"areas":["Bremen"],"start_date":"{next_sunday.strftime('%Y-%m-%d')}T13:00:00","end_date":"{next_sunday.strftime('%Y-%m-%d')}T15:00:00"}}
"mit meiner Oma in Hamburg" → {{"areas":["Hamburg"],"query_intent":"contextual","companion_context":"elderly","suggested_tags":["Bühne & Performance 🎭","Kultur & Gemeinschaft 🌍"],"accessibility_required":true}}
"mit Kind in Bremen" → {{"areas":["Bremen"],"query_intent":"contextual","companion_context":"children","suggested_tags":["Familienfreundlich 🧑‍🧑‍🧒‍🧒","Bildung & Wissen 🔭"]}}
"romantischer Abend mit Freundin in Hamburg" → {{"areas":["Hamburg"],"query_intent":"contextual","companion_context":"partner","suggested_tags":["Bühne & Performance 🎭","Kunst & Ausstellung 🖼️"]}}
"konzerte heute abend" → {{"tag_request":["musik"],"start_date":"{today.strftime('%Y-%m-%d')}T18:00:00","end_date":"{today.strftime('%Y-%m-%d')}T23:59:59"}}

Antworte NUR mit validem JSON."""

    def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """
        Ruft das lokale Sprachmodell auf und gibt den Response-Text zurück.
        Verwendet Structured Outputs (JSON-Schema) → Format ist garantiert.
        Enthält Retry-Logik: bis zu 2 Retries bei Fehlern.
        """
        import time

        max_retries = 2
        last_exception = None
        response_text = None

        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    wait_time = 1.0 * (2 ** (attempt - 1))  # 1s, 2s
                    logger.info(f"RAG Query Parse: Retry {attempt}/{max_retries} nach {wait_time}s Wartezeit")
                    time.sleep(wait_time)

                response_text = llm.generate(
                    system_prompt,
                    user_prompt,
                    schema=EVENT_QUERY_JSON_SCHEMA,
                )
                logger.debug(f"RAG Query Parse: LLM Response (Versuch {attempt + 1}): {response_text}")

                # Sicherheitsnetz: JSON validieren (ein abgeschnittener Response
                # würde hier auffallen und den Retry auslösen)
                json.loads(response_text)
                return response_text

            except json.JSONDecodeError as e:
                last_exception = e
                logger.warning(f"RAG Query Parse: JSON Parsing Error bei Versuch {attempt + 1}: {e}")
                logger.warning(f"RAG Query Parse: Response war: {response_text}")
            except Exception as e:
                last_exception = e
                logger.warning(f"RAG Query Parse: LLM-Call fehlgeschlagen bei Versuch {attempt + 1}: {e}")

        # Alle Retries erschöpft
        logger.error(f"RAG Query Parse: Alle {max_retries + 1} Versuche fehlgeschlagen")
        raise last_exception

    def parse_natural_query(self, query: str, debug: bool = False) -> Tuple[EventQueryParams, Optional[Dict]]:
        """
        Verwendet LLM um natürliche Sprache in strukturierte Parameter zu übersetzen

        Returns:
            tuple: (EventQueryParams, debug_info dict or None)
        """
        debug_info = None if not debug else {}

        try:
            # Verwende immer den Enhanced Prompt (funktioniert für alle Queries)
            system_prompt = self.create_enhanced_system_prompt()
            prompt_type = "enhanced"
            logger.info("RAG Query Parse: Verwende Enhanced Prompt")

            user_prompt = f"Parse diese Anfrage: {query}"

            logger.debug(f"RAG Query Parse: Query='{query}'")
            if debug:
                debug_info['system_prompt'] = system_prompt
                debug_info['user_prompt'] = user_prompt
                debug_info['prompt_type'] = prompt_type

            # LLM-Call mit Retry-Logik
            response_text = self._call_llm(system_prompt, user_prompt)

            if debug:
                debug_info['llm_response'] = response_text
                debug_info['model'] = llm.default_model()

            params_dict = json.loads(response_text)

            # Structured Outputs geben ALLE Schema-Felder zurück,
            # nicht gesetzte als null → entfernen, damit die Dataclass-Defaults greifen.
            params_dict = {k: v for k, v in params_dict.items() if v is not None}

            # Plausibilitätsprüfung: Lokale Modelle füllen optionale Felder gern mit
            # geratenen Werten (z.B. PLZ zur Stadt, Ort = Stadtname). Solche Werte
            # würden die DB-Filter unnötig verengen → nur behalten, was in der
            # Anfrage tatsächlich vorkommt.
            query_lower = query.lower()
            for field in ('zip_code', 'location'):
                value = params_dict.get(field)
                if value and str(value).strip().lower() not in query_lower:
                    logger.info(f"RAG Query Parse: '{field}'='{value}' steht nicht in der Anfrage → ignoriert")
                    del params_dict[field]
            area_names = {str(a).strip().lower() for a in (params_dict.get('areas') or [])}
            if params_dict.get('location') and str(params_dict['location']).strip().lower() in area_names:
                # Ort = genannte Stadt → bereits über areas abgedeckt
                del params_dict['location']
            if params_dict.get('companion_context') == 'alone' and 'allein' not in query_lower and 'alone' not in query_lower:
                del params_dict['companion_context']

            # Nur bekannte Dataclass-Felder durchlassen (Robustheit gegen Schema-Drift)
            import dataclasses as _dc
            allowed_fields = {f.name for f in _dc.fields(EventQueryParams)}
            unknown_keys = [k for k in params_dict if k not in allowed_fields]
            for k in unknown_keys:
                logger.warning(f"RAG Query Parse: Unbekanntes Feld '{k}' vom LLM ignoriert")
                del params_dict[k]

            # Konvertiere String-Dates zu timezone-aware datetime
            # WICHTIG: Das LLM gibt lokale deutsche Zeit aus (wie im Prompt angewiesen).
            # Wir interpretieren diese als Europe/Berlin und konvertieren zu UTC für DB-Queries.
            import re
            import pytz
            berlin_tz = pytz.timezone('Europe/Berlin')

            if 'start_date' in params_dict and params_dict['start_date']:
                date_str = params_dict['start_date']
                if isinstance(date_str, str):
                    # Entferne Timezone-Suffix (z.B. +02:00, +00:00, Z)
                    date_str = re.sub(r'[+-]\d{2}:\d{2}$|Z$', '', date_str)
                    dt = datetime.fromisoformat(date_str)
                    # Interpretiere als Berliner Lokalzeit, dann zu UTC konvertieren
                    dt = berlin_tz.localize(dt).astimezone(pytz.utc)
                    params_dict['start_date'] = dt
                else:
                    del params_dict['start_date']

            if 'end_date' in params_dict and params_dict['end_date']:
                date_str = params_dict['end_date']
                if isinstance(date_str, str):
                    # Entferne Timezone-Suffix (z.B. +02:00, +00:00, Z)
                    date_str = re.sub(r'[+-]\d{2}:\d{2}$|Z$', '', date_str)
                    dt = datetime.fromisoformat(date_str)
                    # Interpretiere als Berliner Lokalzeit, dann zu UTC konvertieren
                    dt = berlin_tz.localize(dt).astimezone(pytz.utc)
                    params_dict['end_date'] = dt
                else:
                    del params_dict['end_date']

            # Areas werden nicht mehr hier validiert - die Validierung erfolgt in build_django_query
            # Das LLM extrahiert jetzt alle genannten Städte, die DB-Prüfung passiert später
            if 'areas' in params_dict:
                logger.info(f"RAG Query Parse: LLM extrahierte Area(s): {params_dict['areas']}")

            # Entferne town falls vorhanden - wird nicht mehr verwendet
            if 'town' in params_dict:
                # Falls LLM noch town verwendet, verschiebe es zu areas
                town = params_dict.pop('town')
                if 'areas' not in params_dict:
                    params_dict['areas'] = [town.title()]  # Capitalize erste Buchstaben
                    logger.info(f"RAG Query Parse: town '{town}' zu areas verschoben")

            if 'tags' in params_dict:
                params_dict['tags'] = [tag for tag in params_dict['tags'] if tag in self.available_tags]
                if not params_dict['tags']:
                    del params_dict['tags']

            # Logging für content_keywords
            if 'content_keywords' in params_dict and params_dict['content_keywords']:
                logger.info(f"RAG Query Parse: LLM extrahierte {len(params_dict['content_keywords'])} Content-Keyword(s): {params_dict['content_keywords']}")

            # Entferne subtags und organization falls vorhanden
            if 'subtags' in params_dict:
                del params_dict['subtags']

            if 'organization' in params_dict:
                del params_dict['organization']

            if debug:
                debug_info['parsed_params'] = params_dict

            return EventQueryParams(**params_dict), debug_info

        except Exception as e:
            logger.error(f"RAG Query Parse: Parsing fehlgeschlagen: {e}")
            if debug:
                debug_info['error'] = f"Parsing Error: {str(e)}"
            raise
    
    def build_django_query(self, params: EventQueryParams, device_id: str = None, category_id: int = None,
                           organization_ids: Optional[List[int]] = None):
        """
        Baut eine Django ORM Query aus den strukturierten Parametern

        Args:
            params: EventQueryParams mit Suchparametern
            device_id: Optional X-Device-ID vom Request Header
            category_id: Optional Kategorie-ID Filter (überschreibt Tags)
            organization_ids: Optional - nur Events dieser Organisationen durchsuchen.
                Wird VOR der Content- und Tag-Suche angewendet, damit das Limit
                (params.limit) nicht von fremden Organisationen aufgebraucht wird.
        """
        queryset = EventV4.objects.all()

        # Filter nach Veröffentlichungsstatus
        if params.published is not None:
            queryset = queryset.filter(published=params.published)

        # Nur zukünftige Events (standardmäßig)
        queryset = queryset.filter(is_past=False)

        # Organisationsfilter (z.B. Seite, die nur ausgewählte Veranstalter zeigt)
        if organization_ids:
            queryset = queryset.filter(source__organization_id__in=organization_ids)
            logger.info(f"RAG Query Build: Organisationsfilter aktiv: {organization_ids}")

        # Zeitfilter
        # Berücksichtige auch laufende Events über Occurrences:
        # Ein Event ist relevant wenn es HEUTE startet ODER wenn eine Occurrence
        # heute noch läuft (start_datetime <= Ende des Zeitraums UND end_datetime >= Start des Zeitraums)
        if params.start_date and params.end_date:
            queryset = queryset.filter(
                Q(start_date__gte=params.start_date, start_date__lte=params.end_date) |
                Q(occurrences__start_datetime__lte=params.end_date,
                  occurrences__end_datetime__gte=params.start_date)
            )
            logger.info(f"RAG Query Build: Zeitfilter mit Occurrence-Unterstützung ({params.start_date} bis {params.end_date})")
        elif params.start_date:
            queryset = queryset.filter(
                Q(start_date__gte=params.start_date) |
                Q(occurrences__end_datetime__gte=params.start_date)
            )
        elif params.end_date:
            queryset = queryset.filter(start_date__lte=params.end_date)

        # Ortsfilter
        # WICHTIG: Neue Area-Logik mit DB-Validierung
        # 1. Wenn Areas in der Query genannt wurden → validiere gegen DB
        # 2. Wenn Area in DB existiert → verwende sie
        # 3. Wenn Area NICHT in DB existiert → verwende als town-Filter (Fallback)
        # 4. Wenn keine Areas genannt → User-Area oder Default

        areas_to_filter = []
        town_fallback = None
        area_source = "none"

        if params.areas:
            # Validiere Areas gegen die Datenbank
            requested_areas = params.areas
            valid_areas = list(Area.objects.filter(name__in=requested_areas, visible=True).values_list('name', flat=True))

            if valid_areas:
                # Mindestens eine Area wurde in DB gefunden
                areas_to_filter = valid_areas
                area_source = "query"
                logger.info(f"RAG Query Build: Area(s) in DB gefunden: {valid_areas}")

                # Prüfe ob es ungültige Areas gab
                invalid_areas = [a for a in requested_areas if a not in valid_areas]
                if invalid_areas:
                    logger.info(f"RAG Query Build: Area(s) nicht in DB: {invalid_areas}")
            else:
                # Keine der angegebenen Areas existiert in DB → als town-Filter verwenden
                town_fallback = requested_areas[0]  # Erste Area als town
                area_source = "town_fallback"
                logger.info(f"RAG Query Build: Area '{town_fallback}' nicht in DB → verwende als town-Filter")

        # Wenn keine Areas aus Query → Fallback zu User-Areas oder Default
        if not areas_to_filter and not town_fallback:
            if device_id:
                try:
                    app_user = AppUser.objects.get(device_id=device_id)
                    if app_user.areas.exists():
                        areas_to_filter = list(app_user.areas.values_list('name', flat=True))
                        area_source = "user"
                        logger.info(f"RAG Query Build: Keine Area in Query → Verwende User-Areas: {areas_to_filter}")
                    else:
                        areas_to_filter = default_area_names()
                        area_source = "default"
                        logger.info(f"RAG Query Build: User hat keine Areas konfiguriert, verwende Standard: {areas_to_filter}")
                except AppUser.DoesNotExist:
                    areas_to_filter = default_area_names()
                    area_source = "default"
                    logger.info(f"RAG Query Build: Kein AppUser gefunden, verwende Standard: {areas_to_filter}")
            else:
                areas_to_filter = default_area_names()
                area_source = "default"
                logger.info(f"RAG Query Build: Keine Area und keine device_id, verwende Standard: {areas_to_filter}")

        # Filter anwenden
        if areas_to_filter:
            queryset = queryset.filter(area__name__in=areas_to_filter)

        if town_fallback:
            # Fallback: Suche nach town wenn Area nicht in DB existiert
            queryset = queryset.filter(town__icontains=town_fallback)

        # Area-Status für die Response merken (z.B. um das Standard-Gebiet sichtbar zu machen)
        self.last_area_source = area_source
        self.last_areas_filtered = list(areas_to_filter) if areas_to_filter else (
            [town_fallback] if town_fallback else []
        )

        if params.zip_code:
            queryset = queryset.filter(zip_code=params.zip_code)
        if params.location:
            queryset = queryset.filter(event_location__icontains=params.location)
        
        # ═══════════════════════════════════════════════════════════════
        # THEMEN-FILTER (Neue vereinfachte Logik)
        # ═══════════════════════════════════════════════════════════════

        tags_to_filter = []

        # 1. Tag-Requests (HÖCHSTE PRIORITÄT bei Themen)
        #    Allgemeine Begriffe wie "musik", "sport" → Fuzzy-Match auf Tags.
        #    ABER NUR wenn keine spezifischen content_keywords vorliegen: Bei z.B.
        #    "Punk Konzerte" würde ein zusätzlicher harter Filter auf den allgemeinen
        #    Tag "Musik & Konzerte" echte Punk-Events ausschließen, die als
        #    "Festival"/"Bühne" getaggt sind (UND-Verknüpfung schadet dem Recall).
        #    Das spezifische Keyword "punk" gewinnt → Tag-Filter wird übersprungen.
        if params.tag_request and not params.content_keywords:
            matched_tags = self._match_tag_requests(params.tag_request)
            if matched_tags:
                tags_to_filter.extend(matched_tags)
                logger.info(f"RAG Query Build: tag_request → {len(matched_tags)} Tags: {matched_tags}")
            else:
                # Kein Tag-Match gefunden → Verwende als content_keywords (Fallback)
                logger.warning(f"RAG Query: Keine Tags gefunden für tag_requests: {params.tag_request}")
                params.content_keywords = params.tag_request
                logger.info(f"RAG Query: Fallback zu content_keywords: {params.content_keywords}")
        elif params.tag_request and params.content_keywords:
            logger.info(
                f"RAG Query Build: tag_request {params.tag_request} wird NICHT als harter Tag-Filter "
                f"angewendet, da content_keywords {params.content_keywords} Vorrang haben. "
                f"Verbreiterung auf tag_request passiert ggf. gestuft in search() bei zu wenigen Treffern."
            )

        # 2. Category-ID Filter (überschreibt tag_request wenn vorhanden)
        if category_id is not None:
            from content.models import Tag

            # Prüfe ob tag_request vorhanden ist
            if params.tag_request and tags_to_filter:
                # Hole Tag-Objekte für die gematchten Tags
                matched_tag_objects = Tag.objects.filter(
                    name__in=tags_to_filter,
                    type='event'
                )

                # Filtere NUR Tags die zur angegebenen TagCategory gehören
                tag_names = list(
                    matched_tag_objects.filter(tagcategories_id=category_id).values_list('name', flat=True)
                )

                if tag_names:
                    # Tags gehören zur Category → verwende diese Tags
                    queryset = queryset.filter(tags__name__in=tag_names).distinct()
                    logger.info(f"RAG Query Build: Category {category_id} + tag_request → {len(tag_names)} passende Tags: {tag_names}")
                    tags_to_filter = []  # Reset, da schon angewendet
                else:
                    # Tags gehören NICHT zur Category → keine Events!
                    queryset = EventV4.objects.none()
                    logger.info(f"RAG Query Build: Tags aus tag_request ({tags_to_filter}) gehören nicht zu Category {category_id} → keine Events")
                    tags_to_filter = []
            else:
                # Keine tag_request oder kein Match → Zeige alle Events aus Category
                # Filtere direkt, DB gibt leeres Ergebnis wenn Category keine Event-Tags hat
                queryset = queryset.filter(tags__tagcategories_id=category_id).distinct()
                logger.info(f"RAG Query Build: TagCategory-ID {category_id} ohne tag_request")
                tags_to_filter = []  # Category-Filter überschreibt andere Tag-Filter

        # 3. Exakte Tags (aus LLM, nur wenn sehr spezifisch)
        #    Gleiche Regel wie bei tag_request oben: Sobald content_keywords vorliegen,
        #    gewinnt der spezifische Begriff. Ein zusaetzlicher harter Tag-Filter waere
        #    UND-verknuepft und schliesst passende Events aus, die anders getaggt sind
        #    (z.B. "flohmarkt" + Tag "Shoppen & Stoebern" -> 0 Treffer).
        elif params.tags and not params.content_keywords:
            tags_to_filter.extend(params.tags)
            logger.info(f"RAG Query Build: Exakte Tags aus LLM: {params.tags}")

        elif params.tags and params.content_keywords:
            logger.info(
                f"RAG Query Build: Exakte Tags {params.tags} werden NICHT als harter Tag-Filter "
                f"angewendet, da content_keywords {params.content_keywords} Vorrang haben."
            )

        # 4. Suggested Tags (Kontext-basiert, nur wenn keine anderen Themen-Filter)
        elif params.suggested_tags and not params.tag_request and not params.tags:
            tags_to_filter.extend(params.suggested_tags)
            logger.info(f"RAG Query Build: suggested_tags (Kontext): {params.suggested_tags}")

        # Wende Tag-Filter an (wenn welche vorhanden)
        if tags_to_filter and category_id is None:
            queryset = queryset.filter(tags__name__in=tags_to_filter).distinct()
            logger.info(f"RAG Query Build: Filtere nach {len(tags_to_filter)} Tag(s)")

            # Fallback für suggested_tags: Wenn keine Events gefunden → zeige alle
            if params.suggested_tags and not params.tag_request and not params.tags:
                if not queryset.exists():
                    logger.info("RAG Query Build: Keine Events mit suggested_tags → zeige alle Events")
                    # Reset queryset (nur Area + Zeit + published)
                    queryset = EventV4.objects.filter(published=True, is_past=False)
                    if organization_ids:
                        queryset = queryset.filter(source__organization_id__in=organization_ids)
                    if params.start_date:
                        queryset = queryset.filter(start_date__gte=params.start_date)
                    if params.end_date:
                        queryset = queryset.filter(start_date__lte=params.end_date)
                    if areas_to_filter:
                        queryset = queryset.filter(area__name__in=areas_to_filter)
                    if town_fallback:
                        queryset = queryset.filter(town__icontains=town_fallback)

        # Logging für semantische Felder
        if params.query_intent:
            logger.info(f"RAG Query Build: Query Intent: {params.query_intent}")
        if params.companion_context:
            logger.info(f"RAG Query Build: Companion Context: {params.companion_context}")

        # ═══════════════════════════════════════════════════════════════
        # KEYWORD-FILTER (Für spezifische Begriffe wie "punk", "Beyoncé")
        # ═══════════════════════════════════════════════════════════════

        # Keyword-Gruppen bestimmen: keyword_variants (LLM-generiert) hat Vorrang,
        # Fallback auf content_keywords als Einzel-Gruppen ohne Varianten
        keyword_groups = params.keyword_variants or (
            [[kw] for kw in params.content_keywords] if params.content_keywords else []
        )

        if keyword_groups:
            logger.info(f"RAG Query Build: Volltextsuche mit keyword_groups: {keyword_groups}")

            for group in keyword_groups:
                # OR über alle Varianten innerhalb einer Gruppe (Singular/Plural des gleichen Begriffs)
                # AND zwischen verschiedenen Gruppen (unterschiedliche Begriffe)
                group_filter = Q()
                for variant in group:
                    variant = variant.strip()
                    if not variant:
                        continue
                    # Wortgrenzen-Regex statt icontains: findet deutsche Komposita-Köpfe
                    # ("Bücherflohmarkt", "Sinfoniekonzert"), blockiert aber Präfix-Rauschen
                    # ("punk" != "Höhepunkt"/"Treffpunkt", "techno" != "Technologie").
                    pattern = keyword_to_regex(variant)
                    group_filter |= (
                        Q(title__iregex=pattern) |
                        Q(content__iregex=pattern) |
                        Q(tags__name__iregex=pattern) |
                        Q(event_location__iregex=pattern)
                    )
                if group_filter:
                    queryset = queryset.filter(group_filter)
                    logger.info(f"RAG Query Build: Gruppe {group} angewendet (Pattern-Matching mit Wortgrenzen)")

            queryset = queryset.distinct()
            logger.info("RAG Query Build: Keyword-Filter abgeschlossen")

        # Sortierung und Limit
        # distinct() nötig da Occurrence-Join Duplikate erzeugen kann
        queryset = queryset.distinct().order_by('start_date')
        if params.limit:
            queryset = queryset[:params.limit]

        return queryset


class EventSearchService:
    """
    High-Level Service für Event-Suche
    """

    def __init__(self):
        self.query_parser = NaturalLanguageEventQuery()

    @staticmethod
    def _ask_for_ids(system_prompt: str, user_prompt: str) -> List[int]:
        """Relevanz-Filter-Call: das Modell antwortet mit {"ids": [...]}."""
        result = llm.generate_json(system_prompt, user_prompt, schema=EVENT_IDS_JSON_SCHEMA)
        ids = []
        for value in result.get('ids') or []:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                logger.warning(f"Relevanz-Filter: Konnte ID nicht parsen: {value}")
        return ids

    def _apply_occurrence_dates(self, events: List[Dict], window_start, window_end) -> None:
        """
        Korrigiert das Anzeige-Datum für Events, die nur über eine Occurrence in den
        Zeitraum fallen (deren Basis-start_date also außerhalb [window_start, window_end] liegt).

        Für solche Events wird das Datum der matchenden Occurrence verwendet (statt der
        weit zurückliegenden Basis-start_date, z.B. bei Dauerausstellungen). Beginnt die
        Occurrence bereits VOR dem Fenster (laufendes Mehrtages-/Dauer-Event), wird das Event
        zusätzlich mit '_ongoing'=True markiert, damit es bei der Sortierung HINTER die Events
        gereiht wird, die tatsächlich im gefragten Zeitfenster beginnen.
        Mutiert die übergebene events-Liste in place.
        """
        out_of_window_ids = [
            e['id'] for e in events
            if e.get('start_date') and not (window_start <= e['start_date'] <= window_end)
        ]
        if not out_of_window_ids:
            return

        # Früheste das Fenster überlappende Occurrence je Event
        occ_rows = (
            Event_Occurrence.objects
            .filter(event_id__in=out_of_window_ids,
                    start_datetime__lte=window_end,
                    end_datetime__gte=window_start)
            .order_by('event_id', 'start_datetime')
            .values_list('event_id', 'start_datetime')
        )
        best_start = {}
        for event_id, start_dt in occ_rows:
            if event_id not in best_start:
                best_start[event_id] = start_dt

        for e in events:
            occ_start = best_start.get(e['id'])
            if occ_start is not None:
                e['start_date'] = occ_start
                # Begann bereits vor dem gefragten Fenster → laufendes Event, nach hinten sortieren
                if occ_start < window_start:
                    e['_ongoing'] = True

    def _filter_events_by_content(self, natural_query: str, events: List[Dict], tag_request: List[str]) -> List[int]:
        """
        Zweite KI-Filterung: Prüft anhand des Event-Contents, ob Events wirklich zur Suchanfrage passen.
        Filtert falsch getaggte Events heraus (z.B. "Musik"-Tag bei Lesungen).

        Args:
            natural_query: Die ursprüngliche Suchanfrage des Users
            events: Liste von Event-Dicts mit 'id', 'title', 'content'
            tag_request: Die verwendeten Tag-Suchwörter (z.B. ["musik"])

        Returns:
            Liste von Event-IDs, die wirklich zur Suchanfrage passen
        """
        if not events:
            return []

        # Baue kompakte Event-Liste für das Modell (HTML raus, sonst verschenken
        # Tags einen Großteil der 300 Zeichen Kontext)
        events_text = ""
        for i, event in enumerate(events, 1):
            title = event.get('title', 'Ohne Titel')
            content = strip_html(event.get('content', ''))[:300]
            event_id = event.get('id')
            events_text += f"{i}. [ID:{event_id}] {title}\n   {content}\n\n"

        # Prompt für Content-Filterung
        system_prompt = f"""Du bist ein Experte für Event-Relevanz-Bewertung.

AUFGABE:
Der User sucht nach: "{natural_query}"
Die erste Filterung hat Events mit dem Tag "{', '.join(tag_request)}" gefunden.

PROBLEM: Manche Events haben falsche Tags (z.B. "Musik"-Tag bei Lesungen mit Musik im Hintergrund).

Deine Aufgabe: Bewerte anhand von Titel und Beschreibung, welche Events WIRKLICH zur Suchanfrage passen.

REGELN:
- Ein Event ist relevant, wenn es HAUPTSÄCHLICH um das Suchthema geht
- Ein Event ist NICHT relevant, wenn das Thema nur nebensächlich erwähnt wird
- Beispiel "musik" Suche:
  ✓ RELEVANT: "Konzert", "Live-Musik", "DJ-Set", "Musikworkshop"
  ✗ NICHT RELEVANT: "Lesung mit musikalischer Begleitung", "Theater mit Hintergrundmusik"

OUTPUT-FORMAT:
JSON-Objekt mit der Liste der relevanten Event-IDs, z.B. {{"ids": [123, 456, 789]}}.
Wenn KEIN Event relevant ist: {{"ids": []}}."""

        user_prompt = f"""Hier sind die {len(events)} Events:

{events_text}

Welche Event-IDs sind wirklich relevant für die Suche "{natural_query}"?"""

        try:
            filtered_ids = self._ask_for_ids(system_prompt, user_prompt)
            logger.info(f"Content-Filter Antwort: {filtered_ids}")

            if not filtered_ids:
                logger.info("Content-Filter: Keine relevanten Events gefunden")
                return []

            logger.info(f"Content-Filter: {len(filtered_ids)} von {len(events)} Events als relevant bewertet")
            return filtered_ids

        except Exception as e:
            logger.error(f"Content-Filter: LLM-Call fehlgeschlagen: {e}")
            # Bei Fehler: Gib alle Event-IDs zurück (fail-safe)
            return [e['id'] for e in events]

    def _filter_events_by_context(self, natural_query: str, events: List[Dict],
                                   companion_context: str, query_intent: str = None) -> List[int]:
        """
        Kontextuelle Filterung: Prüft ob Events zum Kontext passen (Oma, Kind, Freunde etc.)
        Zweiter LLM-Call der die Ergebnisse nach Eignung filtert und rankt.

        Args:
            natural_query: Die ursprüngliche Suchanfrage
            events: Liste von Event-Dicts mit 'id', 'title', 'content'
            companion_context: "elderly", "children", "family", "friends", "alone"
            query_intent: "contextual", "recommendation", "direct"

        Returns:
            Liste von Event-IDs, sortiert nach Relevanz (beste zuerst)
        """
        if not events:
            return []

        # Kontext-Beschreibungen für den Prompt
        context_descriptions = {
            'elderly': 'eine ältere Person (Oma/Opa/Senioren). Geeignet: ruhige Events, Kultur, Theater, Konzerte (klassisch), Ausstellungen, Lesungen, Cafés. Ungeeignet: laute Clubs, Extremsport, Late-Night-Partys, anstrengende Outdoor-Aktivitäten.',
            'children': 'ein Kind/Kinder. Geeignet: Familienveranstaltungen, Kindertheater, Workshops, Bildung, Mitmach-Aktionen, Tierpark, Museen mit Kinderprogramm. Ungeeignet: Abend-Events, Bars, Nachtleben, komplexe Vorträge.',
            'family': 'die ganze Familie (verschiedene Altersgruppen). Geeignet: familienfreundliche Events, Feste, Märkte, Open-Air, Museen. Ungeeignet: reine Erwachsenen-Events, Nachtleben.',
            'friends': 'Freunde. Geeignet: Konzerte, Bars, Sport, Outdoor, Festivals, Kino, Escape Rooms. Ungeeignet: reine Kinder-Events.',
            'partner': 'einen romantischen Ausflug mit Partner/Freund/Freundin. Geeignet: Theater, Konzerte, Kino, Ausstellungen, gemütliche Events, Dinner-Events, Weinproben, Spaziergänge. Ungeeignet: reine Kinder-Events, laute Clubs (außer gewünscht), Sportevents.',
            'alone': 'einen Alleinunternehmung. Geeignet: Museen, Kino, Workshops, Lesungen, Kurse, Ausstellungen.'
        }

        context_desc = context_descriptions.get(companion_context, f'den Kontext "{companion_context}"')

        # Baue kompakte Event-Liste (HTML raus, siehe _filter_events_by_content)
        events_text = ""
        for i, event in enumerate(events[:50], 1):  # Max 50 Events für den LLM-Call
            title = event.get('title', 'Ohne Titel')
            content = strip_html(event.get('content', ''))[:200]
            location = event.get('event_location', '')
            event_id = event.get('id')
            events_text += f"{i}. [ID:{event_id}] {title} | Ort: {location}\n   {content}\n\n"

        system_prompt = f"""Du bist ein Experte für Event-Empfehlungen.

AUFGABE:
Der User sucht: "{natural_query}"
Der Kontext: Aktivität mit/für {context_desc}

Bewerte JEDES Event: Ist es für diesen Kontext geeignet?

REGELN:
- Wähle nur Events die WIRKLICH gut zum Kontext passen
- Sortiere nach Eignung (beste zuerst)
- Sei streng: Lieber weniger aber passende Ergebnisse
- Maximal 30 Events zurückgeben

OUTPUT: JSON-Objekt mit den Event-IDs (beste zuerst), z.B. {{"ids": [123, 456]}}.
Wenn KEIN Event passt: {{"ids": []}}."""

        user_prompt = f"""Hier sind {len(events[:50])} Events:

{events_text}

Welche Events passen am besten für: "{natural_query}"? (beste zuerst)"""

        try:
            filtered_ids = self._ask_for_ids(system_prompt, user_prompt)
            logger.info(f"Context-Filter Antwort: {filtered_ids}")

            if not filtered_ids:
                logger.info("Context-Filter: Keine passenden Events gefunden")
                return []

            logger.info(f"Context-Filter: {len(filtered_ids)} von {len(events[:50])} Events als passend bewertet für '{companion_context}'")
            return filtered_ids

        except Exception as e:
            logger.error(f"Context-Filter: LLM-Call fehlgeschlagen: {e}")
            return [e['id'] for e in events]

    def search(self, natural_query: str, device_id: str = None, debug: bool = False,
               date_start: str = None, date_end: str = None, limit: int = None,
               category_id: int = None, parsed_params: 'EventQueryParams' = None,
               organization_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        Hauptmethode für die Suche

        Args:
            natural_query: Die natürlichsprachliche Suchanfrage
            device_id: Optional X-Device-ID vom Request Header
            debug: Wenn True, werden Debug-Informationen zurückgegeben
            date_start: Optional - Startdatum Filter (YYYY-MM-DD), überschreibt LLM-Extraktion
            date_end: Optional - Enddatum Filter (YYYY-MM-DD), überschreibt LLM-Extraktion
            limit: Optional - Anzahl Ergebnisse (Standard: 50), überschreibt LLM-Extraktion
            category_id: Optional - Kategorie-ID Filter
            parsed_params: Optional - Bereits geparste Parameter (spart LLM-Call)
            organization_ids: Optional - nur Events dieser Organisationen durchsuchen
                (greift vor Content-/Tag-Suche und Limit, siehe build_django_query)
        """
        logger.info(f"RAG EVENT SEARCH: Verarbeite Anfrage='{natural_query}' debug={debug}")
        logger.info(f"RAG EVENT SEARCH: Request-Filter - date_start={date_start}, date_end={date_end}, limit={limit}, category_id={category_id}, organization_ids={organization_ids}")

        # 1. Parse natürliche Anfrage (oder verwende bereits geparste Parameter)
        if parsed_params is not None:
            params = parsed_params
            debug_info = None if not debug else {'reused_params': True}
            logger.info("RAG EVENT SEARCH: Verwende bereits geparste Parameter (kein LLM-Call)")
        else:
            params, debug_info = self.query_parser.parse_natural_query(natural_query, debug=debug)

        # 1b. INTENT-GATE: Anfragen, die keine Event-Suche sind (Supportfragen,
        # News/Wetter, Off-Topic), bekommen eine hilfreiche Meldung statt einer
        # beliebigen Event-Liste (vorher: "Wie gebe ich eine Adresse ein?" → 200 Events).
        if params.not_event_search:
            logger.info(f"RAG EVENT SEARCH: Intent-Gate - keine Event-Suche erkannt: '{natural_query}'")
            result = {
                'query': natural_query,
                'parsed_params': params.to_dict(),
                'results_count': 0,
                'events': [],
                'not_event_search': True,
                'message': (
                    'Diese Suche findet Veranstaltungen. Deine Anfrage sieht nicht nach '
                    'einer Event-Suche aus. Versuche es z.B. mit '
                    '"Konzerte am Wochenende" oder "Flohmarkt morgen".'
                ),
            }
            if debug and debug_info:
                result['debug_info'] = debug_info
            return result

        # 2. DEFAULT-ZEITRAUM: Wenn keine Daten extrahiert → heute bis +6 Monate
        # Verwende Europe/Berlin als Basis, konvertiere zu UTC für DB-Konsistenz
        import pytz
        berlin_tz = pytz.timezone('Europe/Berlin')
        now_berlin = datetime.now(berlin_tz)

        if not params.start_date and date_start is None:
            # Heute 00:00 Berliner Zeit → UTC
            start_of_today = berlin_tz.localize(datetime(now_berlin.year, now_berlin.month, now_berlin.day, 0, 0, 0))
            params.start_date = start_of_today.astimezone(pytz.utc)
            logger.info(f"RAG EVENT SEARCH: Kein Startdatum extrahiert, verwende Default: heute ({now_berlin.strftime('%Y-%m-%d')}, UTC: {params.start_date.strftime('%Y-%m-%d %H:%M')})")

        if not params.end_date and date_end is None:
            # Kontextuelle Queries (Oma, Kind etc.) → kürzeres Zeitfenster (4 Wochen)
            # Normale Queries → 6 Monate
            if params.query_intent == 'contextual':
                end_day = now_berlin + timedelta(days=28)
                time_label = "+4 Wochen (contextual)"
            else:
                end_day = now_berlin + timedelta(days=180)
                time_label = "+6 Monate"
            end_of_day = berlin_tz.localize(datetime(end_day.year, end_day.month, end_day.day, 23, 59, 59))
            params.end_date = end_of_day.astimezone(pytz.utc)
            logger.info(f"RAG EVENT SEARCH: Kein Enddatum extrahiert, verwende Default: {time_label} ({end_day.strftime('%Y-%m-%d')}, UTC: {params.end_date.strftime('%Y-%m-%d %H:%M')})")

        # 3. Request-Parameter haben PRIORITÄT über LLM-Extraktion UND Defaults
        if date_start is not None:
            import pytz
            try:
                # Konvertiere String zu timezone-aware datetime (Startzeit: 00:00:00 Berlin)
                naive_dt = datetime.strptime(date_start, '%Y-%m-%d')
                berlin_tz = pytz.timezone('Europe/Berlin')
                params.start_date = berlin_tz.localize(naive_dt)
                logger.info(f"RAG EVENT SEARCH: Überschreibe start_date mit Request-Wert: {params.start_date}")
            except ValueError as e:
                logger.warning(f"RAG EVENT SEARCH: Ungültiges date_start Format '{date_start}': {e}")

        if date_end is not None:
            import pytz
            try:
                # Konvertiere String zu timezone-aware datetime (Endzeit: 23:59:59 Berlin)
                naive_dt = datetime.strptime(date_end, '%Y-%m-%d')
                berlin_tz = pytz.timezone('Europe/Berlin')
                end_dt = naive_dt.replace(hour=23, minute=59, second=59)
                params.end_date = berlin_tz.localize(end_dt)
                logger.info(f"RAG EVENT SEARCH: Überschreibe end_date mit Request-Wert: {params.end_date}")
            except ValueError as e:
                logger.warning(f"RAG EVENT SEARCH: Ungültiges date_end Format '{date_end}': {e}")

        if limit is not None:
            params.limit = limit
            logger.info(f"RAG EVENT SEARCH: Überschreibe limit mit Request-Wert: {params.limit}")
        elif params.limit is None:
            # Fallback: Wenn LLM kein Limit gesetzt hat, verwende 50
            params.limit = 50
            logger.info(f"RAG EVENT SEARCH: Kein Limit gesetzt, verwende Standard: {params.limit}")

        # 3. Zeige finale Parameter
        logger.info(f"RAG EVENT SEARCH: Finale Parameter (nach Request-Override): {json.dumps(params.to_dict(), ensure_ascii=False)}")

        # 4. Zusätzliche Info über Areas
        if params.areas:
            logger.info(f"RAG EVENT SEARCH: Area-Status - Areas aus Query extrahiert: {params.areas}")
        else:
            logger.info("RAG EVENT SEARCH: Area-Status - Keine Areas in Query, Fallback zu User-Area")

        # 5. Zusätzliche Info über content_keywords
        if params.content_keywords:
            logger.info(f"RAG EVENT SEARCH: Content-Keywords für Volltextsuche: {params.content_keywords}")
        else:
            logger.info("RAG EVENT SEARCH: Keine Content-Keywords extrahiert")

        # 5b. Source/Location-Erkennung: Wenn LLM keine spezifischen Filter extrahiert hat,
        #     prüfe ob der Suchbegriff ein Quellenname oder Veranstaltungsort ist
        if not params.content_keywords and not params.tag_request and not params.tags:
            from content.models import Source
            # Extrahiere Suchtext aus Query (ohne Area-Namen)
            search_text = natural_query.strip()
            if params.areas:
                for area in params.areas:
                    search_text = search_text.replace(area, '').replace(area.lower(), '').replace(area.upper(), '')
            search_text = ' '.join(search_text.split()).strip()  # Normalize whitespace

            if search_text and len(search_text) >= 3:
                # Prüfe ob Suchtext zu einer Quelle passt
                matching_sources = Source.objects.filter(name__icontains=search_text, active=True)
                if matching_sources.exists():
                    source_names = list(matching_sources.values_list('name', flat=True))
                    logger.info(f"RAG EVENT SEARCH: Quelle erkannt! '{search_text}' → {source_names}")
                    # Setze als location-Filter (event_location) statt content_keywords
                    params.location = search_text
                else:
                    # Prüfe ob es ein Veranstaltungsort ist
                    location_count = EventV4.objects.filter(
                        event_location__icontains=search_text,
                        published=True, is_past=False
                    ).count()
                    if location_count > 0:
                        logger.info(f"RAG EVENT SEARCH: Veranstaltungsort erkannt! '{search_text}' → {location_count} Events")
                        params.location = search_text

        # 6. Baue Django Query
        try:
            queryset = self.query_parser.build_django_query(
                params, device_id=device_id, category_id=category_id, organization_ids=organization_ids
            )
            if queryset is not None:
                # Führe Query aus und hole Events mit Beschreibung für Content-Filterung
                events = list(queryset.values(
                    'id', 'title', 'content', 'start_date',
                    'event_location', 'town', 'zip_code'
                )[:params.limit])

                logger.info(f"RAG EVENT SEARCH: Gefundene Events vor Content-Filter: {len(events)}")

                # 6b. GESTUFTE VERBREITERUNG
                # Liefert die spezifische Keyword-Suche zu wenige Treffer (z.B.
                # "Technokonzert in Lübeck" → 3 Events), wird zusätzlich auf die vom
                # LLM mitgelieferte Oberkategorie (tag_request, z.B. "musik") verbreitert.
                # Keyword-Treffer bleiben vorne (_rank=0), Tag-Treffer folgen (_rank=1)
                # und werden per Content-Filter auf Relevanz zur Anfrage geprüft.
                broaden_notice = None
                if (params.content_keywords and params.tag_request
                        and len(events) < MIN_KEYWORD_RESULTS):
                    from dataclasses import replace as _dc_replace
                    keyword_hits = len(events)
                    logger.info(
                        f"RAG EVENT SEARCH: Nur {len(events)} Keyword-Treffer (<{MIN_KEYWORD_RESULTS}) "
                        f"→ Verbreiterung auf tag_request {params.tag_request}"
                    )
                    try:
                        broaden_params = _dc_replace(params, content_keywords=None, keyword_variants=None)
                        broaden_qs = self.query_parser.build_django_query(
                            broaden_params, device_id=device_id, category_id=category_id,
                            organization_ids=organization_ids
                        )
                        seen_ids = {e['id'] for e in events}
                        remaining_slots = max(0, (params.limit or 50) - len(events))
                        broadened_events = []
                        if broaden_qs is not None and remaining_slots > 0:
                            broadened_events = [
                                e for e in broaden_qs.values(
                                    'id', 'title', 'content', 'start_date',
                                    'event_location', 'town', 'zip_code'
                                )[:params.limit]
                                if e['id'] not in seen_ids
                            ][:remaining_slots]
                        logger.info(f"RAG EVENT SEARCH: Verbreiterung fand {len(broadened_events)} zusätzliche Events")

                        # Tag-Treffer auf Relevanz zur ursprünglichen Anfrage prüfen
                        # (gleiche Falsch-Tag-Problematik wie bei reiner Tag-Suche)
                        if broadened_events:
                            try:
                                relevant_ids = set(self._filter_events_by_content(
                                    natural_query=natural_query,
                                    events=broadened_events,
                                    tag_request=params.tag_request
                                ))
                                broadened_events = [e for e in broadened_events if e['id'] in relevant_ids]
                                logger.info(f"RAG EVENT SEARCH: Nach Content-Filter der Verbreiterung: {len(broadened_events)} Events")
                            except Exception as e:
                                logger.warning(f"RAG EVENT SEARCH: Content-Filter der Verbreiterung fehlgeschlagen: {e}")

                        for e in events:
                            e['_rank'] = 0
                        for e in broadened_events:
                            e['_rank'] = 1
                        events = events + broadened_events

                        # Transparenz: Bei 0 direkten Treffern erfährt der User,
                        # dass verwandte Events der Oberkategorie angezeigt werden
                        if keyword_hits == 0 and broadened_events:
                            kw_label = ', '.join(f'"{kw}"' for kw in params.content_keywords)
                            broaden_notice = (
                                f"Keine direkten Treffer für {kw_label} gefunden – "
                                f"es werden verwandte Events angezeigt."
                            )
                    except Exception as e:
                        logger.warning(f"RAG EVENT SEARCH: Gestufte Verbreiterung fehlgeschlagen: {e}")

                # Occurrence-bewusstes Anzeige-Datum:
                # Events, die nur über eine Occurrence in den Zeitraum fallen (Basis-start_date
                # liegt außerhalb), würden sonst mit ihrem Original-Datum (z.B. weit in der
                # Vergangenheit bei Dauerausstellungen) angezeigt. Ersetze durch das tatsächlich
                # matchende Occurrence-Datum innerhalb des Zeitfensters.
                if params.start_date and params.end_date and events:
                    self._apply_occurrence_dates(events, params.start_date, params.end_date)

                # 7. ZWEITE KI-FILTERUNG
                # A) Content-Filter: bei tag_request ohne content_keywords (falsch getaggte Events rausfiltern)
                # B) Context-Filter: bei contextual queries mit companion_context (Eignung prüfen)

                should_content_filter = (
                    params.tag_request and
                    not params.content_keywords and
                    events and
                    len(events) > 0
                )

                # Auch "recommendation"-Anfragen ("Ich würde gerne...") profitieren vom
                # Eignungs-Ranking, nicht nur explizit kontextuelle (Oma/Kind)-Anfragen
                should_context_filter = (
                    params.query_intent in ('contextual', 'recommendation') and
                    params.companion_context and
                    events and
                    len(events) > 0
                )

                if should_content_filter:
                    # A) Content-Filterung: Beim Standard-Limit (50) werden ALLE Events geprüft,
                    #    damit kein Mix aus geprüften und ungeprüften (evtl. falsch getaggten)
                    #    Events entsteht. Nur bei explizit höherem Limit bleibt ein ungeprüfter Rest.
                    MAX_FILTER = 50
                    events_to_filter = events[:MAX_FILTER]
                    events_remaining = events[MAX_FILTER:]

                    logger.info(f"RAG EVENT SEARCH: Starte Content-Filterung für {len(events_to_filter)} von {len(events)} Events (tag_request ohne keywords)")

                    try:
                        filtered_event_ids = self._filter_events_by_content(
                            natural_query=natural_query,
                            events=events_to_filter,
                            tag_request=params.tag_request
                        )

                        filtered_id_set = set(filtered_event_ids)
                        filtered_events = [e for e in events_to_filter if e['id'] in filtered_id_set]
                        events = filtered_events + events_remaining

                        logger.info(f"RAG EVENT SEARCH: Nach Content-Filter: {len(filtered_events)} von {len(events_to_filter)} geprüft, {len(events_remaining)} ungeprüft beibehalten, gesamt: {len(events)}")
                    except Exception as e:
                        logger.warning(f"RAG EVENT SEARCH: Content-Filterung fehlgeschlagen: {e}, verwende alle Events")

                elif should_context_filter:
                    # B) Context-Filterung: LLM prüft Eignung für Kontext (Oma, Kind, Freunde etc.)
                    logger.info(f"RAG EVENT SEARCH: Starte Context-Filterung für {len(events)} Events (companion: {params.companion_context})")

                    try:
                        filtered_event_ids = self._filter_events_by_context(
                            natural_query=natural_query,
                            events=events,
                            companion_context=params.companion_context,
                            query_intent=params.query_intent
                        )

                        # Sortiere Events nach LLM-Ranking (Reihenfolge der zurückgegebenen IDs)
                        events_by_id = {e['id']: e for e in events}
                        ranked_events = []
                        for eid in filtered_event_ids:
                            if eid in events_by_id:
                                ranked_events.append(events_by_id[eid])
                        events = ranked_events

                        logger.info(f"RAG EVENT SEARCH: Nach Context-Filter: {len(events)} passende Events für '{params.companion_context}'")
                    except Exception as e:
                        logger.warning(f"RAG EVENT SEARCH: Context-Filterung fehlgeschlagen: {e}, verwende alle Events")

                else:
                    if params.content_keywords:
                        logger.info(f"RAG EVENT SEARCH: Content-Filter übersprungen (content_keywords verwendet: {params.content_keywords})")
                    else:
                        logger.info("RAG EVENT SEARCH: Kein Filter nötig (kein tag_request, kein contextual query)")

                # 8. FALLBACK-SUCHE bei 0 Ergebnissen
                # Prüfe ob Suchbegriff zu einer Quelle, einem Veranstaltungsort oder Titel passt
                # Suche OHNE Area-Einschränkung
                # Die Fallback-Suche verwirft die Area. Richtig ist das nur, wenn der Ort
                # gar nicht aus der Anfrage stammt (Standard-Ort oder User-Areas) und der
                # Suchbegriff auf eine Quelle oder einen Veranstaltungsort zielt.
                # Hat der User dagegen selbst eine Stadt genannt, sind Treffer aus anderen
                # Orten schlicht falsch: "Flohmaerkte am Wochenende in Luebeck" lieferte so
                # Radolfzell und Ribbesbuettel. Dann bleibt die Liste leer.
                area_from_query = getattr(self.query_parser, 'last_area_source', None) in ('query', 'town_fallback')

                if len(events) == 0 and area_from_query:
                    genannte_orte = getattr(self.query_parser, 'last_areas_filtered', []) or params.areas or []
                    logger.info(
                        f"RAG EVENT SEARCH: 0 Ergebnisse fuer {genannte_orte} - Fallback-Suche "
                        f"uebersprungen, da der Ort aus der Anfrage stammt"
                    )

                elif len(events) == 0:
                    logger.info("RAG EVENT SEARCH: 0 Ergebnisse → Starte Fallback-Suche (ohne Area-Einschränkung)")

                    # Suchbegriffe zusammenstellen
                    fallback_terms = []
                    if params.content_keywords:
                        fallback_terms.extend(params.content_keywords)
                    # Auch den Originaltext versuchen (ohne Area-Namen)
                    raw_query = natural_query.strip()
                    if params.areas:
                        for area in params.areas:
                            raw_query = raw_query.replace(area, '').replace(area.lower(), '').replace(area.upper(), '')
                    raw_query = ' '.join(raw_query.split()).strip()
                    if raw_query and len(raw_query) >= 3 and raw_query.lower() not in [t.lower() for t in fallback_terms]:
                        fallback_terms.append(raw_query)

                    for term in fallback_terms:
                        if not term or len(term) < 3:
                            continue

                        # Basis-Filter für Fallback (ohne Area, aber MIT Organisationsfilter:
                        # sonst fuellen fremde Veranstalter das Limit, z.B. Suche "Ausstellung")
                        base_filter = Q(published=True, is_past=False)
                        if organization_ids:
                            base_filter &= Q(source__organization_id__in=organization_ids)
                        if params.start_date:
                            base_filter &= Q(start_date__gte=params.start_date)
                        if params.end_date:
                            base_filter &= Q(start_date__lte=params.end_date)

                        # Fallback 1: Quellenname
                        from content.models import Source
                        matching_sources = Source.objects.filter(name__icontains=term, active=True)
                        if matching_sources.exists():
                            source_names = list(matching_sources.values_list('name', flat=True))
                            fallback_qs = EventV4.objects.filter(base_filter, source__in=matching_sources)
                            events = list(fallback_qs.order_by('start_date').values(
                                'id', 'title', 'content', 'start_date',
                                'event_location', 'town', 'zip_code'
                            )[:params.limit])
                            if events:
                                logger.info(f"RAG EVENT SEARCH: Fallback Quelle '{term}' → {source_names} → {len(events)} Events")
                                break

                        # Fallback 2: Veranstaltungsort
                        fallback_qs = EventV4.objects.filter(base_filter, event_location__icontains=term)
                        events = list(fallback_qs.order_by('start_date').values(
                            'id', 'title', 'content', 'start_date',
                            'event_location', 'town', 'zip_code'
                        )[:params.limit])
                        if events:
                            logger.info(f"RAG EVENT SEARCH: Fallback Location '{term}' → {len(events)} Events")
                            break

                        # Fallback 3: Titel-Suche (ohne Area-Einschränkung)
                        fallback_qs = EventV4.objects.filter(base_filter, title__icontains=term)
                        events = list(fallback_qs.order_by('start_date').values(
                            'id', 'title', 'content', 'start_date',
                            'event_location', 'town', 'zip_code'
                        )[:params.limit])
                        if events:
                            logger.info(f"RAG EVENT SEARCH: Fallback Titel '{term}' → {len(events)} Events")
                            break

                    if len(events) == 0:
                        logger.info("RAG EVENT SEARCH: Auch Fallback-Suche ergab keine Ergebnisse")

                # Sortierung: Keyword-Treffer vor verbreiterten Tag-Treffern (_rank),
                # innerhalb dessen Events, die tatsächlich im gefragten Zeitfenster
                # beginnen, zuerst (nach Startzeit); laufende Events (Beginn davor,
                # z.B. samstags) danach. Nicht bei Context-Ranking – dort gilt die
                # LLM-Reihenfolge.
                if params.start_date and params.end_date and not should_context_filter:
                    events.sort(key=lambda e: (e.get('_rank', 0), e.get('_ongoing', False), e['start_date']))

                # Entferne 'content' und interne Felder aus finalen Results (nicht für API response)
                for event in events:
                    event.pop('content', None)
                    event.pop('_ongoing', None)
                    event.pop('_rank', None)

                area_source = getattr(self.query_parser, 'last_area_source', None)
                result = {
                    'query': natural_query,
                    'parsed_params': params.to_dict(),
                    'results_count': len(events),
                    'events': events,
                    'area_source': area_source,
                }

                # Standard-Ort transparent machen, wenn kein Ort erkannt/konfiguriert wurde
                notices = []
                areas = getattr(self.query_parser, 'last_areas_filtered', []) or []
                if area_source == 'default' and areas:
                    notices.append(
                        f"Kein Ort in der Anfrage erkannt – es werden Events aus "
                        f"{', '.join(areas)} (Standard) angezeigt."
                    )
                if broaden_notice:
                    notices.append(broaden_notice)
                if notices:
                    result['notice'] = ' '.join(notices)

                if debug and debug_info:
                    result['debug_info'] = debug_info

                return result
        except Exception as e:
            logger.error(f"RAG EVENT SEARCH: Django Query konnte nicht ausgeführt werden: {e}")

        result = {
            'query': natural_query,
            'parsed_params': params.to_dict(),
            'error': 'Django Query konnte nicht ausgeführt werden'
        }

        if debug and debug_info:
            result['debug_info'] = debug_info

        return result


def main():
    """
    Hauptfunktion zum Testen des Natural Language Event Query Systems
    """
    print("=" * 60)
    print("Natural Language Event Query System")
    print("=" * 60)
    
    if not llm.is_available():
        print(f"\n⚠ FEHLER: Sprachmodell-Server nicht erreichbar ({llm.base_url()})")
        print("Bitte Ollama starten und das Modell laden, z.B.: ollama pull gpt-oss:20b")
        return

    print(f"✓ Sprachmodell erreichbar: {llm.default_model()} @ {llm.base_url()}")

    try:
        # Initialisiere Service
        service = EventSearchService()
        
        # Beispiel-Anfragen zum Testen
        test_queries = [
            # "Konzerte morgen",
            # "Kinderveranstaltungen am Wochenende",
            # "Ausstellungen im Museum diese Woche",
            # "Sport Events in der Nähe von 28195",
            # "Was ist heute los?",
            # "Workshops zum Thema Programmierung im März",
            # "Theater nächste Woche",
            # "Party heute Abend",
            # "Alle Events vom Kulturzentrum",
            "Was läuft am Samstag musikalisch?",
        ]
        
        # Teste jede Anfrage
        for query in test_queries:
            result = service.search(query)

            # Mit device_id (verwendet Areas vom AppUser falls keine Area in Query)
            #result = service.search("Was läuft heute?", device_id="abc123")
            
            if 'events' in result and result['events']:
                print("\nErste 3 Events:")
                for i, event in enumerate(result['events'][:3], 1):
                    print(f"{i}. {event['title']}")
                    print(f"   Datum: {event['start_date']}")
                    if event.get('event_location'):
                        print(f"   Ort: {event['event_location']}")
                    if event.get('town'):
                        print(f"   Stadt: {event['town']}")
        
        # Interaktiver Modus
        print("\n" + "="*60)
        print("Interaktiver Modus - Geben Sie Ihre Anfragen ein (oder 'quit' zum Beenden):")
        print("="*60)
        
        while True:
            user_query = input("\n> Ihre Anfrage: ").strip()
            if user_query.lower() in ['quit', 'exit', 'q']:
                print("Auf Wiedersehen!")
                break
            
            if user_query:
                result = service.search(user_query)
                
                if 'events' in result and result['events']:
                    print(f"\n✓ {result['results_count']} Events gefunden")
                    print("\nErste Events:")
                    for i, event in enumerate(result['events'][:5], 1):
                        print(f"{i}. {event['title'][:80]}")
                        print(f"   Datum: {event['start_date']}")
                elif 'parsed_params' in result:
                    print("\nKeine Events gefunden mit diesen Parametern.")
                    print("Versuchen Sie eine andere Anfrage.")
    
    except Exception as e:
        print(f"\n⚠ Fehler beim Initialisieren: {e}")
        print("Bitte überprüfen Sie Ihre Datenbank-Konfiguration und Model-Imports")


if __name__ == "__main__":
    main()