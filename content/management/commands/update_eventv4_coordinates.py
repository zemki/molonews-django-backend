from django.conf import settings
import re
import time
from django.core.management.base import BaseCommand
from django.db.models import Q
from content.models import EventV4
from content.utils.area_creation import reassign_area_by_coords
from geopy.geocoders import Nominatim

class Command(BaseCommand):
    help = "Prüft EventV4 auf fehlende Koordinaten und ergänzt diese per Geocoding (max 1 Request/sec)."

    def handle(self, *args, **options):
        geolocator = Nominatim(user_agent=settings.GEOCODER_USER_AGENT, timeout=10)

        # Also retry events that previously failed due to missing street/zip/town
        # but have an event_location that may contain usable address info
        events = EventV4.objects.filter(
            latitude__isnull=True,
            longitude__isnull=True,
        ).filter(
            Q(geocoding_failed=False) |
            Q(geocoding_failed=True, event_location__gt="")
        )

        processed = 0
        updated = 0

        for event in events:
            processed += 1
            address = self._build_address(event)

            if not address:
                if not event.geocoding_failed:
                    event.geocoding_failed = True
                    event.save(update_fields=["geocoding_failed"])
                self.stdout.write(self.style.NOTICE(
                    f"{event.id}: '{event.title}' hat keine auswertbare Adresse, übersprungen."
                ))
                continue

            try:
                location = geolocator.geocode(address)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Geocoding-Fehler bei '{event.title}': {e}"))
                event.geocoding_failed = True
                event.save(update_fields=["geocoding_failed"])
                continue

            if location:
                event.latitude = location.latitude
                event.longitude = location.longitude
                event.geocoding_failed = False
                event.save(update_fields=["latitude", "longitude", "geocoding_failed"])
                updated += 1
                self.stdout.write(self.style.SUCCESS(
                    f"{event.id}: '{event.title}' [{address}] -> ({location.latitude}, {location.longitude})"
                ))
                # Area anhand der neuen Koord prüfen und ggf. korrigieren
                try:
                    if reassign_area_by_coords(event):
                        self.stdout.write(self.style.SUCCESS(
                            f"{event.id}: Area-Zuordnung korrigiert"
                        ))
                except Exception as e:
                    self.stdout.write(self.style.WARNING(
                        f"{event.id}: Area-Reassign fehlgeschlagen: {e}"
                    ))
            else:
                event.geocoding_failed = True
                event.save(update_fields=["geocoding_failed"])
                self.stdout.write(self.style.WARNING(
                    f"{event.id}: '{event.title}' Keine Koordinaten für '{address}' gefunden."
                ))
            time.sleep(2)

        self.stdout.write(self.style.SUCCESS(f"Fertig! {processed} Events geprüft, {updated} Koordinaten ergänzt."))

    def _build_address(self, event):
        # Primary: use structured address fields
        if event.street or event.town or event.zip_code:
            return f"{event.street}, {event.zip_code} {event.town}, Deutschland".strip(", ")

        # Fallback: use event_location, strip leading "Veranstaltungsort" prefix
        if event.event_location:
            loc = re.sub(r"^Veranstaltungsort", "", event.event_location).strip()
            if loc:
                return loc

        return None
