"""
Cross-Source Event Duplikat-Bereinigung

Dieser Command findet und entfernt Duplikate über alle Event-Sources hinweg unter Verwendung
der gleichen Logik wie der DuplicateHandler beim Import.

Der Vergleich läuft über die Termine (Event_Occurrence), nicht über das Startdatum:
Seit der Termin-Gruppierung hat ein Event viele Termine, während Aggregatoren
(Ticketportale, Stadtportale) pro Vorstellung ein eigenes Event anlegen. Ein Event
wird gelöscht, wenn ALLE seine Termine von einem stärkeren Event (höhere Source-
Priorität, bei Gleichstand besserer Quality-Score, dann älteres Event) abgedeckt
sind. Events mit zusätzlichen Terminen bleiben stehen.

Usage:
  # Test-Modus: Analysiert Duplikate und erstellt MD-Report
  python manage.py cleanup_event_duplicates --test

  # Delete-Modus: Löscht tatsächlich die Duplikate
  python manage.py cleanup_event_duplicates --delete

  # Nur bestimmte Tage zurück schauen (Standard: 365)
  python manage.py cleanup_event_duplicates --test --days 90

  # Auch vergangene Events prüfen (Standard: nur zukünftige)
  python manage.py cleanup_event_duplicates --test --include-past

  # Nur Gruppen, in denen das behaltene Event aus einer dieser Quellen stammt
  python manage.py cleanup_event_duplicates --test --keep-sources 74,483

  # Zusätzlich unscharfe Treffer (ähnlicher Titel); Standard sind nur exakte Treffer
  python manage.py cleanup_event_duplicates --test --fuzzy --keep-sources 74,483
"""

import os
from django.conf import settings
from collections import defaultdict
from datetime import datetime, timedelta
from logging import getLogger

import pytz
from django.core.management.base import BaseCommand
from django.utils.timezone import localtime

from content.import_tools.duplicate_handler import DuplicateHandler, normalize_string
from content.models import EventV4, Event_Occurrence

TARGET_TZ = pytz.timezone('Europe/Berlin')
logger = getLogger("molonews")


def event_rank(event):
    """Höher = wird bevorzugt: Source-Priorität, Quality-Score, älteres Event."""
    return (event.source.priority, DuplicateHandler.calculate_quality_score(event), -event.id)


