import html
import json
import re
from datetime import datetime
from logging import getLogger

import pytz

from django.core.management.base import BaseCommand
from django.utils.timezone import (
    localtime,
)

from content.import_tools import script_generator
from content.import_tools.duplicate_handler import DuplicateHandler
from content.import_tools.import_utils import (
    clean_content, effective_location, fix_misencoded_text_deep, normalize_url,
)
from content.models import (
    EventV4, Event_Occurrence,
    Source, Subtag,
)
from content.utils.area_creation import get_or_create_area
from content.import_tools.source_health import (
    clear_source_error, delete_guard_blocks, record_source_error, upcoming_count,
)
TARGET_TZ = pytz.timezone('Europe/Berlin')
from dateutil import parser

logger = getLogger("molonews")


def parse_to_berlin(dt_value):
    if not dt_value:
        return None
    if isinstance(dt_value, str):
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
    # Entfernt Whitespace, Zeilenumbrüche, wandelt in Kleinbuchstaben um
    return ' '.join(str(s or '').replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').split()).strip().lower()

def build_event_key(title, start_datetime, location=None):
    # Verwendet die zentrale DuplicateHandler-Funktion
    return DuplicateHandler.build_event_key(title, start_datetime, location)

def get_or_create_subtag(subtag_name, organization=None):
    """
    Holt ein Subtag-Objekt oder erstellt es, falls es nicht existiert.
    
    Args:
        subtag_name (str): Name des Subtags
        organization (Organization): Die Organisation, die dem Subtag zugeordnet werden soll
        
    Returns:
        Subtag: Das Subtag-Objekt
    """
    if not subtag_name or not isinstance(subtag_name, str):
        return None
        
    subtag_name = subtag_name.strip()
    if not subtag_name:
        return None
    
    # HTML-Entities dekodieren
    subtag_name = html.unescape(subtag_name)
    
    # Subtag holen oder erstellen (ohne defaults da ManyToMany)
    subtag, created = Subtag.objects.get_or_create(name=subtag_name)
    
    # Organisation zum ManyToMany-Feld hinzufügen (falls noch nicht vorhanden)
    if organization and not subtag.organizations.filter(id=organization.id).exists():
        subtag.organizations.add(organization)
        logger.info(f"Organisation '{organization.name}' zu Subtag '{subtag_name}' hinzugefügt")
    
    if created:
        logger.info(f"Neues Subtag erstellt: '{subtag_name}'")
        if organization:
            logger.info(f"Organisation '{organization.name}' zu neuem Subtag '{subtag_name}' hinzugefügt")
    
    return subtag

def process_event_categories(event_categories, organization=None):
    """
    Verarbeitet die event_categories aus dem JSON und erstellt/holt entsprechende Subtags.
    
    Args:
        event_categories (list): Liste der Kategorien aus dem JSON
        organization (Organization): Die Organisation, die den Subtags zugeordnet werden soll
        
    Returns:
        list: Liste der Subtag-Objekte
    """
    subtags = []
    
    if not event_categories or not isinstance(event_categories, list):
        return subtags
    
    for category in event_categories:
        if isinstance(category, str) and category.strip():
               
            subtag = get_or_create_subtag(category.strip(), organization)
            if subtag and subtag not in subtags:
                subtags.append(subtag)
    
    return subtags

DEUTSCHE_MONATE = {
    'januar': 1, 'februar': 2, 'maerz': 3, 'märz': 3, 'april': 4, 'mai': 5,
    'juni': 6, 'juli': 7, 'august': 8, 'september': 9, 'oktober': 10,
    'november': 11, 'dezember': 12,
}

# "Musik-Lesung 25. September 2026" / "Bild-Ausstellung 18. Oktober - 08. November 2026"
_TITEL_DATUM = re.compile(
    r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöüß]+)\s*(\d{4})?', re.IGNORECASE
)


