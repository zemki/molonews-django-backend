"""
Management Command: import_html_articles

Importiert Artikel von Sources mit type='ai-article'.
Die Import-Scripts erzeugt ein lokales Sprachmodell
(siehe content/import_tools/script_generator.py).

Phase 1: Script-Generierung (alle Sources ohne Script, auch inaktive)
Phase 2: Vorschau-Generierung (alle Sources mit freigegebenem Script)
Phase 3: DB-Import (nur aktive Sources mit freigegebenem Script)

Generierte Scripts werden erst ausgeführt, wenn ein Superuser sie im Admin
freigegeben hat (siehe script_generator.approve_script).
"""

import json
import traceback
from datetime import datetime
from logging import getLogger

from django.core.management.base import BaseCommand
from django.utils.timezone import localtime, make_aware, is_naive

from content.models import Article, Source, Organization
from content.import_tools.article_utils import html_article_exists
from content.import_tools.import_utils import clean_content, fix_misencoded_text, fix_misencoded_text_deep
from content.import_tools import script_generator
from content.import_tools.source_health import clear_source_error, record_source_error

logger = getLogger("molonews")

KIND = script_generator.KIND_ARTICLES


def import_scraped_articles(json_articles, source):
    """
    Importiert gescrapte Artikel in die Datenbank.

    Args:
        json_articles: Liste von Artikel-Dicts mit Feldern:
            - article_title (str, Pflicht)
            - article_content (str)
            - article_abstract (str)
            - article_date (str, ISO-Format)
            - article_link (str)
            - article_image (str)
        source: Source-Objekt

    Returns:
        int: Anzahl importierter Artikel
    """
    def truncate(s, length):
        if s is None:
            return ''
        s = str(s)
        return s[:length] if len(s) > length else s

    articles_imported = 0
    organisation = Organization.objects.get(name=source.organization)
    default_tags = list(source.default_tags.all())
    default_areas = list(source.area.all())

    for article_data in json_articles:
        try:
            title = article_data.get("article_title", "").strip()
            if not title:
                continue

            title = truncate(fix_misencoded_text(title), 340)
            content = article_data.get("article_content", "") or ""
            content = fix_misencoded_text(clean_content(content))
            abstract = article_data.get("article_abstract", "") or ""
            abstract = fix_misencoded_text(truncate(abstract, 1000))
            link = truncate(article_data.get("article_link", ""), 590)
            image_url = truncate(article_data.get("article_image", ""), 990)

            # Datum parsen
            date_str = article_data.get("article_date", "")
            article_date = localtime()
            if date_str:
                try:
                    if isinstance(date_str, str):
                        from dateutil.parser import parse as parse_date
                        article_date = parse_date(date_str)
                    elif isinstance(date_str, datetime):
                        article_date = date_str
                except (ValueError, TypeError):
                    article_date = localtime()

            if isinstance(article_date, datetime) and is_naive(article_date):
                article_date = make_aware(article_date)

            # Duplikat-Erkennung: gleicher Titel + gleiche Source
            if html_article_exists(title, source):
                logger.debug(f"Artikel existiert bereits: '{title[:80]}' (Source: {source.name})")
                continue

            # Duplikat-Erkennung: gleicher Link (cross-source)
            if link and Article.objects.filter(link=link).exists():
                logger.debug(f"Artikel mit gleichem Link existiert: {link[:80]}")
                continue

            # Artikel erstellen
            try:
                article = Article.objects.create(
                    title=title,
                    abstract=abstract,
                    content=content,
                    date=article_date,
                    moddate=localtime(),
                    link=link,
                    source=source,
                    image_url=image_url,
                    image_source=truncate(organisation.name, 200),
                    published=source.default_published,
                    up_for_review=True,
                )
            except Exception as e:
                logger.error(f"Fehler beim Erstellen des Artikels '{title[:80]}': {e}")
                continue

            # Default-Tags zuweisen
            for tag in default_tags:
                article.tags.add(tag)

            # Default-Areas zuweisen
            for area in default_areas:
                article.area.add(area)

            articles_imported += 1
            logger.info(f"Artikel importiert: '{title[:80]}'")

        except Exception as e:
            logger.error(f"Fehler beim Import von Artikel '{article_data.get('article_title', '?')[:50]}': {e}")
            continue

    return articles_imported


def load_and_run_script(source):
    """Führt das freigegebene Scraping-Script einer Source aus."""
    articles = script_generator.run_source_script(source, KIND)
    return fix_misencoded_text_deep(articles)


