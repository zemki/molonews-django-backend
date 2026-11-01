from django.core.management.base import BaseCommand, CommandError
from content.models import EventV4, Source

class Command(BaseCommand):
    help = 'Löscht alle Events für eine bestimmte Quell-ID (Source ID)'

    def add_arguments(self, parser):
        parser.add_argument('source_id', type=int, help='ID der Quelle (Source), deren Events gelöscht werden sollen.')

    def handle(self, *args, **options):
        source_id = options['source_id']
        try:
            source = Source.objects.get(pk=source_id)
        except Source.DoesNotExist:
            raise CommandError(f"Source mit ID {source_id} existiert nicht.")

        events = EventV4.objects.filter(source=source)
        total = events.count()
        if total == 0:
            self.stdout.write(self.style.WARNING(f"Keine Events für Source-ID {source_id} gefunden."))
            return

        confirm = input(f"Sicher, dass du {total} Events von Quelle '{source}' (ID: {source_id}) löschen willst? [j/N]: ")
        if confirm.lower() != "j":
            self.stdout.write(self.style.NOTICE("Abbruch."))
            return

        events.delete()
        self.stdout.write(self.style.SUCCESS(f"{total} Events für Source-ID {source_id} erfolgreich gelöscht."))