def parse_dates_from_title(title):
    """Datum aus dem Titel lesen, wenn der Feed keines liefert.

    Manche Quellen schreiben den Termin
    ausschliesslich in den Titel. Ohne diesen Rueckfall wurden solche Events
    mit "Kein start_datetime" verworfen.

    Gibt (start, ende) als naive datetime zurueck; ende nur bei Zeitraeumen
    ("18. Oktober - 08. November 2026"), sonst None.
    """
    if not title:
        return None, None

    treffer = []
    for m in _TITEL_DATUM.finditer(title):
        monat = DEUTSCHE_MONATE.get(m.group(2).lower())
        if not monat:
            continue
        treffer.append((int(m.group(1)), monat, int(m.group(3)) if m.group(3) else None))

    if not treffer:
        return None, None

    # Bei "18. Oktober - 08. November 2026" traegt nur die letzte Angabe das Jahr.
    jahre = [t[2] for t in treffer if t[2]]
    if not jahre:
        return None, None
    standard_jahr = jahre[-1]

    datumsliste = []
    for tag, monat, jahr in treffer:
        try:
            datumsliste.append(datetime(jahr or standard_jahr, monat, tag))
        except ValueError:
            continue

    if not datumsliste:
        return None, None
    if len(datumsliste) == 1:
        return datumsliste[0], None
    return datumsliste[0], datumsliste[-1]


