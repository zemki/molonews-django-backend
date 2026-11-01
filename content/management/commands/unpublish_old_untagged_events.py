from django.core.management.base import BaseCommand
from django.db.models import Count
from content.models import EventV4
from datetime import datetime
import pytz


class Command(BaseCommand):
    help = (
        'Setzt vergangene Events (2025 und älter) auf unveröffentlicht, '
        'wenn keine Themen/Tags zugeordnet sind.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Zeigt an, was geändert würde, ohne tatsächlich zu ändern.',
        )
        parser.add_argument(
            '--year',
            type=int,
            default=2025,
            help='Events bis einschließlich diesem Jahr werden berücksichtigt (Standard: 2025).',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        cutoff_year = options['year']

        cutoff_date = datetime(cutoff_year + 1, 1, 1, tzinfo=pytz.UTC)

        # Veröffentlichte Events mit start_date <= Ende des cutoff_year UND ohne Tags
        events = (
            EventV4.objects
            .filter(published=True, start_date__lt=cutoff_date)
            .annotate(tag_count=Count('tags'))
            .filter(tag_count=0)
        )

        count = events.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS(
                'Keine Events gefunden, die den Kriterien entsprechen.'
            ))
            return

        self.stdout.write(self.style.WARNING(
            f'{count} veröffentlichte Events von {cutoff_year} oder älter ohne Tags gefunden.'
        ))

        if dry_run:
            self.stdout.write(self.style.NOTICE(
                'DRY-RUN: Es werden keine Änderungen vorgenommen.'
            ))
            for event in events[:20]:
                self.stdout.write(
                    f'  ID {event.id} | {event.start_date.strftime("%Y-%m-%d")} | '
                    f'{event.title[:80]}'
                )
            if count > 20:
                self.stdout.write(f'  ... und {count - 20} weitere Events.')
            return

        # IDs sammeln, da das annotierte Queryset nicht direkt update() unterstützt
        event_ids = list(events.values_list('id', flat=True))
        updated = EventV4.objects.filter(id__in=event_ids).update(published=False)

        self.stdout.write(self.style.SUCCESS(
            f'{updated} Events wurden auf unveröffentlicht gesetzt.'
        ))
