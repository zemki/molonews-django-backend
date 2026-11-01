from django.conf import settings
from datetime import datetime
from django.utils import timezone
from django.utils.timezone import localtime
import traceback
from dateutil.parser import parse as parse_datetime
from content.models import Article, Tag, Area
# ENTFERNT: import ml.news_article_tagging  as ml
from content.import_tools.article_duplicate_handler import ArticleDuplicateHandler
from dateutil.parser import parse
from content.import_tools.import_utils import correct_encoding, fix_misencoded_text
from content.import_tools.import_utils import get_image_url
import logging
logger = logging.getLogger("import_articles")

# Platzhalter fuer "keine Bild-URL uebergeben". None geht dafuer nicht, weil None
# selbst ein gueltiges Ergebnis von get_image_url() ist (= Artikel ohne Bild).
_IMAGE_URL_UNSET = object()


def html_article_exists(title, source):
    """
    Check if an article with the same title, date, and source already exists.

    Args:
        title (str): Title of the article.
        source (Source): The source of the article.

    Returns:
        bool: True if a duplicate exists, False otherwise.
    """
    # Check for an article with the same title, date, and source
    return Article.objects.filter(title=title, source=source).exists()


def add_date(entry):
    if "date" in entry:
        entry.date = parse_datetime(entry.date)
    else:
        entry.date = localtime()


def _get_article(entry):
    article = None
    if hasattr(entry, "foreign_id") and entry.foreign_id:
        try:
            article = Article.objects.get(foreign_id=entry.foreign_id)
        except Article.DoesNotExist:
            pass
    if not article:
        try:
            article = Article.objects.get(link=entry.link)
            # log how many articles are found by link
        except Article.DoesNotExist:
            pass
        except Article.MultipleObjectsReturned:
            #logger.error("getting article by link multiple entries exist " + entry.link + "entry title: " + entry.title + "entry summary: " + entry.summary) 
            article = None
    if not article:
        try:
            article = Article.objects.get(title=entry.title, abstract=entry.summary)
        except Article.DoesNotExist:
            pass
        except Article.MultipleObjectsReturned:
            #logger.error("getting article by title multiple entries exist " + entry.title + " " + entry.summary)
            article = None
        
    return article


def article_exists_for_update(entry, source):
    """
    Prüft ob ein Artikel für ein Update existiert (via foreign_id oder link).

    WICHTIG: Diese Funktion ist NUR für Update-Checks gedacht!
    Duplikat-Erkennung erfolgt in write_article() via ArticleDuplicateHandler.

    Returns:
        Article object wenn gefunden, sonst None
    """
    try:
        # Prüfe via foreign_id (primäre Methode für Updates)
        if hasattr(entry, "foreign_id") and entry.foreign_id:
            try:
                return Article.objects.get(foreign_id=entry.foreign_id, source=source)
            except Article.DoesNotExist:
                pass
            except Article.MultipleObjectsReturned:
                logger.warning(f"Multiple articles with foreign_id {entry.foreign_id} found")
                return Article.objects.filter(foreign_id=entry.foreign_id, source=source).first()

        # Fallback: Prüfe via link (gleiche Source)
        if hasattr(entry, "link") and entry.link:
            try:
                return Article.objects.get(link=entry.link, source=source)
            except Article.DoesNotExist:
                pass
            except Article.MultipleObjectsReturned:
                logger.warning(f"Multiple articles with link {entry.link} for source {source.name} found, returning first")
                return Article.objects.filter(link=entry.link, source=source).first()

            # Fallback: Prüfe via link (alle Sources)
            try:
                return Article.objects.get(link=entry.link)
            except Article.DoesNotExist:
                pass
            except Article.MultipleObjectsReturned:
                logger.warning(f"Multiple articles with link {entry.link} found (cross-source), returning first")
                return Article.objects.filter(link=entry.link).first()

        return None

    except Exception as e:
        logger.error(f"Fehler in article_exists_for_update: {str(e)}")
        return None

def depublicate_article(entry):
    article = _get_article(entry)
    article.delete()


def has_title(entry):
    try:
        if entry.title:
            return True
    except AttributeError:
        return False
