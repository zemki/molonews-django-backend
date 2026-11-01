"""
Zentrale Duplikat-Erkennung für Event-Imports.

Diese Klasse implementiert cross-source Duplikat-Erkennung mit Prioritäts-basierter Entscheidungslogik:
1. Höhere Source-Priorität gewinnt (100=Original > 50=Standard > 20=Aggregator)
2. Bei gleicher Priorität: Ticket-Link → Bild → Mehr Content
3. Fuzzy Matching: Erkennt ähnliche Titel bei gleicher Zeit/Location
"""

import re
from logging import getLogger
from django.utils.timezone import localtime
from datetime import datetime, timedelta
from difflib import SequenceMatcher

logger = getLogger("molonews")


def normalize_string(s):
    """Normalize string by removing whitespace, line breaks, and converting to lowercase"""
    return ' '.join(str(s or '').replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').split()).strip().lower()


class DuplicateHandler:
    """Zentrale Klasse für cross-source Duplikat-Erkennung"""

    # Fuzzy Matching Schwellenwerte
    SIMILARITY_THRESHOLD = 0.75  # 75% String-Ähnlichkeit
    TOKEN_THRESHOLD = 0.60       # 60% gemeinsame Wörter
    TIME_TOLERANCE_MINUTES = 5   # ± 5 Minuten Zeittoleranz
    CONTAINED_MIN_CHARS = 8      # Mindestlaenge fuer die Enthalten-Regel

    @staticmethod
    def calculate_title_similarity(title1, title2):
        """
        Berechnet String-Ähnlichkeit zwischen zwei Titeln.

        Args:
            title1: Erster Titel
            title2: Zweiter Titel

        Returns:
            Float zwischen 0.0 und 1.0 (1.0 = identisch)
        """
        normalized1 = normalize_string(title1)
        normalized2 = normalize_string(title2)

        if not normalized1 or not normalized2:
            return 0.0

        return SequenceMatcher(None, normalized1, normalized2).ratio()

    @staticmethod
    def calculate_token_similarity(title1, title2):
        """
        Berechnet Wort-basierte Ähnlichkeit (Jaccard-Ähnlichkeit).

        Args:
            title1: Erster Titel
            title2: Zweiter Titel

        Returns:
            Float zwischen 0.0 und 1.0 (1.0 = alle Wörter identisch)
        """
        normalized1 = normalize_string(title1)
        normalized2 = normalize_string(title2)

        if not normalized1 or not normalized2:
            return 0.0

        # Wörter extrahieren (min. 2 Zeichen)
        words1 = set(w for w in normalized1.split() if len(w) >= 2)
        words2 = set(w for w in normalized2.split() if len(w) >= 2)

        if not words1 or not words2:
            return 0.0

        # Jaccard-Ähnlichkeit: Schnittmenge / Vereinigung
        intersection = len(words1 & words2)
        union = len(words1 | words2)

        return intersection / union if union > 0 else 0.0

    @staticmethod
    def is_title_contained(title1, title2):
        """
        Prueft, ob der kuerzere Titel vollstaendig im laengeren steckt.

        Aggregatoren stellen dem eigentlichen Titel oft die Reihe voran
        ("Theater 2026/2027 - Liebesjahre" gegen "Liebesjahre"). Beide
        Aehnlichkeitsmasse fallen dadurch ab, je laenger der Praefix ist -
        genau umgekehrt zur tatsaechlichen Sicherheit, dass es dasselbe
        Event ist. Der Treffer muss an Wortgrenzen liegen, damit "Chor"
        nicht "Chorprobe" trifft.

        Greift nur zusammen mit den Ort- und Zeitpruefungen des Aufrufers -
        allein waere die Regel zu weit.
        """
        n1 = normalize_string(title1)
        n2 = normalize_string(title2)
        if not n1 or not n2:
            return False

        kurz, lang = (n1, n2) if len(n1) <= len(n2) else (n2, n1)
        if len(kurz) < DuplicateHandler.CONTAINED_MIN_CHARS:
            return False

        return re.search(rf'(?<!\w){re.escape(kurz)}(?!\w)', lang) is not None

    @staticmethod
    def are_titles_similar(title1, title2):
        """
        Prüft ob zwei Titel ähnlich genug sind um als Duplikat zu gelten.

        Args:
            title1: Erster Titel
            title2: Zweiter Titel

        Returns:
            True wenn ähnlich genug, sonst False
        """
        string_sim = DuplicateHandler.calculate_title_similarity(title1, title2)
        token_sim = DuplicateHandler.calculate_token_similarity(title1, title2)

        # Titel sind ähnlich wenn ENTWEDER:
        # - String-Ähnlichkeit >= 75% ODER
        # - Token-Ähnlichkeit >= 60% ODER
        # - der kuerzere Titel vollstaendig im laengeren steckt
        return (string_sim >= DuplicateHandler.SIMILARITY_THRESHOLD or
                token_sim >= DuplicateHandler.TOKEN_THRESHOLD or
                DuplicateHandler.is_title_contained(title1, title2))

    @staticmethod
    def build_event_key(title, start_datetime, location=None):
        """
        Erstellt eindeutigen Schlüssel für Event-Vergleich.

        Args:
            title: Event-Titel
            start_datetime: Startdatum/zeit (datetime object)
            location: Optional - Veranstaltungsort

        Returns:
            String-Key für Duplikat-Vergleich
        """
        normalized_title = normalize_string(title)

        # EventV4.event_location ist auf 100 Zeichen begrenzt (Importer kürzen auf 99),
        # daher vor dem Normalisieren identisch kürzen, damit DB- und Feed-Seite
        # denselben Key ergeben.
        normalized_location = normalize_string(str(location)[:99]) if location else ""

        # Handle timezone-aware datetimes
        if hasattr(start_datetime, 'tzinfo') and start_datetime.tzinfo is not None:
            start_datetime = localtime(start_datetime)

        # For all-day events (starting at 00:00:00), only use the date
        if start_datetime.time() == datetime.min.time():
            date_part = start_datetime.strftime('%Y-%m-%d')
            key = f"{normalized_title}|{date_part}|all-day|{normalized_location}"
        else:
            key = f"{normalized_title}|{start_datetime.strftime('%Y-%m-%d %H:%M')}|{normalized_location}"

        return key

    @staticmethod
    def calculate_quality_score(event_or_dict):
        """
        Berechnet Qualitäts-Score für Tiebreaker bei gleicher Priorität.

        Scoring:
        - +10 wenn ticket_url vorhanden
        - +5 wenn image oder image_url vorhanden
        - +1 pro 100 Zeichen content (max +10)

        Args:
            event_or_dict: EventV4 Objekt oder Dictionary mit Event-Daten

        Returns:
            Integer Score (0-25)
        """
        score = 0

        # Handle both EventV4 objects and dictionaries
        if isinstance(event_or_dict, dict):
            ticket_url = event_or_dict.get('ticket_url')
            image = event_or_dict.get('image')
            image_url = event_or_dict.get('image_url')
            content = event_or_dict.get('content', '')
        else:
            ticket_url = getattr(event_or_dict, 'ticket_url', None)
            image = getattr(event_or_dict, 'image', None)
            image_url = getattr(event_or_dict, 'image_url', None)
            content = getattr(event_or_dict, 'content', '') or ''

        # Ticket-Link: +10 Punkte
        if ticket_url:
            score += 10

        # Bild: +5 Punkte
        if image or image_url:
            score += 5

        # Content-Länge: +1 pro 100 Zeichen (max 10)
        if content:
            score += min(len(content) // 100, 10)

        return score

    @staticmethod
    def should_replace_existing(existing_event, new_source, new_data):
        """
        Entscheidet ob bestehendes Event durch neues ersetzt werden soll.

        Logik:
        1. new_source.priority > existing.source.priority → ersetzen
        2. new_source.priority < existing.source.priority → nicht ersetzen
        3. Gleiche Priorität → Quality-Score vergleichen

        Args:
            existing_event: Bestehendes EventV4 Objekt
            new_source: Source Objekt der neuen Daten
            new_data: Dictionary mit neuen Event-Daten

        Returns:
            True wenn ersetzt werden soll, False sonst
        """
        existing_priority = existing_event.source.priority
        new_priority = new_source.priority

        # Höhere Priorität gewinnt
        if new_priority > existing_priority:
            logger.debug(f"Ersetze Event '{existing_event.title}': neue Priorität {new_priority} > {existing_priority}")
            return True

        if new_priority < existing_priority:
            logger.debug(f"Behalte Event '{existing_event.title}': bestehende Priorität {existing_priority} > {new_priority}")
            return False

        # Gleiche Priorität: Quality-Score vergleichen
        existing_score = DuplicateHandler.calculate_quality_score(existing_event)
        new_score = DuplicateHandler.calculate_quality_score(new_data)

        if new_score > existing_score:
            logger.debug(f"Ersetze Event '{existing_event.title}': neuer Score {new_score} > {existing_score}")
            return True

        logger.debug(f"Behalte Event '{existing_event.title}': bestehender Score {existing_score} >= {new_score}")
        return False

    @staticmethod
    def find_cross_source_duplicate(title, start_datetime, location=None, exclude_source=None, use_fuzzy=True):
        """
        Findet Duplikat über alle Sources hinweg.

        Zweistufige Suche:
        1. Exakter Match (normalisierter Titel + Zeit)
        2. Fuzzy Match (ähnlicher Titel + gleiche Zeit ± 5min + gleiche Location)

        Args:
            title: Event-Titel
            start_datetime: Startdatum/zeit
            location: Optional - Veranstaltungsort
            exclude_source: Optional - Source die ausgeschlossen werden soll
            use_fuzzy: Optional - Fuzzy Matching aktivieren (Standard: True)

        Returns:
            EventV4 Objekt wenn Duplikat gefunden, sonst None
        """
        from content.models import EventV4

        # Handle timezone-aware datetimes
        if hasattr(start_datetime, 'tzinfo') and start_datetime.tzinfo is not None:
            start_datetime_local = localtime(start_datetime)
        else:
            start_datetime_local = start_datetime

        # Query für potentielle Duplikate
        # Suche nach Events am gleichen Tag
        date_start = start_datetime_local.replace(hour=0, minute=0, second=0, microsecond=0)
        date_end = start_datetime_local.replace(hour=23, minute=59, second=59, microsecond=999999)

        queryset = EventV4.objects.filter(
            start_date__range=(date_start, date_end),
            is_past=False
        ).select_related('source')

        if exclude_source:
            queryset = queryset.exclude(source=exclude_source)

        # STUFE 1: Exakter Match (schnell)
        search_key = DuplicateHandler.build_event_key(title, start_datetime_local, location)

        for event in queryset:
            event_key = DuplicateHandler.build_event_key(
                event.title,
                localtime(event.start_date),
                event.event_location
            )

            if event_key == search_key:
                logger.debug(f"Exakter Duplikat-Match: '{event.title}' (ID: {event.id})")
                return event

        # STUFE 2: Fuzzy Match (nur wenn kein exakter Match und aktiviert)
        if not use_fuzzy:
            return None

        # Zeitfenster für Fuzzy Match: ± TIME_TOLERANCE_MINUTES
        time_tolerance = timedelta(minutes=DuplicateHandler.TIME_TOLERANCE_MINUTES)
        time_start = start_datetime_local - time_tolerance
        time_end = start_datetime_local + time_tolerance

        normalized_location = normalize_string(location) if location else None

        for event in queryset:
            event_datetime = localtime(event.start_date)

            # Prüfe Zeitfenster
            if not (time_start <= event_datetime <= time_end):
                continue

            # Prüfe Location (wenn vorhanden)
            event_location_norm = normalize_string(event.event_location) if event.event_location else None

            # Locations müssen entweder beide leer sein oder ähnlich
            if normalized_location and event_location_norm:
                # Beide haben Location - müssen ähnlich sein (mindestens 80% Übereinstimmung)
                location_sim = SequenceMatcher(None, normalized_location, event_location_norm).ratio()
                if location_sim < 0.8:
                    continue
            elif normalized_location or event_location_norm:
                # Nur eines hat Location - kein Match
                continue

            # Prüfe Titel-Ähnlichkeit
            if DuplicateHandler.are_titles_similar(title, event.title):
                string_sim = DuplicateHandler.calculate_title_similarity(title, event.title)
                token_sim = DuplicateHandler.calculate_token_similarity(title, event.title)

                logger.debug(
                    f"Fuzzy Duplikat-Match: '{event.title}' (ID: {event.id})\n"
                    f"  Neuer Titel: '{title}'\n"
                    f"  String-Ähnlichkeit: {string_sim:.2%}\n"
                    f"  Token-Ähnlichkeit: {token_sim:.2%}\n"
                    f"  Zeit-Differenz: {abs((event_datetime - start_datetime_local).total_seconds() / 60):.1f} min"
                )
                return event

        return None

    @staticmethod
    def locations_match(location1, location2):
        """
        Orte gelten als gleich, wenn beide leer sind oder sich zu mindestens 80% aehneln.
        Ein Ort nur auf einer Seite ist kein Match.
        """
        norm1 = normalize_string(location1) if location1 else None
        norm2 = normalize_string(location2) if location2 else None
        if norm1 and norm2:
            return SequenceMatcher(None, norm1, norm2).ratio() >= 0.8
        return not norm1 and not norm2

    @staticmethod
    def occurrence_matches(title, location, start_local, other_title, other_location, other_start_local, use_fuzzy=True):
        """
        Prueft, ob ein einzelner Termin zweier Events derselbe ist:
        exakter Schluessel (Titel + Zeit + Ort) oder Fuzzy-Match
        (aehnlicher Titel, Zeit +/- TIME_TOLERANCE_MINUTES, aehnlicher Ort).
        Beide Zeiten muessen in lokaler Zeit vorliegen.
        """
        if (DuplicateHandler.build_event_key(title, start_local, location)
                == DuplicateHandler.build_event_key(other_title, other_start_local, other_location)):
            return True
        if not use_fuzzy:
            return False
        tolerance = timedelta(minutes=DuplicateHandler.TIME_TOLERANCE_MINUTES)
        if abs(other_start_local - start_local) > tolerance:
            return False
        if not DuplicateHandler.locations_match(location, other_location):
            return False
        return DuplicateHandler.are_titles_similar(title, other_title)

    @staticmethod
    def find_cross_source_duplicates_for_occurrences(title, occurrence_starts, location=None,
                                                     exclude_source=None, use_fuzzy=True):
        """
        Termin-basierte Dublettensuche ueber alle Sources hinweg.

        Seit der Termin-Gruppierung hat ein Event viele Occurrences, waehrend
        Aggregatoren (Ticketportale, Stadtportale) pro Vorstellung ein eigenes
        Event anlegen. Ein Vergleich nur ueber das Startdatum trifft dann nur
        die erste Vorstellung. Hier wird jeder Termin des Events gegen die
        Occurrences fremder Events am selben Tag geprueft.

        Args:
            title: Event-Titel
            occurrence_starts: Startzeiten aller Termine des Events (aware datetimes)
            location: Optional - Veranstaltungsort
            exclude_source: Optional - eigene Source (wird uebersprungen)
            use_fuzzy: Fuzzy Matching aktivieren (Standard: True)

        Returns:
            Liste von dicts, je fremdes Event mit mindestens einem gemeinsamen Termin:
              event            - EventV4 (mit source)
              other_covered    - True, wenn ALLE Termine des fremden Events
                                 einen Termin dieses Events treffen
              new_covered      - True, wenn ALLE Termine dieses Events einen
                                 Termin des fremden Events treffen
              matched          - Anzahl gemeinsamer Termine
              other_total      - Anzahl Termine des fremden Events
        """
        from content.models import Event_Occurrence

        starts_local = sorted({localtime(s) if getattr(s, 'tzinfo', None) else s for s in occurrence_starts})
        if not starts_local:
            return []

        # Je Kalendertag eine Abfrage ueber die Occurrences fremder Events
        starts_by_day = {}
        for s in starts_local:
            starts_by_day.setdefault(s.date(), []).append(s)

        matched_other_ids = {}   # other_event_id -> set(other_occurrence_id)
        matched_new_starts = {}  # other_event_id -> set(eigene Startzeiten)
        other_events = {}

        for day, day_starts in starts_by_day.items():
            day_start = day_starts[0].replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            rows = (
                Event_Occurrence.objects
                .filter(start_datetime__gte=day_start, start_datetime__lt=day_end, event__is_past=False)
                .select_related('event', 'event__source')
            )
            if exclude_source:
                rows = rows.exclude(event__source=exclude_source)

            for occ in rows:
                other = occ.event
                other_start_local = localtime(occ.start_datetime)
                for s in day_starts:
                    if DuplicateHandler.occurrence_matches(
                        title, location, s, other.title, other.event_location, other_start_local, use_fuzzy
                    ):
                        other_events[other.id] = other
                        matched_other_ids.setdefault(other.id, set()).add(occ.id)
                        matched_new_starts.setdefault(other.id, set()).add(s)

        results = []
        for other_id, other in other_events.items():
            other_total = Event_Occurrence.objects.filter(event_id=other_id).count()
            matched = len(matched_other_ids[other_id])
            results.append({
                'event': other,
                'other_covered': matched >= other_total,
                'new_covered': len(matched_new_starts[other_id]) >= len(starts_local),
                'matched': matched,
                'other_total': other_total,
            })
        return results

    @staticmethod
    def handle_duplicate(existing_event, new_source, new_data, delete_callback=None):
        """
        Verarbeitet ein gefundenes Duplikat.

        Args:
            existing_event: Bestehendes EventV4 Objekt
            new_source: Source der neuen Daten
            new_data: Dictionary mit neuen Event-Daten
            delete_callback: Optional - Callback-Funktion zum Löschen (für Event_Occurrence etc.)

        Returns:
            'replace' wenn ersetzt werden soll (existing wurde gelöscht)
            'skip' wenn bestehendes behalten werden soll
        """
        if DuplicateHandler.should_replace_existing(existing_event, new_source, new_data):
            # Lösche bestehendes Event
            try:
                if delete_callback:
                    delete_callback(existing_event)
                else:
                    # Standard-Löschung mit Event_Occurrence
                    from content.models import Event_Occurrence
                    Event_Occurrence.objects.filter(event=existing_event).delete()
                    existing_event.delete()

                logger.info(f"Duplikat gelöscht: '{existing_event.title}' (Source: {existing_event.source.name}, Priorität: {existing_event.source.priority})")
                return 'replace'
            except Exception as e:
                logger.error(f"Fehler beim Löschen des Duplikats '{existing_event.title}': {e}")
                return 'skip'

        return 'skip'
