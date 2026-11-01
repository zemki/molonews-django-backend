"""
Markiert EventV4-Einträge als is_past=True wenn ALLE ihre Occurrences in der
Vergangenheit liegen UND der Titel kein Wiederholungs-Keyword enthält.

Kein LLM — rein deterministisch, kostenlos, schnell. Streng konservativ:
Events mit Begriffen wie "Markt", "Stammtisch", "Sonntagsspaziergang",
"Führung" etc. werden NICHT automatisch markiert, auch wenn ihre letzten
bekannten Occurrences vergangen sind (stattdessen gelogged als "flagged").

Beispiele:
    python manage.py mark_events_past --dry-run
    python manage.py mark_events_past --grace-days 3
    python manage.py mark_events_past --include-recurring  # auch Titel mit Recurrence-Keywords markieren

Backup: wird vor Änderung unter backups/mark_events_past_*.json abgelegt.
Log:    logs/events_past/<YYYY-MM-DD>/NN.md
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Max
from django.conf import settings
from django.utils import timezone

from content.models import EventV4, Event_Occurrence
from content.management.event_utils import backup_events, title_has_recurring_keyword

LOG_ROOT = Path(settings.LOG_DIR) / 'events_past'
LOG_MAX_ENTRIES_PER_FILE = 200


def _next_log_path() -> Path:
    today = datetime.now().strftime('%Y-%m-%d')
    day_dir = LOG_ROOT / today
    day_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(day_dir.glob('*.md'))
    if existing:
        last = existing[-1]
        try:
            cnt = len(re.findall(r'^### ', last.read_text(encoding='utf-8'), re.MULTILINE))
        except Exception:
            cnt = 0
        if cnt < LOG_MAX_ENTRIES_PER_FILE:
            return last
        m = re.match(r'(\d+)\.md', last.name)
        n = (int(m.group(1)) + 1) if m else 1
    else:
        n = 1
    path = day_dir / f'{n:02d}.md'
    path.write_text(
        f'# PAST-Markierung · {today} · Datei {n:02d}\n\n'
        'Format: `### <event_id> · <STATUS> · <titel>` mit letzter Occurrence + Begründung.\n\n',
        encoding='utf-8',
    )
    return path


def _write_log(lines: list[str]) -> None:
    path = _next_log_path()
    with open(path, 'a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


class Command(BaseCommand):
    help = ('Setzt is_past=True für EventV4 deren letzte Occurrence vergangen ist '
            'und deren Titel kein Wiederholungs-Keyword enthält. Keine LLM-Nutzung.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Keine DB-Änderungen, nur Report')
        parser.add_argument('--limit', type=int, default=None,
                            help='Maximal N Events markieren')
        parser.add_argument('--grace-days', type=int, default=1,
                            help='Nur Events deren letzte Occurrence mind. N Tage zurückliegt (default: 1)')
        parser.add_argument('--include-recurring', action='store_true',
                            help='Auch Events mit Recurrence-Keywords im Titel markieren (default: überspringen)')

    def handle(self, *args, **opts):
        dry_run = opts['dry_run']
        limit = opts['limit']
        grace_days = opts['grace_days']
        include_recurring = opts['include_recurring']

        now = timezone.now()
        cutoff = now - timedelta(days=grace_days)

        # Kandidaten: published, non-draft, not-past, alle Occurrences vor cutoff.
        # occ_count wird in der gleichen Query annotiert (vermeidet N+1).
        qs = (EventV4.objects
              .filter(is_past=False, draft=False, published=True)
              .annotate(
                  last_end=Max('occurrences__end_datetime'),
                  occ_count=Count('occurrences'),
              )
              .filter(last_end__lt=cutoff))

        to_mark: list[tuple[EventV4, datetime, int]] = []  # (event, last_end, occ_count)
        flagged: list[tuple[EventV4, datetime, str]] = []  # (event, last_end, keyword)

        for event in qs.iterator():
            last_end = event.last_end
            occ_count = event.occ_count
            if not include_recurring:
                kw = title_has_recurring_keyword(event.title or '')
                if kw:
                    flagged.append((event, last_end, kw))
                    continue
            to_mark.append((event, last_end, occ_count))

        if limit:
            to_mark = to_mark[:limit]

        self.stdout.write(f'Heute: {now:%Y-%m-%d %H:%M}  |  Cutoff (älter als): {cutoff:%Y-%m-%d %H:%M}')
        self.stdout.write(f'Kandidaten (letzte Occurrence vor Cutoff): {qs.count()}')
        self.stdout.write(f'Zu markieren: {len(to_mark)}')
        self.stdout.write(f'Übersprungen (Recurrence-Keyword im Titel): {len(flagged)}')
        self.stdout.write(f'Dry-run: {dry_run}')

        if to_mark:
            backup_ids = [e.id for e, _, _ in to_mark]
            backup_path = backup_events(backup_ids, dry_run, command_name='mark_events_past')
            self.stdout.write(f'Backup: {backup_path}')
        self.stdout.write('')

        marked = 0
        for event, last_end, occ_count in to_mark:
            title = (event.title or '').strip().replace('\n', ' ')[:80]
            self.stdout.write(f'[{event.id}] letzte occ: {last_end:%Y-%m-%d} ({occ_count} occ) — {title}')
            if not dry_run:
                with transaction.atomic():
                    Event_Occurrence.objects.filter(event=event).delete()
                    event.is_past = True
                    event.save(update_fields=['is_past'])
            marked += 1
            _write_log([
                f'### {event.id} · {"MARKED (dry)" if dry_run else "MARKED"} · {title}',
                f'- letzte occurrence: {last_end:%Y-%m-%d %H:%M}',
                f'- occurrence_count_vorher: {occ_count}',
                '',
            ])

        for event, last_end, kw in flagged:
            title = (event.title or '').strip().replace('\n', ' ')[:80]
            _write_log([
                f'### {event.id} · FLAGGED_RECURRING · {title}',
                f'- letzte occurrence: {last_end:%Y-%m-%d %H:%M}',
                f'- keyword: {kw}',
                '- aktion: NICHT markiert (manuelle Prüfung empfohlen)',
                '',
            ])

        self.stdout.write('')
        self.stdout.write(f'Ergebnis: marked={marked}  flagged_recurring={len(flagged)}')
        self.stdout.write(f'Logs: {LOG_ROOT}/<YYYY-MM-DD>/NN.md')
