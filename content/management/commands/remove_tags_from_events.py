from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from content.models import EventV4, Source  # Passe die Import-Pfade an deine App-Struktur an
import logging
logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Setzt das ai_tagged Flag auf False und entfernt alle Tags für Events einer bestimmten Quelle'

    def add_arguments(self, parser):
        parser.add_argument(
            'source_id',
            type=int,
            help='ID der Quelle, deren Events zurückgesetzt werden sollen'
        )
        parser.add_argument(
            '--source-name',
            type=str,
            help='Name der Quelle (Alternative zur ID)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Zeigt nur an, was gemacht würde, ohne Änderungen durchzuführen'
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Bestätigung erforderlich für die Ausführung'
        )

    def handle(self, *args, **options):
        source_id = options.get('source_id')
        source_name = options.get('source_name')
        dry_run = options.get('dry_run')
        confirm = options.get('confirm')

        self.stdout.write(f'Starte Command mit source_id={source_id}, source_name={source_name}, dry_run={dry_run}, confirm={confirm}')

        try:
            # Quelle finden
            if source_name:
                try:
                    source = Source.objects.get(name=source_name)
                    source_id = source.id
                    self.stdout.write(f'Gefundene Quelle: {source.name} (ID: {source_id})')
                except Source.DoesNotExist:
                    raise CommandError(f'Quelle mit Namen "{source_name}" nicht gefunden')
            elif source_id:
                try:
                    source = Source.objects.get(id=source_id)
                    self.stdout.write(f'Gefundene Quelle: {source.name} (ID: {source_id})')
                except Source.DoesNotExist:
                    raise CommandError(f'Quelle mit ID {source_id} nicht gefunden')
            else:
                raise CommandError('Entweder source_id oder --source-name muss angegeben werden')

            # Events der Quelle finden
            events = EventV4.objects.filter(source_id=source_id)
            
            if not events.exists():
                self.stdout.write(
                    self.style.WARNING(f'Keine Events für Quelle "{source.name}" (ID: {source_id}) gefunden')
                )
                return

            events_count = events.count()
            ai_tagged_count = events.filter(ai_tagged=True).count()

            # Informationen anzeigen
            self.stdout.write(f'Quelle: {source.name} (ID: {source_id})')
            self.stdout.write(f'Gefundene Events: {events_count}')
            self.stdout.write(f'Events mit ai_tagged=True: {ai_tagged_count}')

            if dry_run:
                self.stdout.write(self.style.WARNING('DRY RUN - Keine Änderungen werden durchgeführt'))
                
                # Zeige Details der betroffenen Events
                for event in events.filter(ai_tagged=True)[:10]:  # Nur erste 10 anzeigen
                    tags_info = f" (Tags: {', '.join([tag.name for tag in event.tags.all()])})" if hasattr(event, 'tags') else ""
                    self.stdout.write(f'  - Event ID {event.id}: {event.title[:50]}...{tags_info}')
                
                if ai_tagged_count > 10:
                    self.stdout.write(f'  ... und {ai_tagged_count - 10} weitere Events')
                
                return

            # Bestätigung einholen
            if not confirm:
                response = input(
                    f'Sind Sie sicher, dass Sie {ai_tagged_count} Events zurücksetzen möchten? '
                    f'Dies wird das ai_tagged Flag auf False setzen und alle Tags entfernen. (yes/no): '
                )
                if response.lower() not in ['yes', 'y', 'ja', 'j']:
                    self.stdout.write(self.style.WARNING('Vorgang abgebrochen'))
                    return

            # Änderungen durchführen
            with transaction.atomic():
                updated_count = 0
                
                for event in events:
                    changes_made = False
                    
                    # ai_tagged Flag zurücksetzen
                    if event.ai_tagged:
                        event.ai_tagged = False
                        changes_made = True
                    
                    # Tags entfernen (falls das Event Tags hat)
                    if hasattr(event, 'tags') and event.tags.exists():
                        event.tags.clear()
                        changes_made = True
                    
                    if changes_made:
                        event.save()
                        updated_count += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f'Erfolgreich {updated_count} Events für Quelle "{source.name}" zurückgesetzt'
                )
            )

        except Exception as e:
            raise CommandError(f'Fehler beim Ausführen des Commands: {str(e)}')