class Command(BaseCommand):
    help = 'Findet und bereinigt Cross-Source Event-Duplikate (termin-basiert)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--test',
            action='store_true',
            help='Test-Modus: Erstellt nur MD-Report ohne zu löschen'
        )
        parser.add_argument(
            '--delete',
            action='store_true',
            help='Delete-Modus: Löscht tatsächlich die Duplikate'
        )
        parser.add_argument(
            '--days',
            type=int,
            default=365,
            help='Anzahl Tage in die Vergangenheit (Standard: 365)'
        )
        parser.add_argument(
            '--include-past',
            action='store_true',
            help='Auch vergangene Events prüfen (Standard: nur zukünftige)'
        )
        parser.add_argument(
            '--fuzzy',
            action='store_true',
            help='Zusätzlich Ähnlichkeitssuche (ähnlicher Titel, Zeit ±5 min, ähnlicher Ort). '
                 'Standard sind nur exakte Treffer, weil die Ähnlichkeitsregeln verschiedene '
                 'Veranstaltungen zusammenwerfen; nur mit --keep-sources und nach Reportprüfung nutzen'
        )
        parser.add_argument(
            '--keep-sources',
            type=str,
            default='',
            help='Kommaseparierte Source-IDs: nur Gruppen bearbeiten, deren behaltenes Event aus diesen Quellen stammt'
        )

    def handle(self, *args, **options):
        test_mode = options['test']
        delete_mode = options['delete']
        days_back = options['days']
        include_past = options['include_past']
        keep_sources = {int(x) for x in options['keep_sources'].split(',') if x.strip().isdigit()}
        use_fuzzy = options['fuzzy']

        if not test_mode and not delete_mode:
            self.stdout.write(self.style.ERROR('Bitte --test oder --delete angeben!'))
            return

        if test_mode and delete_mode:
            self.stdout.write(self.style.ERROR('Bitte nur --test ODER --delete angeben, nicht beides!'))
            return

        mode = "TEST-MODUS (Dry Run)" if test_mode else "DELETE-MODUS (Echtes Löschen)"
        mode += " – mit Ähnlichkeitssuche" if use_fuzzy else " – nur exakte Treffer"
        self.stdout.write(self.style.WARNING(f'\n=== {mode} ===\n'))

        cutoff_date = datetime.now() - timedelta(days=days_back)

        queryset = (
            EventV4.objects.filter(start_date__gte=cutoff_date)
            .select_related('source')
            .prefetch_related('occurrences')
            .order_by('id')
        )
        if not include_past:
            queryset = queryset.filter(is_past=False)

        events = list(queryset)
        total_events = len(events)
        self.stdout.write(f'Prüfe {total_events} Events...\n')

        # 1. Alle Termine indexieren: exakter Schlüssel und Fuzzy-Gruppe (10-Minuten-Raster + Ort)
        exact_groups = defaultdict(list)   # key -> [(event, occ_id)]
        fuzzy_groups = defaultdict(list)   # key -> [(event, occ_id, start_local)]
        occurrence_total = {}

        for event in events:
            occs = list(event.occurrences.all())
            if not occs:
                # Events ohne Termine über das Startdatum vergleichen
                occs = [Event_Occurrence(id=-event.id, event=event, start_datetime=event.start_date)]
            occurrence_total[event.id] = len(occs)
            for occ in occs:
                start_local = localtime(occ.start_datetime) if occ.start_datetime.tzinfo else TARGET_TZ.localize(occ.start_datetime)
                exact_groups[DuplicateHandler.build_event_key(event.title, start_local, event.event_location)].append(
                    (event, occ.id)
                )
                rounded = start_local.replace(minute=(start_local.minute // 10) * 10, second=0, microsecond=0)
                location_norm = normalize_string(event.event_location) if event.event_location else 'no-location'
                fuzzy_groups[f"{rounded.strftime('%Y-%m-%d %H:%M')}|{location_norm}"].append(
                    (event, occ.id, start_local)
                )

        # 2. Paarweise Treffer sammeln: covered[(x, y)] = Termine von y, die x abdeckt
        covered = defaultdict(set)
        match_types = {}
        events_by_id = {e.id: e for e in events}

        def record(a, a_occ, b, b_occ, match_type):
            if a.id == b.id:
                return
            covered[(a.id, b.id)].add(b_occ)
            covered[(b.id, a.id)].add(a_occ)
            match_types.setdefault(frozenset((a.id, b.id)), match_type)

        for members in exact_groups.values():
            if len(members) < 2:
                continue
            for i, (a, a_occ) in enumerate(members):
                for b, b_occ in members[i + 1:]:
                    record(a, a_occ, b, b_occ, 'exakt')

        tolerance = timedelta(minutes=DuplicateHandler.TIME_TOLERANCE_MINUTES)
        for members in (fuzzy_groups.values() if use_fuzzy else ()):
            if len(members) < 2:
                continue
            for i, (a, a_occ, a_start) in enumerate(members):
                for b, b_occ, b_start in members[i + 1:]:
                    if a.id == b.id or abs(a_start - b_start) > tolerance:
                        continue
                    if b_occ in covered[(a.id, b.id)]:
                        continue
                    if DuplicateHandler.are_titles_similar(a.title, b.title):
                        record(a, a_occ, b, b_occ, 'fuzzy')

        # 3. Entscheiden: y wird gelöscht, wenn x alle Termine von y abdeckt und x gewinnt
        deletions = {}  # y_id -> x_id
        for (x_id, y_id), matched in covered.items():
            if len(matched) < occurrence_total[y_id]:
                continue
            x, y = events_by_id[x_id], events_by_id[y_id]
            if event_rank(x) <= event_rank(y):
                continue
            if keep_sources and x.source_id not in keep_sources:
                continue
            if y_id not in deletions or event_rank(x) > event_rank(events_by_id[deletions[y_id]]):
                deletions[y_id] = x_id

        # Kein Event löschen, das selbst schon gelöscht wird, wenn sein Sieger ebenfalls fällt
        # (Kette x > y > z: z bleibt, nächster Lauf räumt nach)
        for y_id in list(deletions):
            if deletions[y_id] in deletions:
                del deletions[y_id]

        groups_by_keep = defaultdict(list)
        for y_id, x_id in deletions.items():
            groups_by_keep[x_id].append(events_by_id[y_id])

        duplicate_groups = []
        for x_id, delete_events in groups_by_keep.items():
            keep_event = events_by_id[x_id]
            match_type = 'exakt' if all(
                match_types.get(frozenset((x_id, d.id))) == 'exakt' for d in delete_events
            ) else 'fuzzy'
            duplicate_groups.append({
                'key': DuplicateHandler.build_event_key(
                    keep_event.title, keep_event.start_date.astimezone(TARGET_TZ), keep_event.event_location
                ),
                'keep': keep_event,
                'delete': sorted(delete_events, key=lambda e: e.id),
                'match_type': match_type,
            })
        duplicate_groups.sort(key=lambda g: g['keep'].id)
        total_to_delete = sum(len(g['delete']) for g in duplicate_groups)
        total_duplicates = total_to_delete + len(duplicate_groups)

        # 4. Report
        if duplicate_groups:
            self.stdout.write(
                self.style.WARNING(
                    f'\nGefunden: {len(duplicate_groups)} Duplikat-Gruppen '
                    f'({total_duplicates} Events total, {total_to_delete} zu löschen)\n'
                )
            )

            report_lines = [
                '# Event Duplikat-Bereinigung Report',
                f'\n**Datum:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
                f'**Modus:** {mode}',
                f'**Events geprüft:** {total_events}',
                f'**Duplikat-Gruppen gefunden:** {len(duplicate_groups)}',
                f'**Events zu löschen:** {total_to_delete}',
                '\n---\n'
            ]

            events_deleted = 0

            for i, group in enumerate(duplicate_groups, 1):
                keep_event = group['keep']
                delete_events = group['delete']
                match_type = group['match_type']

                match_emoji = '🔍' if match_type == 'fuzzy' else '✓'
                self.stdout.write(f'\n{i}. {match_emoji} Duplikat-Gruppe ({match_type.upper()}): {group["key"][:80]}...')
                self.stdout.write(
                    self.style.SUCCESS(
                        f'   KEEP: [{keep_event.source.name}] (Priorität: {keep_event.source.priority}, '
                        f'Score: {DuplicateHandler.calculate_quality_score(keep_event)}, '
                        f'Termine: {occurrence_total[keep_event.id]}) '
                        f'ID: {keep_event.id} - "{keep_event.title[:60]}"'
                    )
                )

                match_badge = '🔍 FUZZY MATCH' if match_type == 'fuzzy' else '✓ EXAKT'
                report_lines.append(f'## {i}. {keep_event.title} `[{match_badge}]`')
                report_lines.append(f'\n**Event-Key:** `{group["key"]}`')
                report_lines.append(f'**Datum:** {keep_event.start_date.astimezone(TARGET_TZ).strftime("%Y-%m-%d %H:%M")}')
                report_lines.append(f'**Location:** {keep_event.event_location or "N/A"}')
                report_lines.append('\n### ✅ Event wird BEHALTEN:')
                report_lines.append(f'- **ID:** {keep_event.id}')
                report_lines.append(f'- **Source:** {keep_event.source.name} (ID: {keep_event.source.id})')
                report_lines.append(f'- **Priorität:** {keep_event.source.priority}')
                report_lines.append(f'- **Quality Score:** {DuplicateHandler.calculate_quality_score(keep_event)}')
                report_lines.append(f'- **Termine:** {occurrence_total[keep_event.id]}')
                report_lines.append(f'- **Ticket-URL:** {keep_event.ticket_url or "Nein"}')
                report_lines.append(f'- **Bild:** {"Ja" if keep_event.image_url or keep_event.image else "Nein"}')
                report_lines.append(f'- **Content-Länge:** {len(keep_event.content or "")} Zeichen')
                report_lines.append(f'- **Link:** {keep_event.link or "N/A"}')

                report_lines.append('\n### ❌ Events werden GELÖSCHT:')

                for del_event in delete_events:
                    quality_score = DuplicateHandler.calculate_quality_score(del_event)

                    self.stdout.write(
                        self.style.ERROR(
                            f'   DELETE: [{del_event.source.name}] (Priorität: {del_event.source.priority}, '
                            f'Score: {quality_score}, Termine: {occurrence_total[del_event.id]}) '
                            f'ID: {del_event.id} - "{del_event.title[:60]}"'
                        )
                    )

                    report_lines.append(f'\n#### Event ID {del_event.id}')
                    report_lines.append(f'- **Titel:** {del_event.title}')
                    report_lines.append(f'- **Datum:** {del_event.start_date.astimezone(TARGET_TZ).strftime("%Y-%m-%d %H:%M")}')
                    report_lines.append(f'- **Source:** {del_event.source.name} (ID: {del_event.source.id})')
                    report_lines.append(f'- **Priorität:** {del_event.source.priority}')
                    report_lines.append(f'- **Quality Score:** {quality_score}')
                    report_lines.append(f'- **Termine:** {occurrence_total[del_event.id]} (alle vom behaltenen Event abgedeckt)')
                    report_lines.append(f'- **Ticket-URL:** {del_event.ticket_url or "Nein"}')
                    report_lines.append(f'- **Bild:** {"Ja" if del_event.image_url or del_event.image else "Nein"}')
                    report_lines.append(f'- **Content-Länge:** {len(del_event.content or "")} Zeichen')
                    report_lines.append(f'- **Link:** {del_event.link or "N/A"}')

                    pair_type = match_types.get(frozenset((keep_event.id, del_event.id)), 'exakt')
                    if pair_type == 'fuzzy':
                        string_sim = DuplicateHandler.calculate_title_similarity(keep_event.title, del_event.title)
                        token_sim = DuplicateHandler.calculate_token_similarity(keep_event.title, del_event.title)
                        report_lines.append(f'- **String-Ähnlichkeit:** {string_sim:.1%}')
                        report_lines.append(f'- **Token-Ähnlichkeit:** {token_sim:.1%}')

                    if del_event.source.priority < keep_event.source.priority:
                        reason = f'Niedrigere Source-Priorität ({del_event.source.priority} < {keep_event.source.priority})'
                    else:
                        keep_score = DuplicateHandler.calculate_quality_score(keep_event)
                        if quality_score < keep_score:
                            reason = f'Gleiche Priorität, niedrigerer Quality Score ({quality_score} vs {keep_score})'
                        else:
                            reason = 'Gleiche Priorität und Score, jüngeres Event'
                    if pair_type == 'fuzzy':
                        reason += ' (Fuzzy Match: ähnlicher Titel zur gleichen Zeit/Location)'
                    report_lines.append(f'- **Grund:** {reason}')

                    if delete_mode:
                        try:
                            Event_Occurrence.objects.filter(event=del_event).delete()
                            del_event.delete()
                            events_deleted += 1
                            self.stdout.write(self.style.SUCCESS(f'      ✓ Event ID {del_event.id} gelöscht'))
                        except Exception as e:
                            error_msg = f'Fehler beim Löschen von Event ID {del_event.id}: {e}'
                            self.stdout.write(self.style.ERROR(f'      ✗ {error_msg}'))
                            report_lines.append(f'- **❌ FEHLER:** {error_msg}')

                report_lines.append('\n---\n')

            report_filename = f'event_duplicates_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.md'
            report_path = os.path.join(settings.LOG_DIR, report_filename)
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(report_lines))
            self.stdout.write(self.style.SUCCESS(f'\n✓ Report gespeichert: {report_path}'))

            self.stdout.write('\n' + '=' * 80)
            self.stdout.write('ZUSAMMENFASSUNG:')
            self.stdout.write(f'  Geprüfte Events: {total_events}')
            self.stdout.write(f'  Gefundene Duplikat-Gruppen: {len(duplicate_groups)}')
            self.stdout.write(f'  Events die gelöscht werden sollen: {total_to_delete}')
            if delete_mode:
                self.stdout.write(self.style.SUCCESS(f'  ✓ Gelöschte Events: {events_deleted}'))
            else:
                self.stdout.write(self.style.WARNING('\n  ℹ️  TEST-MODUS: Keine Events wurden gelöscht.'))
                self.stdout.write('  Zum tatsächlichen Löschen führe aus:')
                self.stdout.write('  python manage.py cleanup_event_duplicates --delete')
            self.stdout.write('=' * 80)
        else:
            self.stdout.write(self.style.SUCCESS('\n✓ Keine Duplikate gefunden!'))
