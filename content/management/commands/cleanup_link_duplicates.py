"""
Link-basierte Artikel-Duplikat-Bereinigung

Dieser Command findet und entfernt Artikel-Duplikate, die denselben Link haben.
Das Problem entsteht, wenn der Importer denselben Artikel mehrfach anlegt,
weil die Duplikat-Erkennung beim Import nicht greift.

Zwei Modi:
1. same-source: Artikel mit gleichem Link UND gleicher Source (Standard)
2. all: Zusätzlich Artikel mit gleichem Link über verschiedene Sources

Behalten wird jeweils der Artikel mit:
- Höherer Source-Priorität
- Besserem Quality-Score (Bild, Content-Länge, Abstract-Länge)
- Bei Gleichstand: niedrigere ID (älterer Artikel)

Usage:
  # Test-Modus (DRY-RUN): Analysiert Duplikate
  python manage.py cleanup_link_duplicates

  # Delete-Modus: Löscht tatsächlich die Duplikate
  python manage.py cleanup_link_duplicates --delete

  # Auch Cross-Source Duplikate (gleicher Link, verschiedene Sources)
  python manage.py cleanup_link_duplicates --delete --cross-source

  # Nur bestimmte Source
  python manage.py cleanup_link_duplicates --source "Name der Quelle"

  # Sources ausschließen
  python manage.py cleanup_link_duplicates --exclude-source "Redaktion"

  # Limit für Anzahl zu verarbeitender Gruppen
  python manage.py cleanup_link_duplicates --delete --limit 100
"""

from logging import getLogger

from django.core.management.base import BaseCommand
from django.db.models import Count

from content.import_tools.article_duplicate_handler import ArticleDuplicateHandler
from content.models import Article

logger = getLogger("molonews")


