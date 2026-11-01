from django.conf import settings
from logging import getLogger
import traceback
from datetime import datetime, time, date
from django.utils import timezone
from django.core.management.base import BaseCommand
from django.utils.timezone import is_naive, make_aware, localtime, timedelta
from lxml import etree
from content.import_tools.url_safety import safe_get
import feedparser
from content.models import Source, Tag, EventV4, Event_Occurrence
from content.parsers import get_parser_function
from content.import_tools.parsers import (
    parse_event_json,
    parse_event_xml,
    parse_ics_for_events,
)
from content.import_tools.import_utils import fix_misencoded_text
from content.import_tools.import_utils import get_image_url
from content.import_tools.article_utils import article_exists_for_update, has_title, write_article, update_article
from content.import_tools.event_sync import sync_feed_events
from content.import_tools.source_health import clear_source_error, describe_fetch_error, record_source_error
import chardet


logger = getLogger("import_articles")

# Ein bestehender Artikel ohne eigenes Bild wird nur so lange erneut auf ein Bild
# geprüft (siehe _needs_image_recheck).
IMAGE_RECHECK_MAX_AGE = timedelta(hours=48)

# RSS-Quellen, die nur einmal am Tag abgerufen werden, beim ersten Lauf ab
# DAILY_SOURCE_HOUR (Setting IMPORT_DAILY_SOURCE_IDS). Gedacht für Quellen, die
# selten veröffentlichen oder häufige Abrufe als Bot-Verkehr sperren.
DAILY_SOURCE_IDS = set(getattr(settings, "IMPORT_DAILY_SOURCE_IDS", []))
DAILY_SOURCE_HOUR = 6


def _needs_image_recheck(article, source):
    """Soll für einen bestehenden Artikel noch einmal nach einem Bild gesucht werden?

    Das Bild zu ermitteln heißt: Artikelseite und Bilddatei abrufen. Für jeden
    Feed-Eintrag ohne Änderungsdatum bei jedem Lauf wäre das bei Quellen, die
    ihr ganzes Archiv liefern, viel zu teuer.

    Geprüft wird nur noch, solange ein frischer Artikel kein eigenes Bild hat:
    Manche Redaktionen hängen das Bild erst kurz nach der Veröffentlichung an.
    """
    if article.image_url and article.image_url != source.default_image_url:
        return False
    created_at = getattr(article, "created_at", None)
    return bool(created_at) and created_at >= timezone.now() - IMAGE_RECHECK_MAX_AGE


def _daily_fetch_due(source):
    """Ist der tägliche Abruf einer Quelle aus DAILY_SOURCE_IDS fällig?

    import_date wird bei jedem Abrufversuch gesetzt, auch bei einem
    fehlgeschlagenen. Eine gesperrte Quelle wird so ebenfalls nur einmal am Tag
    angefragt.
    """
    now = localtime()
    if now.hour < DAILY_SOURCE_HOUR:
        return False
    return source.import_date is None or localtime(source.import_date).date() < now.date()