def write_article(entry_parsed, source):
    """Create new article with duplicate detection

    Args:
        entry_parsed (feedparser.entry): parsed feedparser entry
        source (source): article source

    Returns:
        Article: The created article object, or None if skipped due to duplicate
    """
    _image = None
    _image_detail = None

    # Das Bild wird erst ermittelt, wenn es darauf ankommt: get_image_url() ruft
    # Artikelseite und Bilddatei ab. Rund 900 Feed-Einträge je Lauf sind bekannte
    # Dubletten und werden ohnehin verworfen - für sie lief der Abruf umsonst.
    _image_url = _IMAGE_URL_UNSET

    def resolve_image_url():
        nonlocal _image_url
        if _image_url is _IMAGE_URL_UNSET:
            _image_url = get_image_url(entry_parsed)
        return _image_url

    entry_class = Article

    entry_class.area
    entry_parsed_summary = entry_parsed.summary


    if getattr(entry_parsed, 'published', None):
        date_value = getattr(entry_parsed, 'published')
    elif getattr(entry_parsed, 'pubDate', None):
        date_value = getattr(entry_parsed, 'pubDate')
    elif getattr(entry_parsed, 'date', None):
        date_value = getattr(entry_parsed, 'date')
    elif getattr(entry_parsed, 'updated', None):
        date_value = getattr(entry_parsed, 'updated')
    else:
        date_value = timezone.now()

    # Only parse date_value if it's a string
    if isinstance(date_value, str):
        try:
            date_value = parse(date_value, dayfirst=False)
        except ValueError:
            date_value = parse(date_value, dayfirst=True)

    # if date is later than now, set it to now
    if date_value > timezone.now():
        date_value = timezone.now()

    # Make date_value timezone-aware if it's naive
    if isinstance(date_value, datetime) and timezone.is_naive(date_value):
        date_value = timezone.make_aware(date_value)

    # ========== DUPLICATE DETECTION ==========
    # Check for duplicates before creating article
    duplicate_found = False
    try:
        title = correct_encoding(entry_parsed.title, source)
        title = fix_misencoded_text(title)
        abstract = correct_encoding(entry_parsed_summary, source)

        # Prepare new article data for comparison (Bild-URL folgt nur bei Bedarf)
        new_article_data = {
            'title': title,
            'abstract': abstract,
            'content': getattr(entry_parsed, 'content', ''),
            'image_url': None,
            'image': getattr(entry_parsed, "image_square", None) or _image,
            'image_source': getattr(entry_parsed, "image_source", None),
        }

        # Find duplicate (same-source or cross-source)
        duplicate, match_type = ArticleDuplicateHandler.find_duplicate(
            title=title,
            pub_date=date_value,
            source=source,
            abstract=abstract,
            use_fuzzy=True
        )

        if duplicate:
            duplicate_found = True

            # Berechne Scores für Logging
            existing_priority = duplicate.source.priority
            new_priority = source.priority
            existing_score = ArticleDuplicateHandler.calculate_quality_score(duplicate)

            # Das Bild entscheidet nur mit, wenn die neue Quelle gewinnt (dann wird
            # der Artikel angelegt) oder wenn bei gleicher Priorität der neue
            # Artikel mit Bild besser wäre als der bestehende. In allen anderen
            # Fällen steht "überspringen" schon fest.
            if new_priority > existing_priority:
                image_matters = True
            elif new_priority == existing_priority:
                best_case_score = ArticleDuplicateHandler.calculate_quality_score(
                    {**new_article_data, 'image_url': 'x'}
                )
                image_matters = best_case_score > existing_score
            else:
                image_matters = False
            if image_matters:
                new_article_data['image_url'] = resolve_image_url()

            new_score = ArticleDuplicateHandler.calculate_quality_score(new_article_data)

            # Duplikat gefunden - entscheide ob ersetzen oder überspringen
            action = ArticleDuplicateHandler.handle_duplicate(
                existing_article=duplicate,
                new_source=source,
                new_data=new_article_data,
                dry_run=False  # Tatsächlich löschen
            )

            if action == 'skip':
                # Bestehendes Artikel behalten, neues nicht erstellen
                # Bestimme Grund
                if new_priority < existing_priority:
                    reason = f"Niedrigere Priorität (neu: {new_priority} < alt: {existing_priority})"
                elif new_priority == existing_priority:
                    reason = f"Gleiche Priorität ({new_priority}), aber schlechterer Quality-Score (neu: {new_score} <= alt: {existing_score})"
                else:
                    reason = f"Unbekannt (Priority neu: {new_priority}, alt: {existing_priority})"

                logger.info(
                    f"DUPLIKAT ÜBERSPRUNGEN: '{title[:80]}'\n"
                    f"   Match-Typ: {match_type}\n"
                    f"   Bestehend: ID {duplicate.id}, Quelle '{duplicate.source.name}', Priorität {existing_priority}, Score {existing_score}\n"
                    f"   Neu:       Quelle '{source.name}', Priorität {new_priority}, Score {new_score}\n"
                    f"   Grund:     {reason}"
                )
                return None

            # Falls 'replace': bestehendes wurde gelöscht, erstelle neues unten
            # Bestimme Grund
            if new_priority > existing_priority:
                reason = f"Höhere Priorität (neu: {new_priority} > alt: {existing_priority})"
            elif new_priority == existing_priority:
                reason = f"Gleiche Priorität ({new_priority}), aber besserer Quality-Score (neu: {new_score} > alt: {existing_score})"
            else:
                reason = f"Unbekannt (Priority neu: {new_priority}, alt: {existing_priority})"

            logger.info(
                f"DUPLIKAT ERSETZT: '{title[:80]}'\n"
                f"   Match-Typ: {match_type}\n"
                f"   Gelöscht:  ID {duplicate.id}, Quelle '{duplicate.source.name}', Priorität {existing_priority}, Score {existing_score}\n"
                f"   Neu:       Quelle '{source.name}', Priorität {new_priority}, Score {new_score}\n"
                f"   Grund:     {reason}"
            )

    except Exception as e:
        if duplicate_found:
            # Duplikat wurde erkannt aber Handling fehlgeschlagen → sicherheitshalber nicht erstellen
            logger.error(
                f"Duplikat erkannt aber Handling fehlgeschlagen für '{entry_parsed.title}': {str(e)} - Artikel wird NICHT erstellt"
            )
            logger.error(traceback.format_exc())
            return None
        else:
            # Duplikatsuche fehlgeschlagen → einfacher DB-Check als Fallback
            logger.warning(f"Duplikatsuche fehlgeschlagen für '{entry_parsed.title}': {str(e)} - versuche einfachen DB-Check")
            logger.warning(traceback.format_exc())
            try:
                fallback_title = entry_parsed.title.strip() if hasattr(entry_parsed, 'title') and entry_parsed.title else None
                if fallback_title and date_value:
                    fb_date_start = date_value.replace(hour=0, minute=0, second=0, microsecond=0)
                    fb_date_end = date_value.replace(hour=23, minute=59, second=59, microsecond=999999)
                    fallback_dup = Article.objects.filter(
                        title__iexact=fallback_title,
                        date__range=(fb_date_start, fb_date_end)
                    ).select_related('source').first()
                    if fallback_dup:
                        logger.warning(
                            f"FALLBACK Duplikat gefunden: '{fallback_dup.title}' (ID: {fallback_dup.id}, "
                            f"Quelle: {fallback_dup.source.name}) - Artikel wird NICHT erstellt"
                        )
                        return None
            except Exception as fallback_error:
                logger.error(f"Auch Fallback-Duplikatcheck fehlgeschlagen: {fallback_error}")
            # Kein Fallback-Duplikat gefunden → Artikel erstellen um keinen Content zu verlieren
            logger.warning(f"Kein Fallback-Duplikat gefunden, erstelle Artikel: '{entry_parsed.title}'")
    # ========== END DUPLICATE DETECTION ==========

    # ========== LINK-BASED DUPLICATE CHECK ==========
    # Zusätzliche Sicherheit: Prüfe ob bereits ein Artikel mit diesem Link existiert
    if hasattr(entry_parsed, 'link') and entry_parsed.link:
        existing_by_link = Article.objects.filter(link=entry_parsed.link).first()
        if existing_by_link:
            logger.info(
                f"DUPLIKAT ÜBERSPRUNGEN (gleicher Link): '{entry_parsed.title[:80]}'\n"
                f"   Link: {entry_parsed.link}\n"
                f"   Bestehend: ID {existing_by_link.id}, Quelle '{existing_by_link.source.name}'"
            )
            return None
    # ========== END LINK-BASED DUPLICATE CHECK ==========

    # Create article
    # Reuse already-corrected title from duplicate detection
    if 'title' not in locals():
        title = correct_encoding(entry_parsed.title, source)
        title = fix_misencoded_text(title)
    if 'abstract' not in locals():
        abstract = correct_encoding(entry_parsed_summary, source)

    article = entry_class.objects.create(
        title=title,
        abstract=abstract,
        link=entry_parsed.link,
        date=date_value,
        moddate=date_value,
        source=source,
        image_url=resolve_image_url(),
        image=getattr(entry_parsed, "image_square", None) or _image,
        image_detail=_image_detail,
        image_source=getattr(entry_parsed, "image_source", None),
        published=source.default_published,
        foreign_id=getattr(entry_parsed, "foreign_id", None),
        up_for_review=True,
        language=getattr(entry_parsed, "language", None) or "de",
    )

    # ML Tagging wurde entfernt - Artikel werden nur noch mit Source Default-Tags versehen

    # Fallback: Falls keine Tags vom ML-Model vergeben wurden, nutze Default-Tags der Source
    # WICHTIG: Dieser Code läuft IMMER, auch wenn ML-Tagging fehlschlägt
    if not article.tags.exists():
        default_tags_count = source.default_tags.count()
        if default_tags_count > 0:
            for tag in source.default_tags.all():
                article.tags.add(tag)
        else:
            logger.warning(f"No tags assigned to article '{article.title}' - no ML tags and no default tags configured for source '{source.name}'")
    
    fallback_tag_id = settings.IMPORT_FALLBACK_ARTICLE_TAG_ID
    if fallback_tag_id and not article.tags.exists():
        # Ersatz-Thema der Instanz (IMPORT_FALLBACK_ARTICLE_TAG_ID)
        default_tag = Tag.objects.filter(id=fallback_tag_id).first()
        if default_tag:
            article.tags.add(default_tag)
        else:
            logger.error(f"Fallback tag {fallback_tag_id} does not exist, cannot assign to article '{article.title}'")
    
    # Add areas from source
    try:
        all_areas = Area.objects.all().values()
        source_areas = source.area.all().values() 

        article_area = None

        for source_area in source_areas:
            for area in all_areas:
                if area['name'] == source_area['name']:
                    article.area.add(area['id'])
                    article_area = source_area['name']
    except Exception as e:
        logger.error(f"Error adding areas to article '{article.title}': {str(e)}")
    
    return article


