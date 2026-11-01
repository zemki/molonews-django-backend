"""
Import von Events aus einer JSON-Datei in eine Quelle.

Aufruf:
    python manage.py import_events_json <datei.json> <source_id> [--no-group] [--allow-mass-delete]

Format: eine Liste von Einträgen, ein Eintrag pro Termin, mit den Feldern
event_title, event_content, event_start_date, event_end_date (ISO-Datum),
event_link, event_image, event_ticket_link, event_categories (Liste),
location, street, zip_code, town, latitude, longitude, location_link.

Einträge mit gleichem Titel und gleichem Veranstaltungsort werden zu EINEM
Event mit mehreren Occurrences zusammengefasst (Ausstellungen, Theaterstücke,
Führungen). Bestehende Events werden über Titel + Ort wiedererkannt, so dass ein
Event seine Tags behält, auch wenn der erste Termin vorbei ist. Koordinaten aus
dem Feed landen direkt am Event. Mit --no-group entsteht ein Event pro Termin.

Derselbe Import wird vom Datei-Upload der API (api/views/import_file_upload.py)
verwendet.
"""

import html
import json
import os
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from logging import getLogger
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand, CommandError
from django.utils.timezone import localtime
from dateutil import parser
import pytz

from content.models import Source, EventV4, Event_Occurrence, Subtag, ImportRecord
from content.import_tools.import_records import (
    OCCURRENCES, covered_by_manual, create_record, looks_manual, manual_occurrences,
    manually_changed_fields, remember,
)
from content.utils.area_creation import get_or_create_area
from content.import_tools.import_utils import (
    clean_content, effective_location, fix_misencoded_text_deep, normalize_url,
)
from content.import_tools.duplicate_handler import DuplicateHandler
from content.import_tools.source_health import (
    clear_source_error, delete_guard_blocks, record_source_error, upcoming_count,
)

TARGET_TZ = pytz.timezone('Europe/Berlin')
logger = getLogger("molonews")

# Feed-Felder, die bei der Zusammenfassung mehrerer Einträge aus einem anderen
# Eintrag ergänzt werden, wenn der führende (früheste) Eintrag sie nicht hat.
MERGE_FILL_FIELDS = (
    "event_image", "event_ticket_link", "event_content", "event_link",
    "street", "zip_code", "latitude", "longitude", "location_link",
)

# Audiodeskription an einzelnen Terminen: Tags und Subtags gelten für ein ganzes
# Event, ein Termin kann keine eigenen tragen. Nennt nur ein Teil der Feed-Einträge
# eines Titels (am selben Ort) die Audiodeskription, wird die Gruppe deshalb
# aufgeteilt: Bei weniger als AD_SPLIT_MAX_OCCURRENCES kommenden Terminen wird
# jeder Termin ein eigenes Event und lässt sich einzeln verschlagworten. Bei mehr
# (z. B. Ausstellungen mit täglichen Einträgen) bleiben die übrigen Termine
# zusammen, und die Termine mit Audiodeskription bekommen ein eigenes Event mit
# Titelzusatz. Nennen alle oder keiner sie, bleibt alles beim Alten.
AUDIO_DESCRIPTION = re.compile(r"audio-?deskription", re.I)
AD_SPLIT_MAX_OCCURRENCES = 14
AD_VARIANT_SUFFIX = " – mit Audiodeskription"

# Subtags, die nachträglich vergeben werden (Redaktion oder externes Tagging).
# Sie kommen nie aus dem Feed und dürfen beim Subtag-Abgleich eines bestehenden
# Events nicht entfernt werden.
EDITORIAL_SUBTAG_NAMES = {"Audiodeskription", "Englische Sprache"}


def parse_to_berlin(dt_value):
    """Parse datetime value and convert to Berlin timezone"""
    if not dt_value:
        return None
    if isinstance(dt_value, str):
        if dt_value.strip().lower() in ("", "none", "null"):
            return None
        dt = parser.parse(dt_value)
    elif isinstance(dt_value, datetime):
        dt = dt_value
    else:
        return None

    if dt.tzinfo is None:
        dt = TARGET_TZ.localize(dt)
    else:
        dt = dt.astimezone(TARGET_TZ)
    return dt


