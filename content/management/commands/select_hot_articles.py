from django.core.management.base import BaseCommand
from django.utils.timezone import localtime, now
from content.models import Article, Area
from django.utils.timezone import make_aware, datetime
from random import choice
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Markiert den Artikel mit der höchsten Anfragezahl (request_count) als 'is_hot' für jede Area"

    def handle(self, *args, **options):
        today_start = make_aware(datetime.combine(localtime(now()).date(), datetime.min.time()))
        today_end = make_aware(datetime.combine(localtime(now()).date(), datetime.max.time()))

  
        # Gehe alle Areas durch
        areas = Area.objects.all()

        for area in areas:

            area_name = area.name
            # get id of area

            if area_name == "Greifswald":
                continue

            # Prüfen, ob bereits ein Artikel an diesem Tag als "is_hot" markiert wurde
            if Article.objects.filter(area__in=[area], is_hot=True, date__range=(today_start, today_end)).exists():
                logger.info(f"Bereits ein Artikel für Area '{area.name}' ist heute als 'is_hot' markiert. Vorgang abgebrochen.")
                continue

            # Hole die Artikel des heutigen Tages für die aktuelle Area. Schaue ob die Area in dem Abstract erwähnt wird.
            today_articles = Article.objects.filter(area__in=[area], date__range=(today_start, today_end), abstract__icontains=area_name, published=True)

            # Falls die Area nicht erwähnt wird, dann schaue nach Artikeln ohne die area
            if not today_articles.exists():
                today_articles = Article.objects.filter(area__in=[area], date__range=(today_start, today_end), published=True)

            # Falls keine Artikel vorhanden sind, abbrechen
            if not today_articles.exists():
                logger.warning(f"Keine Artikel für Area '{area.name}' wurden heute importiert.")
                continue

            # Suche den Artikel mit der höchsten Anfragezahl (request_count)
            max_request_count_article = today_articles.order_by('-request_count').first()

            # Falls alle Artikel die request_count von 0 haben, wähle einen zufälligen Artikel
            if max_request_count_article and max_request_count_article.request_count == 0:
                max_request_count_article = choice(today_articles)
                logger.info(f"Alle request_count für Area '{area.name}' sind 0. Zufälliger Artikel ausgewählt: {max_request_count_article.title}")

            # Markiere den Artikel als "is_hot"
            max_request_count_article.is_hot = True
            max_request_count_article.save()

            logger.info(f"Artikel '{max_request_count_article.title}' für Area '{area.name}' wurde als 'is_hot' markiert.")

        logger.info("Skript erfolgreich abgeschlossen.")