class Command(BaseCommand):
    help = 'Findet und bereinigt Artikel-Duplikate mit gleichem Link (Standard: DRY-RUN)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete',
            action='store_true',
            help='Delete-Modus: Löscht tatsächlich die Duplikate (ACHTUNG!)'
        )
        parser.add_argument(
            '--cross-source',
            action='store_true',
            help='Auch Cross-Source Duplikate bereinigen (gleicher Link, verschiedene Sources)'
        )
        parser.add_argument(
            '--source',
            type=str,
            default=None,
            help='Nur Duplikate einer bestimmten Source bereinigen (Name)'
        )
        parser.add_argument(
            '--exclude-source',
            type=str,
            action='append',
            default=[],
            help='Source(s) ausschließen (kann mehrfach angegeben werden)'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='Maximale Anzahl Duplikat-Gruppen zu verarbeiten (0 = alle)'
        )

    def handle(self, *args, **options):
        delete_mode = options['delete']
        cross_source = options['cross_source']
        source_filter = options.get('source')
        exclude_sources = options.get('exclude_source', [])
        limit = options['limit']

        mode = "DELETE-MODUS" if delete_mode else "DRY-RUN (Nur Analyse)"
        self.stdout.write(f'\n{"="*80}')
        self.stdout.write(self.style.WARNING(f'=== Link-Duplikat-Bereinigung: {mode} ==='))
        self.stdout.write(f'{"="*80}\n')

        if delete_mode:
            self.stdout.write(self.style.ERROR(
                'WARNUNG: Delete-Modus aktiv! Artikel werden tatsächlich gelöscht!'
            ))
            self.stdout.write('Drücke Ctrl+C zum Abbrechen... (5 Sekunden)\n')
            import time
            time.sleep(5)

        # ========== PHASE 1: Same-Source Duplikate ==========
        self.stdout.write(self.style.WARNING('\n--- Phase 1: Same-Source Duplikate (gleicher Link + gleiche Source) ---\n'))

        queryset = (Article.objects
            .exclude(link__isnull=True)
            .exclude(link=''))

        if source_filter:
            queryset = queryset.filter(source__name=source_filter)
            self.stdout.write(f'Filter: Nur Source "{source_filter}"\n')

        if exclude_sources:
            queryset = queryset.exclude(source__name__in=exclude_sources)
            self.stdout.write(f'Ausgeschlossen: {", ".join(exclude_sources)}\n')

        # Find duplicate groups
        dupe_groups = (queryset
            .values('link', 'source')
            .annotate(cnt=Count('id'))
            .filter(cnt__gt=1)
            .order_by('-cnt'))

        total_groups = dupe_groups.count()
        total_extra = sum(d['cnt'] - 1 for d in dupe_groups)

        self.stdout.write(f'Gefunden: {total_groups} Duplikat-Gruppen mit {total_extra} überflüssigen Artikeln\n')

        if limit > 0:
            dupe_groups = dupe_groups[:limit]
            self.stdout.write(f'Limit: Verarbeite maximal {limit} Gruppen\n')

        deleted_count = 0
        kept_count = 0
        error_count = 0

        for i, group in enumerate(dupe_groups, 1):
            articles = list(
                Article.objects
                .filter(link=group['link'], source_id=group['source'])
                .select_related('source')
                .order_by('id')
            )

            if len(articles) <= 1:
                continue

            # Sort: best article first
            articles.sort(
                key=lambda a: (
                    a.source.priority,
                    ArticleDuplicateHandler.calculate_quality_score(a),
                    -a.id  # Älterer Artikel bevorzugt bei Gleichstand
                ),
                reverse=True
            )

            keep = articles[0]
            to_delete = articles[1:]

            self.stdout.write(
                f'\n[{i}/{total_groups}] Link: {group["link"][:120]}'
            )
            self.stdout.write(self.style.SUCCESS(
                f'  KEEP: ID {keep.id} | {keep.source.name} | '
                f'Score: {ArticleDuplicateHandler.calculate_quality_score(keep)} | '
                f'"{keep.title[:80]}"'
            ))
            abstract_preview = (keep.abstract or '')[:100].replace('\n', ' ')
            if abstract_preview:
                self.stdout.write(f'        Abstract: {abstract_preview}...')

            for article in to_delete:
                title_preview = article.title[:80] if article.title else 'N/A'
                abstract_preview = (article.abstract or '')[:100].replace('\n', ' ')
                date_str = article.date.strftime('%Y-%m-%d') if article.date else 'N/A'

                if delete_mode:
                    try:
                        article_id = article.id
                        article.delete()
                        deleted_count += 1
                        self.stdout.write(self.style.ERROR(
                            f'  DELETE: ID {article_id} | {date_str} | "{title_preview}"'
                        ))
                        if abstract_preview:
                            self.stdout.write(f'          Abstract: {abstract_preview}...')
                    except Exception as e:
                        error_count += 1
                        self.stdout.write(self.style.ERROR(
                            f'  FEHLER: ID {article.id}: {e}'
                        ))
                else:
                    deleted_count += 1
                    self.stdout.write(
                        f'  WOULD DELETE: ID {article.id} | {date_str} | "{title_preview}"'
                    )
                    if abstract_preview:
                        self.stdout.write(f'          Abstract: {abstract_preview}...')

            kept_count += 1

        self.stdout.write('\n--- Phase 1 Ergebnis ---')
        self.stdout.write(f'  Duplikat-Gruppen: {total_groups}')
        self.stdout.write(f'  Artikel {"gelöscht" if delete_mode else "würden gelöscht"}: {deleted_count}')
        if error_count:
            self.stdout.write(self.style.ERROR(f'  Fehler: {error_count}'))

        # ========== PHASE 2: Cross-Source Duplikate ==========
        if cross_source:
            self.stdout.write(self.style.WARNING(
                '\n--- Phase 2: Cross-Source Duplikate (gleicher Link, verschiedene Sources) ---\n'
            ))

            queryset2 = (Article.objects
                .exclude(link__isnull=True)
                .exclude(link=''))

            if source_filter:
                queryset2 = queryset2.filter(source__name=source_filter)

            if exclude_sources:
                queryset2 = queryset2.exclude(source__name__in=exclude_sources)

            cross_groups = (queryset2
                .values('link')
                .annotate(cnt=Count('id'), src_cnt=Count('source', distinct=True))
                .filter(cnt__gt=1, src_cnt__gt=1)
                .order_by('-cnt'))

            cross_total = cross_groups.count()
            cross_extra = sum(d['cnt'] - 1 for d in cross_groups)

            self.stdout.write(
                f'Gefunden: {cross_total} Cross-Source Gruppen mit {cross_extra} überflüssigen Artikeln\n'
            )

            if limit > 0:
                cross_groups = cross_groups[:limit]

            cross_deleted = 0
            cross_errors = 0

            for i, group in enumerate(cross_groups, 1):
                articles = list(
                    Article.objects
                    .filter(link=group['link'])
                    .select_related('source')
                    .order_by('id')
                )

                if len(articles) <= 1:
                    continue

                # Sort: best article first (priority, quality, oldest)
                articles.sort(
                    key=lambda a: (
                        a.source.priority,
                        ArticleDuplicateHandler.calculate_quality_score(a),
                        -a.id
                    ),
                    reverse=True
                )

                keep = articles[0]
                to_delete = articles[1:]

                self.stdout.write(
                    f'\n[{i}/{cross_total}] Link: {group["link"][:120]}'
                )
                self.stdout.write(self.style.SUCCESS(
                    f'  KEEP: ID {keep.id} | {keep.source.name} (Prio: {keep.source.priority}) | '
                    f'"{keep.title[:80]}"'
                ))
                abstract_preview = (keep.abstract or '')[:100].replace('\n', ' ')
                if abstract_preview:
                    self.stdout.write(f'        Abstract: {abstract_preview}...')

                for article in to_delete:
                    title_preview = article.title[:80] if article.title else 'N/A'
                    abstract_preview = (article.abstract or '')[:100].replace('\n', ' ')
                    date_str = article.date.strftime('%Y-%m-%d') if article.date else 'N/A'

                    if delete_mode:
                        try:
                            article_id = article.id
                            source_name = article.source.name
                            source_prio = article.source.priority
                            article.delete()
                            cross_deleted += 1
                            self.stdout.write(self.style.ERROR(
                                f'  DELETE: ID {article_id} | {source_name} (Prio: {source_prio}) | {date_str} | "{title_preview}"'
                            ))
                            if abstract_preview:
                                self.stdout.write(f'          Abstract: {abstract_preview}...')
                        except Exception as e:
                            cross_errors += 1
                            self.stdout.write(self.style.ERROR(
                                f'  FEHLER: ID {article.id}: {e}'
                            ))
                    else:
                        cross_deleted += 1
                        self.stdout.write(
                            f'  WOULD DELETE: ID {article.id} | {article.source.name} (Prio: {article.source.priority}) | {date_str} | "{title_preview}"'
                        )
                        if abstract_preview:
                            self.stdout.write(f'          Abstract: {abstract_preview}...')

            self.stdout.write('\n--- Phase 2 Ergebnis ---')
            self.stdout.write(f'  Cross-Source Gruppen: {cross_total}')
            self.stdout.write(f'  Artikel {"gelöscht" if delete_mode else "würden gelöscht"}: {cross_deleted}')
            if cross_errors:
                self.stdout.write(self.style.ERROR(f'  Fehler: {cross_errors}'))

            deleted_count += cross_deleted
            error_count += cross_errors

        # ========== ZUSAMMENFASSUNG ==========
        self.stdout.write(f'\n{"="*80}')
        self.stdout.write(self.style.WARNING('ZUSAMMENFASSUNG:'))
        self.stdout.write(f'  Artikel {"gelöscht" if delete_mode else "würden gelöscht"}: {deleted_count}')
        if error_count:
            self.stdout.write(self.style.ERROR(f'  Fehler: {error_count}'))

        if not delete_mode:
            self.stdout.write(self.style.WARNING(
                '\n  DRY-RUN-MODUS: Keine Artikel wurden gelöscht.'
            ))
            self.stdout.write(self.style.WARNING(
                '  Zum tatsächlichen Löschen:'
            ))
            cmd = '  python manage.py cleanup_link_duplicates --delete'
            if cross_source:
                cmd += ' --cross-source'
            if source_filter:
                cmd += f' --source "{source_filter}"'
            for ex in exclude_sources:
                cmd += f' --exclude-source "{ex}"'
            self.stdout.write(self.style.WARNING(cmd))

        self.stdout.write(f'{"="*80}\n')