def import_scraped_events(json_events, source):
    """
    Importiert alle Events aus dem Feed und löscht veraltete Events aus der Datenbank.
    """
    def truncate(s, length):
        if s is None:
            return ''  # Return empty string instead of None for NOT NULL fields
        return s[:length] if len(s) > length else s

    def truncate_or_drop_url(s, length):
        """URL-Felder: lieber leer als kaputt abgeschnitten.

        normalize_url zieht vorher Leerzeichen gerade - Feeds liefern sonst
        Adressen wie "http:// www.x.de" oder zwei URLs in einem Feld, die in
        der App nicht klickbar sind.
        """
        if s is None:
            return ''
        s = normalize_url(s)
        return s if len(s) <= length else ''

    events_imported = 0
    events_updated = 0
    events_deleted = 0
    events_without_date = 0

    # Organisation über die Source ermitteln
    organisation = source.organization
    if not organisation:
        logger.error(f"Keine Organisation für Source '{source.name}' gefunden")
        return 0

    default_tags = list(source.default_tags.all())
    default_areas = list(source.area.all())

    # 1. Hole alle existierenden Events der Quelle aus der DB
    existing_db_events = EventV4.objects.filter(source=source, is_past=False).select_related()
    
    db_events_by_key = {}
    duplicate_events_to_delete = []
    
    for db_event in existing_db_events:
        # Key: Titel + Datum+Uhrzeit + Location (für Duplikat-Erkennung).
        # Der Ort wird wie auf der Feed-Seite gebildet (siehe effective_location).
        base_key = build_event_key(
            db_event.title, localtime(db_event.start_date),
            effective_location(db_event.event_location, db_event.street, db_event.town),
        )
        if base_key in db_events_by_key:
            existing_event = db_events_by_key[base_key]
            if db_event.id > existing_event.id:
                duplicate_events_to_delete.append(existing_event)
                db_events_by_key[base_key] = db_event
            else:
                duplicate_events_to_delete.append(db_event)
        else:
            db_events_by_key[base_key] = db_event
    
    # Lösche die Duplikate sofort
    for duplicate_event in duplicate_events_to_delete:
        try:
            Event_Occurrence.objects.filter(event=duplicate_event).delete()
            duplicate_event.delete()
        except Exception as e:
            logger.error(f"Fehler beim Löschen des Duplikats '{duplicate_event.title}': {e}")

    current_feed_event_keys = set()

    # 2. Verarbeite alle Events aus dem Feed
    for event in json_events:
        try:
            title = truncate(event["event_title"].replace("\\u2013", "-"), 300)
            raw_content = event["event_content"]
            start_datetime = parse_to_berlin(event.get("event_start_date"))
            end_datetime = parse_to_berlin(event.get("event_end_date"))
            feed_has_end_date = end_datetime is not None

            # Fehlende Endzeit auf den Start setzen: end_datetime ist in der
            # Datenbank NOT NULL, sonst scheitert das Anlegen der Occurrence
            # und es bleibt ein Event ohne Termin zurueck.
            if start_datetime and not end_datetime:
                end_datetime = start_datetime

            if not start_datetime:
                # Rueckfall: manche Quellen schreiben den Termin nur in den Titel.
                titel_start, titel_ende = parse_dates_from_title(title)
                if titel_start:
                    start_datetime = parse_to_berlin(titel_start.isoformat())
                    if titel_ende and not feed_has_end_date:
                        end_datetime = parse_to_berlin(titel_ende.replace(hour=23, minute=59).isoformat())
                    elif not end_datetime:
                        end_datetime = start_datetime
                    logger.info(f"Datum aus Titel gelesen fuer Event '{title}': {start_datetime}")

            if not start_datetime:
                logger.error(f"Fehler: Kein start_datetime für Event '{title}'!")
                events_without_date += 1
                continue

            location = effective_location(event.get("location"), event.get("street"), event.get("town"))
            base_event_key = build_event_key(title, start_datetime, location)

            # Abgelaufene Events überspringen - sie sind is_past und stehen nicht im
            # DB-Lookup, würden also doppelt angelegt. Maßgeblich ist das Ende, nicht
            # der Beginn: Ein laufendes Event (Kurs am Vormittag, "Touren im Oktober")
            # steht weiter im Feed und wurde sonst beim Lauf um 13 Uhr als verschwunden
            # gelöscht. Steht ein abgelaufenes noch im Bestand (is_past setzt erst der
            # stündliche Cron), bleibt es aus demselben Grund stehen.
            if max(start_datetime, end_datetime or start_datetime) < localtime():
                if base_event_key in db_events_by_key:
                    current_feed_event_keys.add(base_event_key)
                continue

            current_feed_event_keys.add(base_event_key)
            existing_event = db_events_by_key.get(base_event_key)

            image = truncate_or_drop_url(event.get("event_image", ""), 600)
            ticket_link = truncate_or_drop_url(event.get("event_ticket_link", ""), 600)

            processed_content = clean_content(raw_content).replace("\\u2013", "-")
            if ticket_link:
                processed_content += f'<p>Tickets: {ticket_link}</p>'
            if processed_content.endswith('"'):
                processed_content = processed_content[:-1]

            # Verarbeite event_categories zu Subtags mit Organisation
            event_categories = event.get("event_categories", [])
            subtags_from_categories = process_event_categories(event_categories, organisation)

            if existing_event:
                changes = []

                # Content-Vergleich normalisiert (kein unsichtbarer Unterschied)
                if normalize_string(existing_event.content) != normalize_string(processed_content):
                    changes.append(f"Content: '{existing_event.content[:50]}...' -> '{processed_content[:50]}...'")
                    existing_event.content = processed_content
                    
                # Location-Vergleich normalisiert
                new_location = truncate(location, 99)
                if existing_event.event_location != new_location:
                    old_loc = existing_event.event_location or "LEER"
                    new_loc = new_location or "LEER"
                    changes.append(f"Location: '{old_loc}' -> '{new_loc}'")
                    existing_event.event_location = new_location

                if existing_event.image_url != image:
                    old_image = existing_event.image_url or "LEER"
                    new_image = image or "LEER"
                    changes.append(f"Image: '{old_image}' -> '{new_image}'")
                    existing_event.image_url = image
                    
                if existing_event.street != truncate(event.get("street", ''), 100):
                    old_street = existing_event.street or "LEER"
                    new_street = truncate(event.get("street", ''), 100) or "LEER"
                    changes.append(f"Street: '{old_street}' -> '{new_street}'")
                    existing_event.street = truncate(event.get("street", ''), 100)
                    
                if existing_event.zip_code != truncate(event.get("zip_code", ''), 10):
                    old_zip = existing_event.zip_code or "LEER"
                    new_zip = truncate(event.get("zip_code", ''), 10) or "LEER"
                    changes.append(f"ZIP: '{old_zip}' -> '{new_zip}'")
                    existing_event.zip_code = truncate(event.get("zip_code", ''), 10)
                    
                if existing_event.town != truncate(event.get("town", ''), 100):
                    old_town = existing_event.town or "LEER"
                    new_town = truncate(event.get("town", ''), 100) or "LEER"
                    changes.append(f"Town: '{old_town}' -> '{new_town}'")
                    existing_event.town = truncate(event.get("town", ''), 100)
                    
                if existing_event.link != truncate_or_drop_url(event.get("event_link", ''), 600):
                    old_link = existing_event.link or "LEER"
                    new_link = truncate_or_drop_url(event.get("event_link", ''), 600) or "LEER"
                    changes.append(f"Link: '{old_link}' -> '{new_link}'")
                    existing_event.link = truncate_or_drop_url(event.get("event_link", ''), 600)
                    
                if existing_event.ticket_url != ticket_link:
                    old_ticket = existing_event.ticket_url or "LEER"
                    new_ticket = ticket_link or "LEER"
                    changes.append(f"Ticket: '{old_ticket}' -> '{new_ticket}'")
                    existing_event.ticket_url = ticket_link

                # Wichtig: Nur gleiche Zeitzone vergleichen!
                if existing_event.start_date.astimezone(TARGET_TZ) != start_datetime:
                    time_diff = abs((existing_event.start_date.astimezone(TARGET_TZ) - start_datetime).total_seconds() / 60)
                    if time_diff > 5:
                        changes.append(f"Start: '{existing_event.start_date.astimezone(TARGET_TZ)}' -> '{start_datetime}' (Diff: {time_diff:.1f} min)")
                        existing_event.start_date = start_datetime
                
                #print changes
                #if changes:
                #    print(changes)
                
                
                # Subtags aktualisieren
                current_subtags = set(existing_event.subtags.all())
                new_subtags = set(subtags_from_categories)
                
                if current_subtags != new_subtags:
                    changes.append(f"Subtags: {len(current_subtags)} -> {len(new_subtags)}")
                    existing_event.subtags.set(new_subtags)
                
                if changes:
                    existing_event.moddate = localtime()
                    existing_event.save()
                    
                    occurrence = Event_Occurrence.objects.filter(event=existing_event).first()
                    if occurrence:
                        occurrence_changes = []
                        occ_start = occurrence.start_datetime.astimezone(TARGET_TZ) if occurrence.start_datetime else None
                        occ_end = occurrence.end_datetime.astimezone(TARGET_TZ) if occurrence.end_datetime else None

                        if occ_start != start_datetime:
                            start_diff = abs(((occ_start or start_datetime) - start_datetime).total_seconds() / 60)
                            if start_diff > 5:
                                occurrence_changes.append(f"Occurrence Start: '{occ_start}' -> '{start_datetime}' (Diff: {start_diff:.1f} min)")
                                occurrence.start_datetime = start_datetime
                        if feed_has_end_date and occ_end != end_datetime:
                            end_diff = abs(((occ_end or end_datetime) - end_datetime).total_seconds() / 60)
                            if end_diff > 5:
                                occurrence_changes.append(f"Occurrence End: '{occ_end}' -> '{end_datetime}' (Diff: {end_diff:.1f} min)")
                                occurrence.end_datetime = end_datetime
                        if occurrence_changes:
                            occurrence.save()
                    
                    events_updated += 1

            else:
                # Neues Event erstellen - aber zuerst cross-source Duplikat prüfen
                new_event_data = {
                    'ticket_url': ticket_link,
                    'image_url': image,
                    'content': processed_content,
                }

                # Cross-source Duplikat-Prüfung
                cross_source_duplicate = DuplicateHandler.find_cross_source_duplicate(
                    title, start_datetime, location, exclude_source=source
                )

                if cross_source_duplicate:
                    # Entscheiden ob wir ersetzen oder überspringen
                    action = DuplicateHandler.handle_duplicate(
                        cross_source_duplicate, source, new_event_data
                    )
                    if action == 'skip':
                        logger.info(f"Überspringe Event '{title}': Cross-source Duplikat mit höherer/gleicher Priorität existiert")
                        continue
                    # Bei 'replace' wurde das Duplikat gelöscht, wir erstellen das neue Event

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
                        published=source.default_published,
                        street=truncate(event.get("street", ''), 100),
                        zip_code=truncate(event.get("zip_code", ''), 10),
                        town=truncate(event.get("town", ''), 100),
                        link=truncate_or_drop_url(event.get("event_link", ''), 600),
                        ticket_url=ticket_link,
                        language=event.get("language") or "de",
                    )

                    Event_Occurrence.objects.create(
                        event=event_obj,
                        start_datetime=start_datetime,
                        end_datetime=end_datetime
                    )

                    # Tags hinzufügen
                    for tag in default_tags:
                        event_obj.tags.add(tag)

                    # Subtags aus event_categories hinzufügen (mit Organisation)
                    for subtag in subtags_from_categories:
                        event_obj.subtags.add(subtag)

                    # Areas hinzufügen
                    area = None
                    if event.get("town"):
                        area, _ = get_or_create_area(
                            event.get("town"),
                            zip_code=event.get("zip_code"),
                            latitude=event.get("latitude"),
                            longitude=event.get("longitude"),
                            create_if_not_found=True,
                        )
                    if area:
                        event_obj.area.add(area)
                    else:
                        for a in default_areas:
                            event_obj.area.add(a)

                    events_imported += 1
                    logger.info(f"Neues Event erstellt: '{title}' mit {len(subtags_from_categories)} Subtags für Organisation '{organisation.name}'")

                except Exception as e:
                    logger.error(f"Fehler beim Erstellen des Events '{title}': {e}")
                    continue

        except Exception as e:
            logger.error(f"Fehler beim Importieren des Events '{event.get('event_title')}': {e}")
            continue

    # 3. Lösche Events aus der Datenbank, die nicht mehr im Feed vorhanden sind
    # Schutz vor Massenlöschung: Die Scraper geben bei einem Abruffehler (Timeout,
    # HTTP-Fehler, geändertes Seitenlayout) eine leere oder abgeschnittene Liste
    # zurück. Dann bleibt der Bestand stehen - siehe source_health.
    skip_delete = delete_guard_blocks(
        source, upcoming_count(db_events_by_key.values()), len(current_feed_event_keys)
    )

    for event_key, db_event in db_events_by_key.items():
        if skip_delete:
            break
        if event_key not in current_feed_event_keys:
            try:
                Event_Occurrence.objects.filter(event=db_event).delete()
                db_event.delete()
                events_deleted += 1
            except Exception as e:
                logger.error(f"Fehler beim Löschen des Events '{db_event.title}': {e}")

    # Ein Scraper, der zur Hälfte keine Termine mehr erkennt, läuft formal durch -
    # die Quelle verliert aber still ihre Events.
    dates_missing = bool(events_without_date) and events_without_date * 2 >= len(json_events)
    if dates_missing:
        record_source_error(
            source, f"{events_without_date} von {len(json_events)} Einträgen ohne lesbares Startdatum"
        )
    elif json_events and not skip_delete:
        # Lauf ohne Befund: einen früheren Fehlervermerk zurücknehmen. Bei leerem
        # Ergebnis bleibt er stehen - das vermerkt der Aufrufer.
        clear_source_error(source)

    logger.info(f"Import abgeschlossen: {events_imported} neue Events, {events_updated} aktualisierte Events, {events_deleted} gelöschte Events")
    
    return events_imported