def update_article(article, entry_parsed, source, article_date, moddate=None, image_url=_IMAGE_URL_UNSET):
    """update an existing article

    Args:
        article (article): article object
        entry_parsed (feedparser.entry): parsed feedparser entry
        source (source): article source
        article_date (date): modification date
        moddate (date): modification date from the feed
        image_url (str): bereits ermittelte Bild-URL. Ohne Angabe wird sie hier
            geholt - get_image_url() laedt dafuer Artikelseite und Bild herunter,
            deshalb sollte ein Aufrufer, der sie schon hat, sie durchreichen.

    Returns:
        None
    """

    # check if article is none
    if article is None:
        # ToDo: log error
        # logger.error("Cannot update article: article is None")
        return
    
    # Check if entry_parsed is None and log an error if it is
    if entry_parsed is None:
        logger.error("Cannot update article: entry_parsed is None")
        return

    # Ensure entry_parsed has the necessary attributes
    if not hasattr(entry_parsed, 'title'):
        logger.error("Cannot update article: entry_parsed has no title")
        return

    if image_url is _IMAGE_URL_UNSET:
        _image_url = get_image_url(entry_parsed, source.default_image_url)
    else:
        _image_url = image_url

    changes = []  # List to track changes

    # Compare title
    new_title = fix_misencoded_text(entry_parsed.title)
    title_changed = article.title != new_title
    if title_changed:
        changes.append(f"Title changed from '{article.title}' to '{new_title}'")
        article.title = new_title

    # Compare link
    if article.link != entry_parsed.link:
        changes.append(f"Link changed from '{article.link}' to '{entry_parsed.link}'")
        article.link = entry_parsed.link

    # Compare summary
    if article.abstract != entry_parsed.summary:
        changes.append(f"Summary changed from '{article.abstract}' to '{entry_parsed.summary}'")
        article.abstract = entry_parsed.summary

    # Compare date
    if article.date != article_date:
        changes.append(f"Date changed from '{article.date}' to '{article_date}'")
        article.date = article_date

    # Compare moddate
    if article.moddate != moddate:
        changes.append(f"Modification date changed from '{article.moddate}' to '{moddate}'")
        article.moddate = moddate or localtime()

    # Compare image_url (bereits oben ermittelt - get_image_url() laedt Artikelseite
    # und Bild herunter, deshalb nicht ein zweites Mal aufrufen)
    if article.image_url != _image_url:
        changes.append(f"Image URL changed from '{article.image_url}' to '{_image_url}'")
        article.image_url = _image_url

    # Compare image
    if article.image != getattr(entry_parsed, "image_square", article.image):
        changes.append("Image content has changed.")
        article.image = getattr(entry_parsed, "image_square", article.image)

    # Compare image source
    if article.image_source != getattr(entry_parsed, "image_source", None):
        changes.append(f"Image source changed from '{article.image_source}' to '{getattr(entry_parsed, 'image_source', None)}'")
        article.image_source = getattr(entry_parsed, "image_source", None)

    # Log changes if any
    if changes:
        logger.info(f"Updating article '{article.title}' with the following changes: {', '.join(changes)}")
        article.save()

    # Nach Titeländerung: Cross-Source-Duplikate prüfen
    # Wenn der Titel sich ändert, kann der neue Titel jetzt mit einem Artikel
    # aus einer anderen Quelle übereinstimmen (z.B. BuB ändert Titel → identisch mit Tagesschau)
    if title_changed:
        try:
            pub_date = article.date
            # WICHTIG: nach der Quelle des Artikels ausschliessen, nicht nach der
            # gerade importierten. article_exists_for_update() findet ueber den Link
            # auch Artikel fremder Quellen (zwei Feeds koennen dieselben Beitraege
            # fuehren). Mit exclude_source=source faende der Artikel sonst sich
            # selbst als "Duplikat aus anderer Quelle" und wuerde sich loeschen.
            cross_dup = ArticleDuplicateHandler.find_cross_source_duplicate(
                title=article.title,
                pub_date=pub_date,
                abstract=article.abstract,
                exclude_source=article.source,
                use_fuzzy=True
            )
            if cross_dup and cross_dup.pk != article.pk:
                # Entscheide welcher Artikel behalten wird
                if ArticleDuplicateHandler.should_replace_existing(cross_dup, source, {
                    'title': article.title,
                    'abstract': article.abstract,
                    'content': article.content or '',
                    'image_url': article.image_url,
                    'image': article.image,
                    'image_source': article.image_source,
                }):
                    # Unser aktualisierter Artikel ist besser → Cross-Source-Duplikat löschen
                    logger.info(
                        f"TITEL-UPDATE DUPLIKAT: Lösche Cross-Source-Duplikat '{cross_dup.title}' "
                        f"(ID: {cross_dup.id}, Quelle: {cross_dup.source.name}, Priorität: {cross_dup.source.priority}) "
                        f"zugunsten von '{article.title}' (ID: {article.id}, Quelle: {source.name}, Priorität: {source.priority})"
                    )
                    cross_dup.delete()
                else:
                    # Cross-Source-Duplikat ist besser → unseren Artikel löschen
                    logger.info(
                        f"TITEL-UPDATE DUPLIKAT: Lösche eigenen Artikel '{article.title}' "
                        f"(ID: {article.id}, Quelle: {source.name}, Priorität: {source.priority}) "
                        f"zugunsten von '{cross_dup.title}' (ID: {cross_dup.id}, Quelle: {cross_dup.source.name}, Priorität: {cross_dup.source.priority})"
                    )
                    article.delete()
        except Exception as e:
            logger.warning(f"Duplikat-Prüfung nach Titeländerung fehlgeschlagen für '{article.title}': {e}")


