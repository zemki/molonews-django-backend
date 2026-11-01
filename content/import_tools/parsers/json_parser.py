from datetime import timedelta  # Modified to include timedelta
from django.utils.timezone import is_naive, make_aware, localtime
from logging import getLogger
from dateutil.parser import parse as parse_datetime

from content.models import EventV4, Event_Occurrence
import re
from content.import_tools.import_utils import extract_address_parts_json, clean_content, effective_location
from content.import_tools.event_sync import sync_feed_events



IMAGE_MIN_SIZE = 150
logger = getLogger("molonews")

def safe_parse_datetime(dt_str):
    """
    Versucht, eine Datums-Zeit-String robust zu parsen.
    Gibt None zurück, wenn es nicht klappt.
    """
    if not dt_str:
        return None
    try:
        # dateutil ist sehr tolerant bei Formaten
        dt = parse_datetime(dt_str)
        if is_naive(dt):
            dt = make_aware(dt)
        else:
            dt = localtime(dt)
        return dt
    except Exception as e:
        logger.error(f"Datum konnte nicht geparst werden: '{dt_str}'. Fehler: {e}")
        return None

def parse_event_json(json_data, source):
    events_imported = 0
    # Alle gültigen Feed-Einträge für den Abgleich nach dem Anlegen (event_sync)
    feed_events = []

    for event in json_data:
        try:
            # Felder mit mehreren möglichen Namen abfragen:
            title = event.get("event_title") or event.get("title") or "Unbenanntes Event"
            link = event.get("event_link") or event.get("link")
            content = event.get("event_content") or event.get("content") or ""
            image = event.get("event_image") or event.get("image_url") or ""
            start_date = (
                event.get("event_start_date")
                or event.get("start_date")
                or event.get("start")
                or event.get("begin")
            )
            end_date = (
                event.get("event_end_date")
                or event.get("end_date")
                or event.get("end")
                or event.get("finish")
            )
            location = (
                event.get("location")
                or event.get("location_title")
                or event.get("location_name")
                or ""
            )
            # Adresse aus mehreren Feldern zusammensetzen, wenn nötig:
            address = (
                event.get("location_address")
                or (
                    (event.get("street") or "")
                    + ", "
                    + (event.get("postal_code") or event.get("zip_code") or "")
                    + " "
                    + (event.get("city") or event.get("town") or "")
                )
            ).strip(", ")

            ticket_link = event.get("event_ticket_link") or ""
            # Fallback für location_title
            location_title = event.get("location_title") or event.get("location_name") or ""

            # Sonderzeichen im Text ersetzen
            def clean_special_chars(txt):
                if not txt:
                    return ""
                return (
                    txt.replace("\u2013", "-")
                    .replace("&#8211;", "-")
                    .replace("&#038;", "&")
                    .replace("&#060;", "<")
                    .replace("&#062;", ">")
                    .replace("&#034;", '"')
                )

            title = clean_special_chars(title)
            content = clean_special_chars(content)

            # SVG-Bilder ignorieren
            if image and re.search(r'\.svg$', image, re.IGNORECASE):
                image = ""

            # Datumswerte parsen, robust gegen unterschiedliche Formate
            start_datetime = safe_parse_datetime(start_date)
            end_datetime = safe_parse_datetime(end_date)
            if not start_datetime:
                logger.error(f"Kein gültiges Startdatum: {start_date} (Event: {title})")
                continue
            if not end_datetime:
                logger.warning(f"Kein gültiges Enddatum: {end_date} (Event: {title}) - setze 2h nach Start")
                end_datetime = start_datetime + timedelta(hours=2)

            # Adresse aufteilen
            try:
                street, zip_code, town = extract_address_parts_json(address)
            except Exception as addr_e:
                logger.warning(f"Adresse konnte nicht extrahiert werden: '{address}'. Fehler: {addr_e}")
                street, zip_code, town = "", "", ""

            content = clean_content(content)

            # Ticketlink am Ende anhängen
            if ticket_link:
                content += f'<p>Tickets: {ticket_link}</p>'

            # " am Ende entfernen
            if content and '"' in content[-10:]:
                content = content[:-10] + content[-10:].replace('"', '', 1)

            # Fallback für Location: erst der Ortsname, dann wie fill_event_location
            # Straße und Stadt - sonst trüge der Cron den Ort nach und der Abgleich
            # setzte ihn bei jedem Lauf wieder zurück (siehe effective_location).
            if not location:
                location = location_title
            location = effective_location(location, street, town)

            feed_events.append({
                "title": title,
                "start": start_datetime,
                "end": end_datetime,
                "content": content,
                "event_location": location,
                "image_url": image,
                "link": link,
                "street": street,
                "zip_code": zip_code,
                "town": town,
            })

            # Duplikate überspringen
            if EventV4.objects.filter(title=title, source=source, start_date=start_datetime).exists():
                continue

            # Event anlegen
            event_obj = EventV4.objects.create(
                title=title,
                content=content,
                start_date=start_datetime,
                moddate=localtime(),
                source=source,
                event_location=location,
                image_url=image,
                #image_source=organisation.name if organisation else "",
                published=source.default_published,
                street=street,
                zip_code=zip_code,
                town=town,
                link=link,
                language=event.get("language") or "de",
            )

            # Event Occurrence anlegen
            Event_Occurrence.objects.create(
                event=event_obj,
                start_datetime=start_datetime,
                end_datetime=end_datetime
            )

            # Tags und Areas zuweisen
            for tag in source.default_tags.all():
                event_obj.tags.add(tag)
            for area in source.area.all():
                event_obj.area.add(area)

            events_imported += 1

        except Exception as e:
            logger.error(
                f"Fehler beim Import von Event '{event.get('event_title', 'Unknown')}': {str(e)}"
            )
            import traceback
            logger.error(traceback.format_exc())
            continue

    logger.info(f"Erfolgreich {events_imported} Events importiert.")

    # Geänderte Events nachziehen, entfallene löschen
    sync_feed_events(source, feed_events)

    return events_imported
