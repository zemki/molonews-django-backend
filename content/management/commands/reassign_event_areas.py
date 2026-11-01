from django.core.management.base import BaseCommand
from content.models import EventV4
from content.utils.area_creation import _is_area_near, reassign_area_by_coords


class Command(BaseCommand):
    help = (
        "Korrigiert Area-Zuordnungen von Events anhand ihrer Koordinaten. "
        "Entfernt Areas, die weiter als --max-km vom Event entfernt liegen, "
        "und ergänzt passende Areas per get_or_create_area."
    )

    def add_arguments(self, parser):
        parser.add_argument("--max-km", type=int, default=50,
                            help="Abstandsschwelle in km (Default: 50)")
        parser.add_argument("--include-past", action="store_true",
                            help="Auch vergangene Events prüfen")
        parser.add_argument("--dry-run", action="store_true",
                            help="Nur Zählen, nichts ändern")

    def handle(self, *args, **opts):
        max_km = opts["max_km"]
        include_past = opts["include_past"]
        dry = opts["dry_run"]

        qs = EventV4.objects.filter(
            published=True,
            latitude__isnull=False,
            longitude__isnull=False,
        ).prefetch_related("area")
        if not include_past:
            qs = qs.filter(is_past=False)

        touched = 0
        scanned = 0
        for ev in qs.iterator(chunk_size=2000):
            scanned += 1
            if not any(not _is_area_near(a, ev.latitude, ev.longitude, max_km) for a in ev.area.all()):
                continue

            if dry:
                touched += 1
                self.stdout.write(
                    f"[dry] ev {ev.id} town={ev.town!r} areas={[a.id for a in ev.area.all()]}"
                )
                continue

            try:
                if reassign_area_by_coords(ev, max_distance_km=max_km):
                    touched += 1
                    self.stdout.write(
                        f"ev {ev.id} town={ev.town!r} korrigiert -> areas={[a.id for a in ev.area.all()]}"
                    )
            except Exception as e:
                self.stderr.write(f"ev {ev.id}: reassign Fehler: {e}")

        self.stdout.write(self.style.SUCCESS(
            f"Fertig. Scanned={scanned}, touched={touched} (max_km={max_km}, dry_run={dry})"
        ))