KIND = script_generator.KIND_EVENTS

# Quellentypen mit generierten Import-Scripts
SCRIPT_SOURCE_TYPES = ("html-event", "ai-event")


def run_source_script(source):
    """Führt das freigegebene Import-Script einer Quelle aus und gibt die Events zurück."""
    events = script_generator.run_source_script(source, KIND)
    return fix_misencoded_text_deep(events)


def note_pending_approval(source):
    """Vermerkt an der Quelle, dass das Script auf Freigabe wartet (ohne Zeitstempel)."""
    note = script_generator.approval_note(source)
    if source.import_errors != note:
        source.import_errors = note
        source.save(update_fields=["import_errors"])
    logger.info(f"Quelle {source.id}: {note}")


def write_preview(source, filename):
    """Schreibt das Ergebnis eines Probelaufs als Vorschau und verlinkt es an der Quelle."""
    events = run_source_script(source)
    with open(script_generator.preview_path(KIND, filename), "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, default=str, ensure_ascii=False)
    source.result = script_generator.preview_url(KIND, filename)
    source.save()


def prepare_scripts(source_type):
    """Phase 1+2: fehlende Scripts generieren und Vorschau erzeugen (alle Quellen des Typs).

    Neu generierte Scripts werden erst nach der Freigabe im Admin ausgeführt.
    """
    for source in Source.objects.filter(type=source_type):
        if not source.link:
            logger.warning(f"Quelle {source.id} ({source_type}) hat keinen Link - übersprungen")
            continue

        if not source.import_script:
            logger.info(f"Generiere Import-Script für {source.link}...")
            filename = script_generator.source_script_name(source, KIND)
            if not script_generator.create_script(source.link, filename, KIND):
                record_source_error(source, "Script-Generierung fehlgeschlagen. Das Sprachmodell hat kein gültiges Script erstellt.")
                continue
            source.import_script = filename + ".py"
            source.import_script_approved_hash = ''
            source.result = ''
            source.import_errors = script_generator.PENDING_APPROVAL_NOTE
            source.save()
            logger.info(f"Script {source.import_script} erstellt (wartet auf Freigabe).")
            continue

        if not script_generator.is_approved(source):
            note_pending_approval(source)
            continue

        filename = script_generator.preview_name(source)
        # Vorschau nur erzeugen, wenn sie noch fehlt
        if script_generator.preview_path(KIND, filename).exists():
            continue
        try:
            write_preview(source, filename)
        except Exception as e:
            logger.error(f"Vorschau für {source.import_script} fehlgeschlagen: {e}")


def import_sources(source_type):
    """Phase 3: Events aktiver Quellen in die Datenbank übernehmen."""
    counter = 0
    for source in Source.objects.filter(type=source_type, active=True):
        logger.info(f"Importiere Events von {source.link}...")
        if not source.link or not source.import_script:
            logger.warning(f"Quelle {source.id} ohne Link oder Import-Script - übersprungen")
            continue
        if not script_generator.is_approved(source):
            note_pending_approval(source)
            continue

        try:
            events = run_source_script(source)
            counter += import_scraped_events(events, source)
            if not events and not source.import_errors:
                record_source_error(source, "Scraper liefert keine Events")
        except Exception as e:
            logger.error(f"Fehler beim Importieren von Events: {e}")
            record_source_error(source, f"Import fehlgeschlagen: {e}")
            continue

        source.import_date = localtime()
        source.save()
    return counter


def start_import():
    counter = 0
    for source_type in SCRIPT_SOURCE_TYPES:
        prepare_scripts(source_type)
        counter += import_sources(source_type)
    logger.info(f"{counter} HTML-Events importiert.")


class Command(BaseCommand):
    help = "Importiert Events von Quellen mit KI-generierten Import-Scripts"

    def handle(self, *args, **options):
        start_import()

if __name__ == "__main__":
    c = Command()
    c.run_from_argv(["", ""])