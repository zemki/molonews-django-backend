"""
Cross-Source Article Duplikat-Bereinigung

Dieser Command findet und entfernt Duplikate über alle Article-Sources hinweg unter Verwendung
der gleichen Logik wie der ArticleDuplicateHandler beim Import.

Usage:
  # Test-Modus (DRY-RUN): Analysiert Duplikate und erstellt MD-Report
  python manage.py cleanup_article_duplicates --test

  # Delete-Modus: Löscht tatsächlich die Duplikate
  python manage.py cleanup_article_duplicates --delete

  # Nur bestimmte Tage zurück schauen (Standard: 365)
  python manage.py cleanup_article_duplicates --test --days 90

  # Mit Log-File
  python manage.py cleanup_article_duplicates --test --log-file cleanup_articles.log
"""

from django.conf import settings
from collections import defaultdict
from datetime import datetime, timedelta
from logging import getLogger
import logging

from django.core.management.base import BaseCommand
from django.utils.timezone import localtime

from content.import_tools.article_duplicate_handler import ArticleDuplicateHandler
from content.models import Article

logger = getLogger("molonews")


class Command(BaseCommand):
    help = 'Findet und bereinigt Cross-Source Article-Duplikate (Standard: DRY-RUN)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--test',
            action='store_true',
            default=True,
            help='Test-Modus (DRY-RUN): Erstellt nur Report ohne zu löschen (Standard)'
        )
        parser.add_argument(
            '--delete',
            action='store_true',
            help='Delete-Modus: Löscht tatsächlich die Duplikate (ACHTUNG!)'
        )
        parser.add_argument(
            '--days',
            type=int,
            default=365,
            help='Anzahl Tage in die Vergangenheit (Standard: 365)'
        )
        parser.add_argument(
            '--log-file',
            type=str,
            default=None,
            help='Log-Datei für detaillierte Ausgabe'
        )

    def handle(self, *args, **options):
        delete_mode = options['delete']
        test_mode = not delete_mode  # DRY-RUN ist Standard
        days_back = options['days']
        log_file = options.get('log_file')

        # Setup file logging if specified
        file_logger = None
        if log_file:
            file_logger = self.setup_file_logging(log_file)

        def log_output(message, style=None):
            """Helper to log to both console and file"""
            if style:
                self.stdout.write(style(message))
            else:
                self.stdout.write(message)

            if file_logger:
                file_logger.info(message)

        mode = "DRY-RUN (Nur Analyse)" if test_mode else "DELETE-MODUS (Echtes Löschen!)"
        log_output(f'\n{"="*80}')
        log_output(f'=== {mode} ===', self.style.WARNING)
        log_output(f'{"="*80}\n')

        if delete_mode:
            log_output(
                'WARNUNG: Delete-Modus aktiv! Artikel werden tatsächlich gelöscht!',
                self.style.ERROR
            )
            log_output('Drücke Ctrl+C zum Abbrechen... (5 Sekunden)\n')
            import time
            time.sleep(5)

        # Get articles
        cutoff_date = datetime.now() - timedelta(days=days_back)

        queryset = Article.objects.filter(
            date__gte=cutoff_date,
            published=True
        ).select_related('source').order_by('id')

        total_articles = queryset.count()
        log_output(f'Prüfe {total_articles} Artikel (letzte {days_back} Tage)...\n')

        # Group articles by their unique key (exakter Match)
        articles_by_key = defaultdict(list)

        for article in queryset:
            article_key = ArticleDuplicateHandler.build_article_key(
                article.title,
                localtime(article.date)
            )
            articles_by_key[article_key].append(article)

        # Find duplicates (exakt und fuzzy)
        duplicate_groups = []
        total_duplicates = 0
        total_to_delete = 0
        processed_article_ids = set()

        # PHASE 1: Exakte Duplikate
        log_output('Phase 1: Suche nach exakten Duplikaten...')
        for article_key, article_list in articles_by_key.items():
            if len(article_list) > 1:
                # Sort by priority and quality score
                article_list.sort(
                    key=lambda a: (
                        a.source.priority,
                        ArticleDuplicateHandler.calculate_quality_score(a),
                        -a.id  # Bei Gleichstand: älterer Artikel bevorzugen
                    ),
                    reverse=True
                )

                duplicate_groups.append({
                    'key': article_key,
                    'articles': article_list,
                    'keep': article_list[0],
                    'delete': article_list[1:],
                    'match_type': 'exakt'
                })

                total_duplicates += len(article_list)
                total_to_delete += len(article_list) - 1

                # Markiere als verarbeitet
                for article in article_list:
                    processed_article_ids.add(article.id)

        log_output(f'  -> {len(duplicate_groups)} exakte Duplikat-Gruppen gefunden\n')

        # PHASE 2: Fuzzy Duplikate
        log_output('Phase 2: Suche nach ähnlichen Duplikaten (Fuzzy-Match)...')
        unprocessed_articles = [a for a in queryset if a.id not in processed_article_ids]

        # Gruppiere nach Datum (± 1 Tag)
        fuzzy_groups = defaultdict(list)

        for article in unprocessed_articles:
            article_date = localtime(article.date)
            date_key = article_date.strftime('%Y-%m-%d')
            fuzzy_groups[date_key].append(article)

        fuzzy_count = 0
        for date_key, article_list in fuzzy_groups.items():
            if len(article_list) <= 1:
                continue

            # Vergleiche alle Artikel paarweise
            already_grouped = set()

            for i, article1 in enumerate(article_list):
                if article1.id in already_grouped:
                    continue

                similar_articles = [article1]

                for article2 in article_list[i+1:]:
                    if article2.id in already_grouped:
                        continue

                    # Prüfe ob Titel ähnlich sind
                    if ArticleDuplicateHandler.are_titles_similar(article1.title, article2.title):
                        # Zusätzlich: prüfe Zeitfenster (± 24h)
                        time_diff = abs((localtime(article1.date) - localtime(article2.date)).total_seconds() / 3600)
                        if time_diff <= ArticleDuplicateHandler.TIME_TOLERANCE_HOURS:
                            similar_articles.append(article2)
                            already_grouped.add(article2.id)

                # Wenn ähnliche Artikel gefunden
                if len(similar_articles) > 1:
                    similar_articles.sort(
                        key=lambda a: (
                            a.source.priority,
                            ArticleDuplicateHandler.calculate_quality_score(a),
                            -a.id
                        ),
                        reverse=True
                    )

                    keep_article = similar_articles[0]
                    fuzzy_display_key = f"{keep_article.title[:80]}|{date_key}|FUZZY"

                    duplicate_groups.append({
                        'key': fuzzy_display_key,
                        'articles': similar_articles,
                        'keep': keep_article,
                        'delete': similar_articles[1:],
                        'match_type': 'fuzzy'
                    })

                    total_duplicates += len(similar_articles)
                    total_to_delete += len(similar_articles) - 1
                    fuzzy_count += 1

                    already_grouped.add(article1.id)

        log_output(f'  -> {fuzzy_count} Fuzzy Duplikat-Gruppen gefunden\n')

        # Generate report
        if duplicate_groups:
            log_output(
                f'\n{"="*80}\n'
                f'ERGEBNIS: {len(duplicate_groups)} Duplikat-Gruppen gefunden\n'
                f'  - Gesamt Artikel in Gruppen: {total_duplicates}\n'
                f'  - Zu löschen: {total_to_delete}\n'
                f'{"="*80}\n',
                self.style.WARNING
            )

            # Create markdown report
            report_lines = [
                '# Article Duplikat-Bereinigung Report',
                f'\n**Datum:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
                f'**Modus:** {mode}',
                f'**Artikel geprüft:** {total_articles}',
                f'**Zeitraum:** Letzte {days_back} Tage',
                f'**Duplikat-Gruppen gefunden:** {len(duplicate_groups)}',
                f'**Artikel zu löschen:** {total_to_delete}',
                '\n---\n'
            ]

            articles_deleted = 0
            articles_skipped = 0
            articles_protected = 0  # Artikel aus DUPLICATE_PROTECTED_SOURCE_IDS

            for i, group in enumerate(duplicate_groups, 1):
                keep_article = group['keep']
                delete_articles = group['delete']
                match_type = group.get('match_type', 'exakt')

                # Console/Log output
                match_emoji = '🔍' if match_type == 'fuzzy' else '✓'
                log_output(f'\n{i}. {match_emoji} Duplikat-Gruppe ({match_type.upper()})')
                log_output(f'   Key: {group["key"][:100]}...')

                keep_score = ArticleDuplicateHandler.calculate_quality_score(keep_article)
                log_output(
                    f'   KEEP: [{keep_article.source.name}] (Priorität: {keep_article.source.priority}, '
                    f'Score: {keep_score}) ID: {keep_article.id}',
                    self.style.SUCCESS
                )
                log_output(f'        Titel: "{keep_article.title[:80]}"')

                # Markdown report
                match_badge = '🔍 FUZZY MATCH' if match_type == 'fuzzy' else '✓ EXAKT'
                report_lines.append(f'## {i}. {keep_article.title[:100]} `[{match_badge}]`')
                report_lines.append(f'\n**Article-Key:** `{group["key"][:100]}`')
                report_lines.append(f'**Datum:** {keep_article.date.strftime("%Y-%m-%d %H:%M")}')
                report_lines.append(f'**Abstract:** {(keep_article.abstract or "N/A")[:150]}...')
                report_lines.append('\n### ✅ Artikel wird BEHALTEN:')
                report_lines.append(f'- **ID:** {keep_article.id}')
                report_lines.append(f'- **Source:** {keep_article.source.name} (ID: {keep_article.source.id})')
                report_lines.append(f'- **Priorität:** {keep_article.source.priority}')
                report_lines.append(f'- **Quality Score:** {keep_score}')
                report_lines.append(f'- **Bild:** {"Ja" if keep_article.image_url or keep_article.image else "Nein"}')
                report_lines.append(f'- **Content-Länge:** {len(keep_article.content or "")} Zeichen')
                report_lines.append(f'- **Abstract-Länge:** {len(keep_article.abstract or "")} Zeichen')
                report_lines.append(f'- **Link:** {keep_article.link or "N/A"}')

                report_lines.append(f'\n### ❌ Artikel werden {"GELÖSCHT" if delete_mode else "WÜRDEN GELÖSCHT"}:')

                for del_article in delete_articles:
                    quality_score = ArticleDuplicateHandler.calculate_quality_score(del_article)

                    # Console/Log output
                    log_output(
                        f'   {"DELETE" if delete_mode else "WOULD DELETE"}: [{del_article.source.name}] '
                        f'(Priorität: {del_article.source.priority}, Score: {quality_score}) '
                        f'ID: {del_article.id}',
                        self.style.ERROR
                    )
                    log_output(f'        Titel: "{del_article.title[:80]}"')

                    # Markdown report
                    report_lines.append(f'\n#### Artikel ID {del_article.id}')
                    report_lines.append(f'- **Titel:** {del_article.title}')
                    report_lines.append(f'- **Source:** {del_article.source.name} (ID: {del_article.source.id})')
                    report_lines.append(f'- **Priorität:** {del_article.source.priority}')
                    report_lines.append(f'- **Quality Score:** {quality_score}')
                    report_lines.append(f'- **Bild:** {"Ja" if del_article.image_url or del_article.image else "Nein"}')
                    report_lines.append(f'- **Content-Länge:** {len(del_article.content or "")} Zeichen')
                    report_lines.append(f'- **Abstract-Länge:** {len(del_article.abstract or "")} Zeichen')
                    report_lines.append(f'- **Link:** {del_article.link or "N/A"}')

                    # Bei Fuzzy Match: Zeige Ähnlichkeits-Scores
                    if match_type == 'fuzzy':
                        string_sim = ArticleDuplicateHandler.calculate_title_similarity(keep_article.title, del_article.title)
                        token_sim = ArticleDuplicateHandler.calculate_token_similarity(keep_article.title, del_article.title)
                        report_lines.append(f'- **String-Ähnlichkeit:** {string_sim:.1%}')
                        report_lines.append(f'- **Token-Ähnlichkeit:** {token_sim:.1%}')

                        if keep_article.abstract and del_article.abstract:
                            abstract_sim = ArticleDuplicateHandler.calculate_abstract_similarity(
                                keep_article.abstract, del_article.abstract
                            )
                            report_lines.append(f'- **Abstract-Ähnlichkeit:** {abstract_sim:.1%}')

                    # Reason for deletion
                    if del_article.source.priority < keep_article.source.priority:
                        reason = f'Niedrigere Source-Priorität ({del_article.source.priority} < {keep_article.source.priority})'
                    elif del_article.source.priority > keep_article.source.priority:
                        reason = f'⚠️ WARNUNG: Höhere Priorität! ({del_article.source.priority} > {keep_article.source.priority})'
                    else:
                        reason = f'Gleiche Priorität, niedrigerer Quality Score ({quality_score} vs {keep_score})'

                    if match_type == 'fuzzy':
                        reason += ' (Fuzzy Match: ähnlicher Titel zur ähnlichen Zeit)'

                    report_lines.append(f'- **Grund:** {reason}')

                    # Artikel aus geschützten Quellen nie löschen (DUPLICATE_PROTECTED_SOURCE_IDS)
                    is_protected = del_article.source_id in settings.DUPLICATE_PROTECTED_SOURCE_IDS

                    if is_protected:
                        articles_protected += 1
                        protection_msg = f'🛡️  Artikel ID {del_article.id} ist geschützt (Quelle {del_article.source_id})'
                        log_output(f'      {protection_msg}', self.style.WARNING)
                        report_lines.append(f'- **🛡️  GESCHÜTZT:** {protection_msg}')
                    # Actually delete if in delete mode
                    elif delete_mode:
                        try:
                            del_article.delete()
                            articles_deleted += 1
                            log_output(f'      ✓ Artikel ID {del_article.id} gelöscht', self.style.SUCCESS)
                        except Exception as e:
                            articles_skipped += 1
                            error_msg = f'Fehler beim Löschen von Artikel ID {del_article.id}: {e}'
                            log_output(f'      ✗ {error_msg}', self.style.ERROR)
                            report_lines.append(f'- **❌ FEHLER:** {error_msg}')
                    else:
                        log_output(f'      [DRY-RUN] Würde Artikel ID {del_article.id} löschen')

                report_lines.append('\n---\n')

            # Summary
            log_output(f'\n{"="*80}')
            log_output('ZUSAMMENFASSUNG:', self.style.WARNING)
            log_output(f'  Geprüfte Artikel: {total_articles}')
            log_output(f'  Gefundene Duplikat-Gruppen: {len(duplicate_groups)}')
            log_output(f'  Artikel die gelöscht werden sollen: {total_to_delete}')

            if articles_protected > 0:
                log_output(
                    f'  🛡️  Geschützte Artikel: {articles_protected}',
                    self.style.WARNING
                )

            if delete_mode:
                log_output(f'  ✓ Tatsächlich gelöscht: {articles_deleted}', self.style.SUCCESS)
                if articles_skipped > 0:
                    log_output(f'  ✗ Fehler beim Löschen: {articles_skipped}', self.style.ERROR)
            else:
                log_output(
                    '\n  ℹ️  DRY-RUN-MODUS: Keine Artikel wurden gelöscht.',
                    self.style.WARNING
                )
                log_output(
                    '  Zum tatsächlichen Löschen führe aus:',
                    self.style.WARNING
                )
                log_output(
                    '  python manage.py cleanup_article_duplicates --delete',
                    self.style.WARNING
                )

            log_output(f'{"="*80}\n')

        else:
            log_output('\n✓ Keine Duplikate gefunden!\n', self.style.SUCCESS)

        if log_file:
            log_output(f'\n✓ Detaillierte Logs gespeichert in: {log_file}', self.style.SUCCESS)

    def setup_file_logging(self, log_file):
        """Setup file logger"""
        file_logger = logging.getLogger('cleanup_articles_file')
        file_logger.setLevel(logging.INFO)

        # Remove existing handlers
        file_logger.handlers = []

        # Create file handler
        fh = logging.FileHandler(log_file, mode='w', encoding='utf-8')
        fh.setLevel(logging.INFO)

        # Create formatter
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)

        file_logger.addHandler(fh)

        return file_logger
