from django.core.management.base import BaseCommand
from django.utils import timezone
from content.models import EventV4

class Command(BaseCommand):
    help = "Aktualisiert das 'is_past' Flag für alle Events (True, wenn alle Occurrences vorbei sind)."

    def handle(self, *args, **options):
        now = timezone.now()
        events = EventV4.objects.all().prefetch_related('occurrences')
        updated = 0

        for event in events:
            # Gibt es noch eine Occurrence, die noch nicht vorbei ist?
            has_upcoming = event.occurrences.filter(end_datetime__gte=now).exists()
            new_flag = not has_upcoming
            if event.is_past != new_flag:
                event.is_past = new_flag
                event.save(update_fields=['is_past'])
                updated += 1
            # also save the false value 
            

        self.stdout.write(self.style.SUCCESS(f'Updated {updated} Events.'))
