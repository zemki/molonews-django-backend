from django.core.management.base import BaseCommand
from content.models import EventV4

class Command(BaseCommand):
    help = "Füllt das Feld 'event_location' aus 'street' und/oder 'town', falls es leer ist."

    def handle(self, *args, **options):
        updated = 0
        total = 0
        for event in EventV4.objects.all():
            total += 1
            # Nur, wenn event_location leer oder None ist
            if not event.event_location or event.event_location.strip() == '':
                street = (event.street or '').strip()
                town = (event.town or '').strip()
                # Wenn beide leer, mach nichts
                if not street and not town:
                    continue
                # Beide vorhanden
                elif street and town:
                    new_location = f"{street}, {town}"
                # Nur street
                elif street:
                    new_location = street
                # Nur town
                elif town:
                    new_location = town
                else:
                    new_location = None

                # Speichern, nur wenn wir was gebaut haben
                if new_location:
                    event.event_location = new_location
                    event.save()
                    updated += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"Updated Event {event.id}: {new_location}"
                    ))

        self.stdout.write(self.style.SUCCESS(
            f"Fertig! {updated} von {total} Events wurden aktualisiert."
        ))