def import_articles(source_id=None, force_update=False):
    xml_parser = etree.XMLParser(recover=True)
    counter = 0                                      # stores the amount of imported articles
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    def active_sources(source_type):
        """Aktive Quellen eines Typs, optional auf eine einzelne Quelle begrenzt."""
        sources = Source.objects.filter(type=source_type, active=True)
        if source_id:
            sources = sources.filter(id=source_id)
        return sources

    for source in active_sources("json-event"):
        (f"Importiere Events von {source.name}...")
        source.import_date = localtime()
        # Nur dieses Feld schreiben: die Quellen-Liste wurde zu Beginn der Schleife
        # geladen, ein volles save() wuerde zwischenzeitliche Aenderungen an der
        # Quelle (z.B. aus dem Admin) mit dem alten Stand ueberschreiben.
        source.save(update_fields=["import_date"])
        try:
            response = safe_get(source.link, timeout=20, headers=headers)
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Quelle {source.link}: {str(e)}")
            record_source_error(source, f"Feed nicht abrufbar: {describe_fetch_error(e)}")
            continue

        try:
            json_data = response.json()
            events_imported = parse_event_json(json_data, source)
            counter += events_imported
        except Exception as e:
            logger.error(f"JSON-Import Fehler: {str(e)}")
            logger.error(traceback.format_exc())
            record_source_error(source, f"Feed ist kein gültiges JSON (HTTP {response.status_code}): {e}")
            continue

    for source in active_sources("xml-event"):
        logger.info(f"Importiere Events von {source.name}...")
        source.import_date = localtime()
        source.save(update_fields=["import_date"])
        try:
            response = safe_get(source.link, timeout=20, headers=headers)
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Quelle {source.link}: {str(e)}")
            record_source_error(source, f"Feed nicht abrufbar: {describe_fetch_error(e)}")
            continue
    
        try:
            events_imported = parse_event_xml(response.content, source)
            counter += events_imported
        except Exception as e:
            logger.error(f"Fehler beim XML-Import: {e}")
            record_source_error(source, f"XML-Import fehlgeschlagen: {e}")
            continue

    for source in active_sources("ics"):
        logger.info(f"Importiere Events von {source.name}...")
        source.import_date = localtime()
        source.save(update_fields=["import_date"])

        try:
                # get response
                response = safe_get(source.link, timeout=20)

                # Handle .ics file parsing and event creation
                events = parse_ics_for_events(response.content, base_url=source.link)
                # Alle gültigen Termine für den Abgleich nach dem Anlegen (event_sync)
                feed_events = []
                
                for event_data in events:
                    # Ein fehlerhafter Termin darf nicht die ganze Quelle abbrechen:
                    # vor dieser Absicherung beendete z.B. ein zu langer Ort
                    # (event_location ist varchar(100)) den Import aller weiteren
                    # Termine dieser Quelle.
                    try:
                        title = event_data["title"]
                        start_datetime = event_data["start_datetime"]
                        end_datetime = event_data["end_datetime"]

                         # Handle `datetime.date` objects
                        if isinstance(start_datetime, date) and not isinstance(start_datetime, datetime):
                            # Convert to `datetime` at midnight
                            start_datetime = datetime.combine(start_datetime, time.min)

                        if isinstance(end_datetime, date) and not isinstance(end_datetime, datetime):
                            end_datetime = datetime.combine(end_datetime, time.min)

                        # Check if the datetime is naive
                        if is_naive(start_datetime):
                            # Make it timezone-aware using the system timezone
                            start_datetime = make_aware(start_datetime)
                        else:
                            # Normalize the aware datetime to Django's current timezone
                            start_datetime = localtime(start_datetime)

                        if is_naive(end_datetime):
                            # Make it timezone-aware using the system timezone
                            end_datetime = make_aware(end_datetime)
                        else:
                            # Normalize the aware datetime to Django's current timezone
                            end_datetime = localtime(end_datetime)

                        #end_datetime = timezone.make_aware(event_data["end_datetime"])
                        # Laengen an die Modellfelder anpassen: event_location ist
                        # varchar(100), title varchar(300). Ueberlange Werte liessen
                        # den Import der Quelle mit einem DB-Fehler abbrechen.
                        location = (event_data["location"] or "")[:100]
                        description = event_data["description"]
                        title = (title or "")[:300]

                        feed_events.append({
                            "title": title,
                            "start": start_datetime,
                            "end": end_datetime,
                            "content": description,
                            "event_location": location,
                        })

                        # check if starte_datetime in past then skip
                        if start_datetime < timezone.now():
                            continue

                        # Check for duplicate event
                        if EventV4.objects.filter(title=title, source=source, start_date=start_datetime).exists():
                            continue  # Skip if duplicate exists
                    
                        event = EventV4.objects.create(
                            title=title,
                            content=description,
                            start_date=start_datetime,
                            moddate=timezone.now(),
                            source=source,
                            event_location=location,
                            published=source.default_published
                        )

                        # Create event occurrences
                        Event_Occurrence.objects.create(
                            event=event,
                            start_datetime=start_datetime,
                            end_datetime=end_datetime
                        )

                        # Add areas and tags
                        for area in source.area.all():
                            event.area.add(area)
                        for tag in source.default_tags.all():
                            event.tags.add(tag)
                        # check if no tags are set and set the default tags
                        if not event.tags.all() and settings.IMPORT_FALLBACK_EVENT_TAG_ID:
                            for tag in Tag.objects.filter(id=settings.IMPORT_FALLBACK_EVENT_TAG_ID):
                                event.tags.add(tag)
                   
                        counter += 1
                    except Exception as event_error:
                        logger.error(
                            f"Termin uebersprungen in .ics-Quelle {source.name}: "
                            f"'{event_data.get('title', '?')}' - {event_error}"
                        )
                        continue

                # Geänderte Termine nachziehen, entfallene löschen
                sync_feed_events(source, feed_events)
                if not events:
                    record_source_error(source, "Kalenderdatei enthält keine lesbaren Termine")
        except Exception as e:
            logger.error(f"Error processing .ics source {source.name}: {str(e)}")
            record_source_error(source, f"Kalender nicht abrufbar oder nicht lesbar: {describe_fetch_error(e)}")
            continue


    #go through List of Sources from DB and fetch articles
    for source in active_sources("rss"):
        # Ein gezielter Lauf mit --source-id holt auch diese Quellen sofort
        if source.id in DAILY_SOURCE_IDS and not source_id and not _daily_fetch_due(source):
            continue
        logger.info(f"Importiere Events von {source.name}...")
        source.import_date = localtime()
        source.save(update_fields=["import_date"])

        if source.link == None:
            continue
        if source.link == "":
            continue
       
        text = ""
        sanitized_text  = ""
        feed  = ""
        parser  = "" 
        s = "{} - {}".format(source.name, source.link)

        # Get source data
        try:

            response = safe_get(source.link, timeout=20, headers=headers)
            # Fehlerseiten sind kein Feed. Vorher lief ihr HTML still durch den
            # Parser und ergab null Einträge - ohne jede Spur im Log.
            if response.status_code >= 400:
                logger.info(f"Error while getting HTML code of source {s}: HTTP {response.status_code}")
                record_source_error(source, f"Feed nicht abrufbar: HTTP {response.status_code}")
                continue
            raw_bytes = response.content

            # Encoding erkennen lassen:
            result = chardet.detect(raw_bytes)
            encoding = result['encoding'] if result['encoding'] else 'utf-8'


            # Bytes in Unicode-Text konvertieren:
            text = raw_bytes.decode(encoding, errors='replace')

        except Exception as e:
            logger.info(f"Error while getting HTML code of source {s}: {str(e)}")
            record_source_error(source, f"Feed nicht abrufbar: {describe_fetch_error(e)}")
            continue

       # Sanitize source data
        try:
            sanitized_text = etree.tostring(
                etree.fromstring(text.encode('utf-8'), parser=xml_parser)
            )

        except Exception as e:
            # Die Antwort ist kein XML (HTML-Seite, Fehlertext, leer)
            logger.info(f"Error while sanitizing feed of source {s}: {str(e)}")
            record_source_error(
                source,
                f"Antwort ist kein XML-Feed ({response.headers.get('Content-Type', 'ohne Content-Type')}, "
                f"{len(raw_bytes)} Bytes)",
            )
            continue

        # Parse source data
        try:
            feed = feedparser.parse(sanitized_text)

        except Exception as e:
            logger.error(f"Error while parsing sanitized_text for source {s}: {str(e)}")
            logger.error(traceback.format_exc())
            continue

        # Erfolg oder Störung an der Quelle vermerken (Admin-Spalte "Fehler", Monitoring)
        if feed.entries:
            clear_source_error(source)
        elif getattr(feed, "version", ""):
            record_source_error(source, "Feed enthält keine Einträge")
        else:
            record_source_error(
                source,
                f"Antwort ist kein Feed ({response.headers.get('Content-Type', 'ohne Content-Type')})",
            )

        # Get parser function
        try:
            parser = get_parser_function(source.parser)

        except Exception as e:
            logger.error(f"Error while getting parser function for source {s}: {str(e)}")
            logger.error(traceback.format_exc())
            continue
                  
        # iterate through all entries
        feed_links = set()
        for entry in feed.entries:

            if not has_title(entry):
                continue

            try:
                #add_date(entry)
                if parser:
                    entry_parsed = parser(entry)
                else:
                    entry_parsed = entry

                # Check if entry_parsed is None
                if entry_parsed is None:
                    logger.error(f"Error: Parsed entry is None for feed entry {entry}")
                    continue

                # Fuer den Abgleich am Ende: den geparsten Link merken, denn nur der
                # entspricht dem, was in Article.link landet
                _entry_link = getattr(entry_parsed, "link", None)
                if _entry_link:
                    feed_links.add(_entry_link)

            except Exception:
                # ToDo: log error
                # logger.error(f"Error while parsing entry: {str(e)}")
                # logger.error(traceback.format_exc())
                continue
            
            try:
                depublicated = getattr(entry_parsed, "depublicated", None)
                moddate = getattr(entry_parsed, "moddate", None)
                summary = getattr(entry_parsed, "summary", None)
                description = getattr(entry_parsed, "description", None)
                content_element = getattr(entry_parsed, "content", None)
                # if content contains <![CDATA[  then handle correctly

                if description:
                    if not summary:
                        summary = description

                if content_element:
                    if isinstance(content_element, list):
                        content_element = content_element[0].value
                    if content_element.startswith("<![CDATA["):
                        content_element = content_element.replace("<![CDATA[", "")
                        content_element = content_element.replace("]]>", "")

                if summary:

                    summary = summary.strip()
                    summary = fix_misencoded_text(summary)
                    summary = summary.replace(" erschien zuerst auf Lüne-Blog", "")
                    

                    if summary.find('mehr...') > 8:
                        summary = summary.replace("mehr...", "")

                    if len(summary) == 0:
                        summary = "mehr..."

                    von_pos = summary.find("Von")
                    dpa_pos = summary.find("dpa")

                    if von_pos > -1 and dpa_pos > -1:
                        dpa_pos = dpa_pos + len("dpa")
                        summary = summary[dpa_pos:]

                    setattr(entry_parsed, "summary", summary)
                else:
                    # Handle missing summary case, assign a default value
                    # logger.warning(f"Article {entry_parsed.title} has no summary, assigning default value.")
                    if content_element:
                        summary = content_element[:1500]
                    else:
                        summary = "No summary available"
                    setattr(entry_parsed, "summary", summary)


                # Prüfe ob Artikel für Update existiert (via foreign_id oder link)
                # WICHTIG: Duplikat-Erkennung erfolgt in write_article() via ArticleDuplicateHandler!
                existing_article = article_exists_for_update(entry_parsed, source)


            except Exception as e:
                logger.error(f"Error while processing entry: {str(e)}")
                logger.error(traceback.format_exc())
                continue

            title = getattr(entry_parsed, "title")
            # Fix encoding issues in title (same as for summary)
            title = fix_misencoded_text(title)
            setattr(entry_parsed, "title", title)

            try:
                # UPDATE-LOGIK: Artikel existiert bereits (via foreign_id/link)
                if existing_article:
                    depublicated = getattr(entry_parsed, "depublicated", False)
                    moddate = getattr(entry_parsed, "moddate", None)

                    # Falls der Artikel als depubliziert markiert ist, lösche ihn
                    if depublicated:
                        logger.info(f"Deleting depublished article '{existing_article.title}'")
                        existing_article.delete()
                        continue

                    # Unverändertes Änderungsdatum heißt: die Quelle hat den Eintrag nicht
                    # angefasst. Dann hier abbrechen, bevor get_image_url() Artikelseite
                    # und Bild herunterlädt - das ist der Löwenanteil der Importzeit.
                    # Ausnahme --force-update: noetig, wenn eine Quelle neu einen Parser
                    # mit moddate bekommt. Deren Bestand ist dann inhaltlich veraltet,
                    # traegt aber schon das passende moddate und wuerde nie nachgezogen.
                    if moddate and not force_update and existing_article.moddate == moddate:
                        continue

                    # Ohne Änderungsdatum im Feed gäbe es hier nur das Bild neu zu
                    # ermitteln - das lohnt nur bei frischen Artikeln ohne Bild.
                    if not moddate and not force_update and not _needs_image_recheck(existing_article, source):
                        continue

                    _image_url = get_image_url(entry_parsed, source.default_image_url)

                    # Falls sich das Bild geändert hat, aktualisiere den Artikel
                    if existing_article.image_url != _image_url:
                        existing_article.image_url = _image_url
                        existing_article.save()

                    # Falls es eine Modifikation am Artikel gibt, aktualisiere ihn
                    if moddate:
                        article_date = getattr(entry_parsed, "statedate", None) or moddate
                        update_article(existing_article, entry_parsed, source, article_date,
                                       moddate=moddate, image_url=_image_url)

                    continue  # Da der Artikel existiert, keine Neuerstellung nötig

                # NEUE ARTIKEL: Duplikat-Prüfung erfolgt in write_article()
                else:
                    created_article = write_article(entry_parsed, source)
                    # write_article() gibt None zurück wenn Duplikat übersprungen wurde
                    if created_article:
                        counter += 1

            except Exception as e:
                logger.error(f"Error processing article '{getattr(entry_parsed, 'title', 'Unknown')}': {str(e)}")
                logger.error(traceback.format_exc())


    logger.info (str(counter) + " Articles imported.")


class Command(BaseCommand):
    help = "Imports new articles"

    def add_arguments(self, parser):
        parser.add_argument(
            "--source-id",
            type=int,
            default=None,
            help="Nur diese eine Quelle importieren (Standard: alle aktiven Quellen)",
        )
        parser.add_argument(
            "--force-update",
            action="store_true",
            help="Bestehende Artikel auch dann abgleichen, wenn das Änderungsdatum "
                 "unverändert ist (einmaliger Nachzug, z.B. nach einem Parser-Wechsel)",
        )

    def handle(self, *args, **options):
        source_id = options.get("source_id")
        force_update = options.get("force_update", False)
        if source_id:
            logger.info(f"Starting article import for source {source_id}.")
        else:
            logger.info("Starting article import.")
        import_articles(source_id=source_id, force_update=force_update)

if __name__ == "__main__":
    c = Command()
    c.run_from_argv(["", ""])

