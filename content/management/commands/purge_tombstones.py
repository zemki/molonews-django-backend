"""Alte Löschmarker (content.DeletedContent) entfernen.

Die Grabsteine versorgen den Delta-Abruf
GET /api/v4/organizations/content/?changed_since=... mit gelöschten IDs. Sie
werden nur so lange gebraucht, wie ein Client plausibel offline gewesen sein
kann - danach fordert die Route ohnehin einen Vollsync an (full_sync_required).

Empfohlen als täglicher Cronjob:
    python manage.py purge_tombstones
"""

from logging import getLogger

from django.core.management.base import BaseCommand
from django.utils.timezone import localtime
from datetime import timedelta

from content.models import DeletedContent, TOMBSTONE_RETENTION_DAYS

logger = getLogger("molonews")


class Command(BaseCommand):
    help = "Entfernt Löschmarker, die älter als die Aufbewahrungsfrist sind."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=TOMBSTONE_RETENTION_DAYS,
            help="Aufbewahrungsfrist in Tagen (Default: {})".format(
                TOMBSTONE_RETENTION_DAYS
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur anzeigen, wie viele Marker entfernt würden.",
        )

    def handle(self, *args, **options):
        cutoff = localtime() - timedelta(days=options["days"])
        queryset = DeletedContent.objects.filter(deleted_at__lt=cutoff)
        count = queryset.count()

        if options["dry_run"]:
            self.stdout.write(
                "{} Löschmarker älter als {} würden entfernt.".format(count, cutoff)
            )
            return

        queryset.delete()
        message = "{} Löschmarker älter als {} entfernt.".format(count, cutoff)
        logger.info(message)
        self.stdout.write(self.style.SUCCESS(message))