def note_pending_approval(source):
    """Vermerkt an der Source, dass das Script auf Freigabe wartet (ohne Zeitstempel)."""
    note = script_generator.approval_note(source)
    if source.import_errors != note:
        source.import_errors = note
        source.save(update_fields=["import_errors"])
    logger.info(f"Source {source.name}: {note}")


def set_source_error(source, error_msg):
    """Setzt import_errors auf der Source und speichert.

    Mit Zeitpunkt des ersten Fehlschlags (record_source_error), damit das
    Monitoring die Dauer der Störung kennt.
    """
    if error_msg:
        record_source_error(source, error_msg)
    else:
        clear_source_error(source)
    logger.error(f"Source {source.name}: {error_msg}")


def start_import():
    counter = 0

    # === PHASE 1+2: Script-Generierung und Vorschau (alle ai-article Sources) ===
    for source in Source.objects.filter(type="ai-article"):
        logger.info(f"Verarbeite AI-Article Source: {source.name} (ID: {source.id})")

        if not source.link:
            set_source_error(source, "Kein Link konfiguriert.")
            continue

        # Phase 1: Script-Generierung (nur wenn noch kein Script vorhanden)
        if not source.import_script:
            logger.info(f"Generiere Script für {source.name}...")
            filename = script_generator.source_script_name(source, KIND)
            if script_generator.create_script(source.link, filename, KIND):
                source.import_script = filename + ".py"
                source.import_script_approved_hash = ''
                source.result = ''
                source.import_errors = script_generator.PENDING_APPROVAL_NOTE
                source.save()
                logger.info(f"Script gespeichert: {source.import_script} (wartet auf Freigabe)")
            else:
                set_source_error(source, "Script-Generierung fehlgeschlagen. Das Sprachmodell hat kein gültiges Script erstellt.")
            # Ein neues Script läuft erst nach der Freigabe im Admin
            continue

        if not script_generator.is_approved(source):
            note_pending_approval(source)
            continue

        filename = script_generator.preview_name(source)
        resultfile = script_generator.preview_path(KIND, filename)
        resultfilelink = script_generator.preview_url(KIND, filename)

        # Phase 2: Vorschau generieren (immer aktualisieren)
        try:
            logger.info(f"Generiere Vorschau für {source.name}...")
            articles = load_and_run_script(source)

            with open(resultfile, "w", encoding="utf-8") as f:
                json.dump(articles, f, indent=2, default=str, ensure_ascii=False)

            source.result = resultfilelink
            if not articles:
                record_source_error(source, 'Vorschau: 0 Artikel gefunden. Script prüfen oder neu generieren.')
            elif not source.active:
                # Für aktive Quellen entscheidet erst der DB-Import (Phase 3), ob der
                # Lauf gelungen ist - sonst bekäme ein Fehler dort bei jedem Lauf
                # einen neuen Beginn.
                source.import_errors = ''
            source.save()
            logger.info(f"Vorschau gespeichert: {resultfilelink} ({len(articles)} Artikel)")
        except Exception as e:
            error_msg = f"Fehler bei Vorschau: {str(e)}"
            set_source_error(source, error_msg)
            logger.error(traceback.format_exc())

    # === PHASE 3: DB-Import (nur aktive Sources) ===
    for source in Source.objects.filter(type="ai-article", active=True):
        if not source.import_script:
            set_source_error(source, "Kein Import-Script vorhanden. Script muss erst generiert werden.")
            continue
        if not script_generator.is_approved(source):
            note_pending_approval(source)
            continue

        logger.info(f"DB-Import für {source.name} (ID: {source.id})...")

        try:
            articles = load_and_run_script(source)
            articles_imported = import_scraped_articles(articles, source)
            counter += articles_imported
            # Ein leeres Ergebnis ist kein Erfolg: der Vermerk "0 Artikel gefunden"
            # aus der Vorschau bleibt dann stehen.
            if articles:
                source.import_errors = ''
            logger.info(f"{articles_imported} Artikel importiert von {source.name}")
        except Exception as e:
            error_msg = f"Fehler beim DB-Import: {str(e)}"
            set_source_error(source, error_msg)
            logger.error(traceback.format_exc())
            continue

        source.import_date = localtime()
        source.save()

    logger.info(f"AI-Article Import abgeschlossen: {counter} Artikel importiert.")


class Command(BaseCommand):
    help = "Importiert Artikel von Sources mit KI-generierten Scraping-Scripts"

    def handle(self, *args, **options):
        start_import()


if __name__ == "__main__":
    c = Command()
    c.run_from_argv(["", ""])
