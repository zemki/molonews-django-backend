from django.conf import settings
import xml.etree.ElementTree as ET
from django.utils import timezone
from django.utils.timezone import localtime
from logging import getLogger
from content.models import Tag, Organization, EventV4, Event_Occurrence
from email.utils import parsedate_to_datetime
from content.import_tools.event_sync import sync_feed_events
from content.import_tools.source_health import record_source_error

IMAGE_MIN_SIZE = 150

logger = getLogger("molonews")


def parse_event_xml(xml_content, source):
    """
    Liest eine RSS-XML-Struktur mit Terminen ein,
    extrahiert pro <item>-Element die Felder und schreibt sie in die Datenbank.
    """
    events_imported = 0
    # Alle gültigen Feed-Einträge für den Abgleich nach dem Anlegen (event_sync)
    feed_events = []

    try:
        # XML parsen
        root = ET.fromstring(xml_content)
    except Exception as e:
        logger.error(f"Fehler beim Einlesen des XML-Inhalts: {source.name} {str(e)}")
        record_source_error(source, f"Feed ist kein gültiges XML: {e}")
        return 0


    for event_elem in root.findall('./channel/item'):
        try:
            title = event_elem.findtext('title', default="").strip()
            description = event_elem.findtext('description', default="").strip()

            content_encoded_elem = event_elem.find('{http://purl.org/rss/1.0/modules/content/}encoded')
            if content_encoded_elem is not None and content_encoded_elem.text:
                content_full = content_encoded_elem.text.strip()
            else:
                content_full = description  # Fallback, wenn kein content:encoded vorhanden

            pub_date_str = event_elem.findtext('pubDate', default="").strip()
            start_datetime = None
            end_datetime = None
            if pub_date_str:
                try:
                    # Parsing inkl. Zeitzone
                    dt = parsedate_to_datetime(pub_date_str)
                    start_datetime = dt
                    end_datetime = dt
                except ValueError as ve:
                    dt = timezone.now()
                    start_datetime = dt
                    end_datetime = dt
                    logger.warning(f"Datum konnte nicht geparst werden: {pub_date_str} - {ve}")

            if not start_datetime:
                logger.warning(f"Event '{title}' uebersprungen - kein Datum im Feed")
                continue

            # Ort, Adresse und Link stammen bei dieser Quelle aus der Organisation,
            # aus dem Feed kommt nur der Text - nur der wird abgeglichen.
            feed_events.append({"title": title, "start": start_datetime, "content": content_full})

            # Duplicate-Check: gleiche Quelle, gleicher Titel und gleiche Startzeit
            if EventV4.objects.filter(title=title, source=source, start_date=start_datetime).exists():
                continue

            organisation = Organization.objects.get(name=source.organization)

            event_obj = EventV4.objects.create(
                title=title,
                content=content_full,
                start_date=start_datetime,
                moddate=localtime(),
                source=source,
                event_location=organisation.name,  
                image_url="",
                image_source="",
                published=source.default_published,
                street=organisation.street,
                zip_code=organisation.zip,
                town=organisation.town,
                link=source.link.replace(".xml", ".html"),
                language=event_elem.findtext('language', default="").strip() or "de",
            )

            # Event_Occurrence erstellen. Fehlende Endzeit faellt auf den Start
            # zurueck - vorher wurde der Termin einfach weggelassen und es blieb
            # ein Event ohne Datum zurueck, ohne Fehler und ohne Logeintrag.
            Event_Occurrence.objects.create(
                event=event_obj,
                start_datetime=start_datetime,
                end_datetime=end_datetime or start_datetime,
            )

            # Standard-Tags zuweisen
            for tag in source.default_tags.all():
                event_obj.tags.add(tag)
                # check if no tags are set and set the default tags
                if not event_obj.tags.all() and settings.IMPORT_FALLBACK_EVENT_TAG_ID:
                    for tag in Tag.objects.filter(id=settings.IMPORT_FALLBACK_EVENT_TAG_ID):
                        event_obj.tags.add(tag)
                        
            # Areas zuweisen
            for area in source.area.all():
                event_obj.area.add(area)

            events_imported += 1
            event_obj.save()

        except Exception as e:
            logger.error(f"Fehler beim Importieren eines Events aus dem RSS-XML: {str(e)}")
            continue

    # Geänderte Events nachziehen, entfallene löschen
    sync_feed_events(source, feed_events)

    return events_imported