def normalize_string(s):
    """Normalize string by removing whitespace, line breaks, and converting to lowercase"""
    return ' '.join(str(s or '').replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').split()).strip().lower()


def build_event_key(title, start_datetime, location=None):
    """Build unique event key for duplicate detection - uses central DuplicateHandler"""
    return DuplicateHandler.build_event_key(title, start_datetime, location)


def build_group_key(title, location=None):
    """Schlüssel für die Zusammenfassung von Terminen: Titel + Ort, ohne Datum."""
    normalized_location = normalize_string(str(location)[:99]) if location else ""
    return f"{normalize_string(title)}|{normalized_location}"


def truncate(s, length):
    if s is None:
        return ''  # Return empty string instead of None for NOT NULL fields
    s_str = str(s)
    return s_str[:length] if len(s_str) > length else s_str


def truncate_or_drop_url(s, length):
    """URL-Felder: lieber leer als kaputt abgeschnitten.

    normalize_url zieht vorher Leerzeichen gerade (siehe import_utils).
    """
    if s is None:
        return ''
    s_str = normalize_url(s)
    return s_str if len(s_str) <= length else ''


def parse_coord(value, lower, upper):
    """Feed-Koordinate -> Decimal. Leere, unlesbare oder unplausible Werte -> None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("none", "null", "nan"):
        return None
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if dec == 0 or not (lower <= dec <= upper):
        return None
    return dec


def coord_changed(old_value, new_value):
    """Decimal-sicherer Vergleich. Liefert der Feed nichts, bleibt der DB-Wert
    (z. B. vom Geocoding-Cron) unangetastet."""
    if new_value is None:
        return False
    if old_value is None:
        return True
    try:
        return abs(float(old_value) - float(new_value)) > 1e-6
    except (TypeError, ValueError):
        return True


def norm_minute(dt):
    """Auf Minute gekürzt in Berliner Zeit - für den Occurrence-Vergleich."""
    return dt.astimezone(TARGET_TZ).replace(second=0, microsecond=0)


def normalize_occurrence(start_datetime, end_datetime):
    """Endzeit normalisieren: fehlend oder vor dem Start -> Start; ganztägig (00:00) -> +1 Tag.

    Returns (start, end, feed_has_end)
    """
    feed_has_end = end_datetime is not None
    if not end_datetime or end_datetime < start_datetime:
        end_datetime = start_datetime
    if start_datetime == end_datetime and start_datetime.time() == datetime.min.time():
        end_datetime = end_datetime + timedelta(days=1)
        logger.debug(f"Adjusted end_datetime: added 1 day to midnight end time ({start_datetime})")
    return start_datetime, end_datetime, feed_has_end


def get_or_create_subtag(subtag_name, organization=None):
    """Get or create a subtag and associate it with an organization"""
    if not subtag_name or not isinstance(subtag_name, str):
        return None

    subtag_name = subtag_name.strip()
    if not subtag_name:
        return None

    # Decode HTML entities
    subtag_name = html.unescape(subtag_name)

    # Get or create subtag
    subtag, created = Subtag.objects.get_or_create(name=subtag_name)

    # Add organization to ManyToMany field (if not already present)
    if organization and not subtag.organizations.filter(id=organization.id).exists():
        subtag.organizations.add(organization)
        logger.info(f"Organization '{organization.name}' added to subtag '{subtag_name}'")

    if created:
        logger.info(f"New subtag created: '{subtag_name}'")
        if organization:
            logger.info(f"Organization '{organization.name}' added to new subtag '{subtag_name}'")

    return subtag


def process_event_categories(event_categories, organization=None):
    """Process event categories from JSON and create/get corresponding subtags"""
    subtags = []

    if not event_categories or not isinstance(event_categories, list):
        return subtags

    for category in event_categories:
        if isinstance(category, str) and category.strip():
            subtag = get_or_create_subtag(category.strip(), organization)
            if subtag and subtag not in subtags:
                subtags.append(subtag)

    return subtags


def feed_title(event):
    """Titel eines Feed-Eintrags so, wie group_feed_events ihn verwendet."""
    return truncate(str(event["event_title"]).replace("\\u2013", "-"), 300)


def mentions_audio_description(event):
    raw = f"{event.get('event_title', '')} {event.get('event_content', '')}"
    return bool(AUDIO_DESCRIPTION.search(html.unescape(re.sub(r"<[^>]+>", " ", str(raw)))))


def plan_audio_description_split(json_events):
    """Gruppen aufteilen, in denen nur ein Teil der Termine Audiodeskription hat.

    Bei vielen Terminen werden die Einträge mit Audiodeskription umbenannt (eigenes
    Event mit AD_VARIANT_SUFFIX). Zurück kommen die Gruppenschlüssel, deren Termine
    einzeln als Events angelegt werden sollen.
    """
    now = localtime()
    groups = defaultdict(list)
    for event in json_events:
        try:
            title = feed_title(event)
            start = parse_to_berlin(event.get("event_start_date"))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue  # unbrauchbare Einträge verwirft group_feed_events ohnehin
        if not start:
            continue
        location = effective_location(event.get("location"), event.get("street"), event.get("town"))
        groups[build_group_key(title, location)].append((event, start))

    single_keys = set()
    for key, entries in groups.items():
        marks = [mentions_audio_description(event) for event, _ in entries]
        if not any(marks) or all(marks):
            continue
        upcoming = {norm_minute(start) for _, start in entries if start >= now}
        if len(upcoming) < AD_SPLIT_MAX_OCCURRENCES:
            single_keys.add(key)
            logger.info(
                f"Audiodeskription nur an {sum(marks)} von {len(entries)} Terminen von "
                f"'{feed_title(entries[0][0])}': {len(upcoming)} kommende Termine werden einzelne Events"
            )
        else:
            for (event, _), mark in zip(entries, marks):
                if mark:
                    event["event_title"] = feed_title(event)[:300 - len(AD_VARIANT_SUFFIX)] + AD_VARIANT_SUFFIX
            logger.info(
                f"Audiodeskription nur an {sum(marks)} von {len(entries)} Terminen von "
                f"'{feed_title(entries[0][0])}': eigenes Event mit Titelzusatz für diese Termine"
            )
    return single_keys


def first_upcoming_start(db_event):
    """Frühester kommender Termin eines Events (für den Schlüssel einzeln geführter Termine)."""
    now = localtime()
    starts = [o.start_datetime for o in db_event.occurrences.all() if o.start_datetime >= now]
    return min(starts) if starts else db_event.start_date


def group_feed_events(json_events, group_occurrences=True, started_keys=None, single_keys=None):
    """Feed-Einträge (ein Eintrag pro Termin) zu Events mit Terminliste zusammenfassen.

    Gruppiert wird über Titel + Ort. Der Eintrag mit dem frühesten Termin liefert
    die Metadaten (Text, Bild, Adresse); fehlende Felder werden aus den anderen
    Einträgen der Gruppe ergänzt. Termine in der Vergangenheit und identische
    Termine innerhalb der Gruppe werden verworfen. Die Schlüssel der Einträge,
    deren Termin schon begonnen hat, sammelt started_keys (falls übergeben).

    Returns: Liste von dicts mit key, title, location, event (Rohdaten),
             occurrences [(start, end)], feed_has_end (bool)
    """
    now = localtime()
    groups = {}
    order = []
    skipped_past = 0
    skipped_invalid = 0
    in_group_duplicates = 0

    for event in json_events:
        try:
            title = truncate(str(event["event_title"]).replace("\\u2013", "-"), 300)
        except (KeyError, TypeError):
            logger.error(f"Error: Feed-Eintrag ohne event_title: {str(event)[:120]}")
            skipped_invalid += 1
            continue

        try:
            start_datetime = parse_to_berlin(event.get("event_start_date"))
            end_datetime = parse_to_berlin(event.get("event_end_date"))
        except (ValueError, OverflowError) as e:
            logger.error(f"Error: Unlesbares Datum bei Event '{title}': {e}")
            skipped_invalid += 1
            continue

        if not start_datetime:
            logger.error(f"Error: No start_datetime for event '{title}'!")
            skipped_invalid += 1
            continue

        start_datetime, end_datetime, feed_has_end = normalize_occurrence(start_datetime, end_datetime)

        # Ort wie fill_event_location bilden, damit Feed und Bestand denselben
        # Schlüssel ergeben (siehe effective_location)
        location = effective_location(event.get("location"), event.get("street"), event.get("town"))
        if group_occurrences:
            key = build_group_key(title, location)
            if single_keys and key in single_keys:
                # Termine dieser Gruppe einzeln führen (Audiodeskription nur an einigen)
                key = build_event_key(title, start_datetime, location)
        else:
            key = build_event_key(title, start_datetime, location)

        # Skip occurrences with start in the past - den Schlüssel aber merken: ein
        # laufender Termin (Messe über mehrere Tage, Ausstellung) steht weiter im
        # Feed und darf beim Abgleich nicht als verschwunden gelten.
        if start_datetime < now:
            logger.debug(f"Termin '{title}' übersprungen - start_date {start_datetime} liegt in der Vergangenheit")
            if started_keys is not None:
                started_keys.add(key)
            skipped_past += 1
            continue

        group = groups.get(key)
        if group is None:
            group = {
                "key": key,
                "title": title,
                "location": location,
                "event": event,
                "first_start": start_datetime,
                "occurrences": {},
                "feed_has_end": False,
            }
            groups[key] = group
            order.append(key)
        else:
            if start_datetime < group["first_start"]:
                # Frühester Termin führt; bisherige Felder als Lückenfüller behalten
                previous = group["event"]
                group["event"] = event
                group["first_start"] = start_datetime
                group["title"] = title
                for field in MERGE_FILL_FIELDS:
                    if not event.get(field) and previous.get(field):
                        event[field] = previous[field]
            else:
                leading = group["event"]
                for field in MERGE_FILL_FIELDS:
                    if not leading.get(field) and event.get(field):
                        leading[field] = event[field]

        occ_key = (norm_minute(start_datetime), norm_minute(end_datetime))
        if occ_key in group["occurrences"]:
            in_group_duplicates += 1
            logger.debug(f"In-Feed-Duplikat übersprungen: '{title}' ({start_datetime})")
            continue
        group["occurrences"][occ_key] = True
        group["feed_has_end"] = group["feed_has_end"] or feed_has_end

    grouped = []
    for key in order:
        group = groups[key]
        occurrences = sorted(group["occurrences"].keys())
        group["occurrences"] = occurrences
        grouped.append(group)

    merged_entries = sum(len(g["occurrences"]) for g in grouped) - len(grouped)
    logger.info(
        f"Feed gruppiert: {len(json_events)} Einträge -> {len(grouped)} Events "
        f"({merged_entries} Termine zusammengefasst, {skipped_past} vergangene Termine, "
        f"{in_group_duplicates} Feed-Duplikate, {skipped_invalid} ungültige Einträge)"
    )
    return grouped


def resolve_cross_source_duplicates(title, location, normalized_occurrences, source, event_data, allow_skip=True):
    """
    Fremd-Dubletten zu einem (gruppierten) Feed-Event aufloesen.

    Jeder Termin wird gegen die Occurrences fremder Events geprueft. Ein
    fremdes Event, dessen Termine alle von diesem Event abgedeckt sind, wird
    geloescht, wenn unsere Source gewinnt (hoehere Prioritaet, bei Gleichstand
    besserer Quality-Score). Deckt umgekehrt ein staerkeres fremdes Event alle
    unsere Termine ab, wird 'skip' zurueckgegeben (nur bei allow_skip). Fremde
    Events mit zusaetzlichen Terminen bleiben stehen.

    Returns:
        'skip' wenn das Feed-Event nicht angelegt werden soll, sonst 'create'
    """
    matches = DuplicateHandler.find_cross_source_duplicates_for_occurrences(
        title, [s for s, _ in normalized_occurrences], location, exclude_source=source
    )
    for match in matches:
        other = match['event']
        if match['other_covered']:
            action = DuplicateHandler.handle_duplicate(other, source, event_data)
            if action == 'replace':
                continue
        else:
            logger.debug(
                f"Fremd-Event '{other.title}' (ID {other.id}, {other.source.name}) hat "
                f"{match['other_total'] - match['matched']} Termine ohne Gegenstueck, bleibt stehen"
            )
        if allow_skip and match['new_covered'] and not DuplicateHandler.should_replace_existing(other, source, event_data):
            logger.info(
                f"Skipping event '{title}': Cross-source duplicate '{other.title}' "
                f"(ID {other.id}, {other.source.name}, Prioritaet {other.source.priority}) deckt alle Termine ab"
            )
            return 'skip'
    return 'create'


def make_db_key(group_occurrences, single_keys):
    """Schlüssel eines Bestandsevents, passend zu den Feed-Schlüsseln von group_feed_events."""
    def db_key(db_event):
        location = effective_location(db_event.event_location, db_event.street, db_event.town)
        if group_occurrences:
            key = build_group_key(db_event.title, location)
            if key not in single_keys:
                return key
            # Einzeln geführte Termine: über den (frühesten kommenden) Termin zuordnen,
            # sonst würden sie als Dubletten desselben Titels gelöscht
            return build_event_key(db_event.title, first_upcoming_start(db_event).astimezone(TARGET_TZ), location)
        return build_event_key(db_event.title, db_event.start_date.astimezone(TARGET_TZ), location)
    return db_key


def init_import_records(json_events, source, group_occurrences=True, dry_run=False):
    """Buchführung einer Quelle aus der zuletzt importierten Feed-Datei aufbauen.

    Der Bestand entspricht dem letzten Import plus dem, was seitdem von Hand angelegt
    wurde, plus eventuell Import-Resten. Was zum Feed passt oder im Import-Schub angelegt
    wurde, gehört dem Importer (Basis = aktueller Stand); was von Hand angelegt aussieht
    (App-Bild, einzeln angelegt), bekommt keinen Eintrag und bleibt unangetastet.
    Returns: (Liste eigener Event-IDs, Liste von Hand angelegter Events)
    """
    import copy
    feed = copy.deepcopy(json_events)
    single_keys = plan_audio_description_split(feed) if group_occurrences else set()
    db_key = make_db_key(group_occurrences, single_keys)
    grouped = group_feed_events(feed, group_occurrences=group_occurrences, started_keys=set(),
                                single_keys=single_keys)
    feed_keys = {group["key"] for group in grouped}
    owned, manual = [], []
    events = (EventV4.objects.filter(source=source, is_past=False)
              .select_related("source").prefetch_related("occurrences"))
    for db_event in events:
        key = db_key(db_event)
        # Ohne Feed-Gegenstück: von Hand angelegt nur, wenn es so aussieht (sonst Import-Rest)
        if key in feed_keys or not looks_manual(db_event):
            owned.append(db_event.id)
            if not dry_run and not ImportRecord.objects.filter(event=db_event).exists():
                create_record(db_event, source, key)
        else:
            manual.append(db_event)
    return owned, manual


def import_scraped_events(json_events, source, group_occurrences=True, allow_mass_delete=False):
    """Import all events from the feed and delete outdated events from database.

    Der Importer ändert und löscht nur seine eigenen Events (ImportRecord, siehe
    import_tools/import_records.py). In der App oder im Admin angelegte Events der
    Quelle bleiben stehen, und ihre Termine lässt er aus dem Feed weg. Felder seiner
    Events, die jemand von Hand geändert hat, überschreibt er nicht mehr; fällt ein
    solches Event aus dem Feed, wird es trotzdem gelöscht.
    """

    events_imported = 0
    events_updated = 0
    events_deleted = 0
    omitted_occurrences = 0

    # Get organization through the source
    organisation = source.organization
    if not organisation:
        logger.error(f"No organization found for source '{source.name}'")
        return 0

    default_tags = list(source.default_tags.all())
    default_areas = list(source.area.all())

    # Gruppen mit Audiodeskription nur an einzelnen Terminen aufteilen (muss vor der
    # Schlüsselbildung für den Bestand passieren, damit Feed und DB übereinstimmen)
    single_keys = plan_audio_description_split(json_events) if group_occurrences else set()

    db_key = make_db_key(group_occurrences, single_keys)

    def keep_rank(db_event):
        # kleinerer Rang = wird behalten
        return (0 if db_event.ai_tagged else 1, db_event.id)

    # Feed gruppieren - vor der Zuordnung, weil die Ersteinrichtung die Schlüssel braucht
    started_feed_keys = set()
    grouped_events = group_feed_events(
        json_events, group_occurrences=group_occurrences, started_keys=started_feed_keys,
        single_keys=single_keys,
    )
    feed_keys = {group["key"] for group in grouped_events}

    # 1. Bestand aufteilen: eigene Events (mit ImportRecord) und von Hand angelegte.
    existing_db_events = list(
        EventV4.objects.filter(source=source, is_past=False)
        .select_related("source")
        .prefetch_related("occurrences", "subtags")
    )
    records = {
        record.event_id: record
        for record in ImportRecord.objects.filter(event_id__in=[e.id for e in existing_db_events])
    }
    if ImportRecord.objects.filter(source=source).exists() or not existing_db_events:
        owned_events = [e for e in existing_db_events if e.id in records]
        manual_events = [e for e in existing_db_events if e.id not in records]
    else:
        # Ersteinrichtung: die Quelle hat noch keine Buchführung. Was im Feed steht,
        # gehört dem Importer; was fehlt, nur dann, wenn es nicht von Hand angelegt aussieht.
        owned_events, manual_events = [], []
        for db_event in existing_db_events:
            if db_key(db_event) in feed_keys or not looks_manual(db_event):
                owned_events.append(db_event)
            else:
                manual_events.append(db_event)
        logger.info(
            f"Ersteinrichtung der Import-Buchführung für '{source.name}': {len(owned_events)} eigene, "
            f"{len(manual_events)} von Hand angelegte Events"
        )
    if manual_events:
        logger.info(
            f"{len(manual_events)} von Hand angelegte Events in '{source.name}' bleiben unangetastet "
            f"(IDs {', '.join(str(e.id) for e in manual_events[:20])})"
        )

    # Mit Gruppierung fallen Alt-Events, die pro Termin einzeln angelegt wurden, auf
    # einen Schlüssel zusammen; behalten wird das bereits getaggte bzw. älteste Event,
    # die übrigen werden gelöscht und ihre Termine landen am behaltenen Event.
    # Betrifft nur eigene Events.
    owned_by_key = {}
    duplicate_events_to_delete = []
    for db_event in owned_events:
        base_key = db_key(db_event)
        existing_event = owned_by_key.get(base_key)
        if existing_event is None:
            owned_by_key[base_key] = db_event
        elif keep_rank(db_event) < keep_rank(existing_event):
            duplicate_events_to_delete.append(existing_event)
            owned_by_key[base_key] = db_event
        else:
            duplicate_events_to_delete.append(db_event)

    for duplicate_event in duplicate_events_to_delete:
        try:
            Event_Occurrence.objects.filter(event=duplicate_event).delete()
            duplicate_event.delete()
        except Exception as e:
            logger.error(f"Error deleting duplicate '{duplicate_event.title}': {e}")
    if duplicate_events_to_delete:
        logger.info(
            f"{len(duplicate_events_to_delete)} Alt-Events mit gleichem Titel/Ort zusammengeführt "
            f"(Quelle '{source.name}')"
        )
    deleted_ids = {e.id for e in duplicate_events_to_delete}
    owned_events = [e for e in owned_events if e.id not in deleted_ids]
    # Zuordnung zuerst über den beim letzten Import gemerkten Schlüssel (übersteht eine
    # Titeländerung von Hand), sonst über Titel + Ort (bzw. + Termin)
    owned_by_record_key = {}
    for db_event in owned_events:
        record = records.get(db_event.id)
        if record is not None:
            owned_by_record_key.setdefault(record.key, db_event)

    # Termine, die schon in einem von Hand angelegten Event stehen, aus dem Feed nehmen
    if manual_events:
        manual_occ = manual_occurrences(manual_events)
        kept_groups = []
        for group in grouped_events:
            remaining = []
            for occurrence in group["occurrences"]:
                manual_id = covered_by_manual(group["title"], group["location"], occurrence[0], manual_occ)
                if manual_id:
                    omitted_occurrences += 1
                    logger.info(
                        f"Termin {occurrence[0]:%d.%m.%Y %H:%M} von '{group['title']}' ausgelassen: "
                        f"von Hand eingetragen in Event {manual_id}"
                    )
                else:
                    remaining.append(occurrence)
            if remaining:
                group["occurrences"] = remaining
                kept_groups.append(group)
        grouped_events = kept_groups

    current_feed_event_keys = set()
    used_ids = set()

    # 2. Process all (grouped) events from the feed
    for group in grouped_events:
        event = group["event"]
        title = group["title"]
        location = group["location"]
        normalized_occurrences = group["occurrences"]
        feed_has_end_date = group["feed_has_end"]
        start_datetime = normalized_occurrences[0][0]
        base_event_key = group["key"]
        try:
            raw_content = event.get("event_content", "") or ""

            # Check if town exists - events without town cannot be imported
            town_name = event.get("town", '').strip() if event.get("town") else ''
            if not town_name:
                logger.warning(f"Event '{title}' übersprungen - keine Angabe von 'town' (Pflichtfeld)")
                continue

            latitude = parse_coord(event.get("latitude"), Decimal("-90"), Decimal("90"))
            longitude = parse_coord(event.get("longitude"), Decimal("-180"), Decimal("180"))
            if latitude is None or longitude is None:
                latitude = longitude = None

            # Versuche Area zu holen oder automatisch zu erstellen
            area, area_created = get_or_create_area(
                town_name=town_name,
                zip_code=event.get("zip_code"),
                latitude=event.get("latitude"),
                longitude=event.get("longitude")
            )

            if not area:
                logger.warning(f"Event '{title}' übersprungen - Area '{town_name}' nicht erstellbar")
                continue

            if area_created:
                logger.info(f"Neue Area '{town_name}' automatisch erstellt für Event '{title}'")

            current_feed_event_keys.add(base_event_key)
            existing_event = owned_by_record_key.get(base_event_key)
            if existing_event is None or existing_event.id in used_ids:
                existing_event = owned_by_key.get(base_event_key)
                if existing_event is not None and existing_event.id in used_ids:
                    existing_event = None
            if existing_event is not None:
                used_ids.add(existing_event.id)

            image = truncate_or_drop_url(event.get("event_image", ""), 600)
            ticket_link = truncate_or_drop_url(event.get("event_ticket_link") or "", 600)
            link = truncate_or_drop_url(event.get("event_link", ''), 600)
            street = truncate(event.get("street", ''), 100)
            zip_code = truncate(event.get("zip_code", ''), 10)
            town = truncate(event.get("town", ''), 100)

            processed_content = clean_content(raw_content).replace("\\u2013", "-")
            if processed_content.endswith('"'):
                processed_content = processed_content[:-1]

            # Process event_categories to subtags with organization
            event_categories = event.get("event_categories", [])
            subtags_from_categories = process_event_categories(event_categories, organisation)

            if existing_event:
                changes = []
                record = records.get(existing_event.id)
                if record is None:
                    record = create_record(existing_event, source, base_event_key)
                    records[existing_event.id] = record
                manual = manually_changed_fields(record, existing_event)
                new_manual = manual - set(record.manual_fields or [])
                if new_manual:
                    logger.info(
                        f"Event {existing_event.id} '{title}': von Hand geändert ({', '.join(sorted(new_manual))}) "
                        f"- der Feed überschreibt diese Felder nicht mehr"
                    )

                # Content comparison normalized (no invisible differences)
                if "content" not in manual and normalize_string(existing_event.content) != normalize_string(processed_content):
                    changes.append(f"Content: '{existing_event.content[:50]}...' -> '{processed_content[:50]}...'")
                    existing_event.content = processed_content

                for field, value, label in (
                    ("image_url", image, "Image"),
                    ("street", street, "Street"),
                    ("zip_code", zip_code, "ZIP"),
                    ("town", town, "Town"),
                    ("link", link, "Link"),
                    ("ticket_url", ticket_link, "Ticket"),
                ):
                    current = getattr(existing_event, field)
                    if field not in manual and current != value:
                        changes.append(f"{label}: '{current or 'EMPTY'}' -> '{value or 'EMPTY'}'")
                        setattr(existing_event, field, value)

                # Koordinaten aus dem Feed übernehmen (nur wenn der Feed welche liefert)
                if latitude is not None and (
                    coord_changed(existing_event.latitude, latitude)
                    or coord_changed(existing_event.longitude, longitude)
                ):
                    changes.append(f"Koordinaten: {existing_event.latitude},{existing_event.longitude} -> {latitude},{longitude}")
                    existing_event.latitude = latitude
                    existing_event.longitude = longitude
                    existing_event.geocoding_failed = False

                # Important: Only compare same timezone! (Termine von Hand geändert: Start bleibt)
                if OCCURRENCES not in manual and existing_event.start_date.astimezone(TARGET_TZ) != start_datetime:
                    time_diff = abs((existing_event.start_date.astimezone(TARGET_TZ) - start_datetime).total_seconds() / 60)
                    if time_diff > 5:
                        changes.append(f"Start: '{existing_event.start_date.astimezone(TARGET_TZ)}' -> '{start_datetime}' (Diff: {time_diff:.1f} min)")
                        existing_event.start_date = start_datetime

                # Update subtags
                current_subtags = set(existing_event.subtags.all())
                new_subtags = set(subtags_from_categories) | {
                    s for s in current_subtags if s.name in EDITORIAL_SUBTAG_NAMES
                }

                if current_subtags != new_subtags:
                    changes.append(f"Subtags: {len(current_subtags)} -> {len(new_subtags)}")
                    existing_event.subtags.set(new_subtags)

                # Termine abgleichen (Minutengenau). Liefert der Feed keine Endzeiten,
                # werden vorhandene Endzeiten in der DB nicht überschrieben.
                current_occurrences = {
                    (norm_minute(o.start_datetime), norm_minute(o.end_datetime))
                    for o in existing_event.occurrences.all()
                }
                occurrences_changed = False
                if OCCURRENCES not in manual:
                    if feed_has_end_date:
                        new_occurrences = {(norm_minute(s), norm_minute(e)) for s, e in normalized_occurrences}
                        occurrences_changed = current_occurrences != new_occurrences
                    else:
                        current_starts = {s for s, _ in current_occurrences}
                        new_starts = {norm_minute(s) for s, _ in normalized_occurrences}
                        occurrences_changed = current_starts != new_starts
                if occurrences_changed:
                    changes.append(f"Termine: {len(current_occurrences)} -> {len(normalized_occurrences)}")

                if changes:
                    existing_event.moddate = localtime()
                    existing_event.save()

                    if occurrences_changed:
                        # Vorhandene Endzeiten behalten, wenn der Feed keine liefert
                        existing_end_by_start = {s: e for s, e in current_occurrences}
                        Event_Occurrence.objects.filter(event=existing_event).delete()
                        Event_Occurrence.objects.bulk_create([
                            Event_Occurrence(
                                event=existing_event,
                                start_datetime=occ_start,
                                end_datetime=(
                                    occ_end if feed_has_end_date
                                    else existing_end_by_start.get(norm_minute(occ_start), occ_end)
                                ),
                            )
                            for occ_start, occ_end in normalized_occurrences
                        ])

                    events_updated += 1
                    logger.debug(f"Event '{title}' aktualisiert: {'; '.join(changes)[:300]}")

                # Stand merken (auch ohne Änderung: Schlüssel und Basis aktuell halten)
                remember(record, existing_event, base_event_key, manual)

                # Fremd-Dubletten mit niedrigerer Prioritaet auch bei bestehenden
                # Events entfernen (z.B. Aggregator-Eintraege, die seit dem letzten
                # Import dazugekommen sind)
                resolve_cross_source_duplicates(
                    title, location, normalized_occurrences, source,
                    {
                        'ticket_url': existing_event.ticket_url,
                        'image_url': existing_event.image_url,
                        'content': existing_event.content,
                    },
                    allow_skip=False,
                )

            else:
                # Create new event - but first check for cross-source duplicate
                new_event_data = {
                    'ticket_url': ticket_link,
                    'image_url': image,
                    'content': processed_content,
                }

                # Cross-source duplicate check ueber alle Termine des Events
                if resolve_cross_source_duplicates(
                    title, location, normalized_occurrences, source, new_event_data
                ) == 'skip':
                    continue

                try:
                    event_obj = EventV4.objects.create(
                        title=title,
                        content=processed_content,
                        start_date=start_datetime,
                        moddate=localtime(),
                        source=source,
                        event_location=truncate(location, 99),
                        image_url=image,
                        image_source=truncate(organisation.name or "", 200),
                        published=True,
                        street=street,
                        zip_code=zip_code,
                        town=town,
                        link=link,
                        ticket_url=ticket_link,
                        latitude=latitude,
                        longitude=longitude,
                    )

                    Event_Occurrence.objects.bulk_create([
                        Event_Occurrence(event=event_obj, start_datetime=occ_start, end_datetime=occ_end)
                        for occ_start, occ_end in normalized_occurrences
                    ])

                    # Add tags
                    for tag in default_tags:
                        event_obj.tags.add(tag)

                    # Add subtags from event_categories (with organization)
                    for subtag in subtags_from_categories:
                        event_obj.subtags.add(subtag)

                    # Add areas
                    if area:
                        event_obj.area.add(area)
                    else:
                        for a in default_areas:
                            event_obj.area.add(a)

                    # Buchführung: dieses Event gehört dem Importer
                    records[event_obj.id] = create_record(event_obj, source, base_event_key)

                    events_imported += 1
                    area_label = area.name if area else ', '.join(a.name for a in default_areas) or '-'
                    logger.info(
                        f"New event created: '{title}' with {len(normalized_occurrences)} occurrences and "
                        f"{len(subtags_from_categories)} subtags for organization '{organisation.name}' in area '{area_label}'"
                    )

                except Exception as e:
                    logger.error(f"Error creating event '{title}': {e}")
                    continue

        except Exception as e:
            logger.error(f"Error importing event '{title}': {e}")
            continue

    # 3. Eigene Events löschen, die nicht mehr im Feed stehen - auch von Hand geänderte.
    # Von Hand angelegte Events fasst der Importer nicht an. Ist der Feed leer oder
    # auffällig klein (abgebrochener Scraper, kaputte Datei), bleibt der Bestand stehen,
    # siehe source_health.delete_guard_blocks.
    skip_delete = delete_guard_blocks(
        source, upcoming_count(owned_events), len(current_feed_event_keys),
        allow_mass_delete,
    )
    # Lauf ohne Befund: einen früheren Fehlervermerk an der Quelle zurücknehmen
    if not skip_delete:
        clear_source_error(source)
    for db_event in owned_events:
        if skip_delete:
            break
        if db_event.id in used_ids:
            continue
        # Nennt der Feed das Event nur noch mit begonnenen Terminen und hat es auch
        # im Bestand keinen künftigen Termin mehr, läuft es gerade oder ist eben
        # vorbei - is_past setzt der stündliche Cron. Gelöscht wurden so bisher
        # laufende Events an ihrem ersten Tag.
        record = records.get(db_event.id)
        event_keys = {db_key(db_event)} | ({record.key} if record else set())
        if event_keys & started_feed_keys and not upcoming_count([db_event]):
            continue
        try:
            Event_Occurrence.objects.filter(event=db_event).delete()
            db_event.delete()
            events_deleted += 1
        except Exception as e:
            logger.error(f"Error deleting event '{db_event.title}': {e}")

    logger.info(
        f"Import completed: {events_imported} new events, {events_updated} updated events, "
        f"{events_deleted} deleted events, {len(manual_events)} von Hand angelegte Events unangetastet, "
        f"{omitted_occurrences} Feed-Termine wegen Handeintrag ausgelassen"
    )

    return events_imported


class Command(BaseCommand):
    help = "Import events from a JSON file using a specific source ID"

    def add_arguments(self, parser):
        parser.add_argument(
            'json_file',
            type=str,
            help='Path to the JSON file containing events'
        )
        parser.add_argument(
            'source_id',
            type=int,
            help='ID of the source to use for importing events'
        )
        parser.add_argument(
            '--no-group',
            action='store_true',
            help='Termine nicht zu Events zusammenfassen (altes Verhalten: ein Event pro Termin)'
        )
        parser.add_argument(
            '--allow-mass-delete',
            action='store_true',
            help='Löschschutz aufheben: auch dann abgleichen, wenn die Datei leer ist oder '
                 'weniger als die Hälfte des Bestands enthält (bewusst verkleinerte Quelle)'
        )

    def handle(self, *args, **options):
        json_file = options['json_file']
        source_id = options['source_id']
        group_occurrences = not options.get('no_group', False)
        allow_mass_delete = options.get('allow_mass_delete', False)

        # Check if JSON file exists
        if not os.path.exists(json_file):
            raise CommandError(f'JSON file "{json_file}" does not exist.')

        # Get the source
        try:
            source = Source.objects.get(id=source_id)
        except Source.DoesNotExist:
            raise CommandError(f'Source with ID "{source_id}" does not exist.')

        self.stdout.write(f'Loading events from "{json_file}" for source "{source.name}" (ID: {source_id})')

        # Load and process JSON file
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                json_events = json.load(f)
        except json.JSONDecodeError as e:
            record_source_error(source, f'Importdatei ist kein gültiges JSON: {e}')
            raise CommandError(f'Error parsing JSON file: {e}')
        except Exception as e:
            record_source_error(source, f'Importdatei nicht lesbar: {e}')
            raise CommandError(f'Error reading JSON file: {e}')

        if not isinstance(json_events, list):
            record_source_error(source, 'Importdatei enthält keine Liste von Events')
            raise CommandError('JSON file must contain a list of events.')

        # Fix encoding issues
        json_events = fix_misencoded_text_deep(json_events)

        self.stdout.write(f'Found {len(json_events)} events in JSON file')

        # Import events
        try:
            events_imported = import_scraped_events(
                json_events, source, group_occurrences=group_occurrences,
                allow_mass_delete=allow_mass_delete,
            )

            # Update source import date
            source.import_date = localtime()
            source.save()

            self.stdout.write(
                self.style.SUCCESS(
                    f'Successfully imported {events_imported} events from "{json_file}" '
                    f'for source "{source.name}"'
                )
            )
        except Exception as e:
            logger.error(f'Error during import: {e}')
            record_source_error(source, f'Import abgebrochen: {e}')
            raise CommandError(f'Error during import: {e}')
