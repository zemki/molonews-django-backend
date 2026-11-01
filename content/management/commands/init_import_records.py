"""Buchführung der Datei-Importe (ImportRecord) aus den zuletzt importierten Dateien aufbauen.

Nur nötig für Quellen, die schon vor Einführung der Buchführung importiert wurden:
Der Bestand einer Quelle entspricht ihrem letzten Import plus dem, was seitdem in
der App oder im Admin angelegt wurde. Was zur zuletzt importierten Datei passt,
gehört dem Importer; der Rest gilt als von Hand angelegt und wird vom Import
künftig nicht mehr angefasst.

Zuordnung Datei -> Quelle:
- automatisch aus den Upload-Logs in IMPORT_DATA_DIR/uploads/
  ("Loading events from ... (ID: N)", geschrieben von import_events_json)
- oder explizit: --file <pfad.json> --source <id>

Quellen, die schon Einträge haben, werden übersprungen. Für Quellen ohne Einrichtung
greift beim nächsten Import die Erstlauf-Regel in import_scraped_events.

Aufruf: manage.py init_import_records [--dry-run] [--source N] [--file PFAD]
"""
import glob
import json
import os
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from content.management.commands.import_events_json import init_import_records
from content.models import ImportRecord, Source

LOG_LINE = re.compile(r'Loading events from "([^"]+)" for source "[^"]*" \(ID: (\d+)\)')


def feed_files():
    """{source_id: Pfad der zuletzt importierten Datei} aus den Upload-Logs."""
    mapping = {}
    upload_dir = os.path.join(str(settings.IMPORT_DATA_DIR), "uploads")
    for log_path in sorted(glob.glob(os.path.join(upload_dir, "import_*.log")),
                           key=os.path.getmtime):
        last = None
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                match = LOG_LINE.search(line)
                if match:
                    last = match
        if last:
            mapping[int(last.group(2))] = last.group(1)
    return mapping


class Command(BaseCommand):
    help = "Import-Buchführung (ImportRecord) aus den zuletzt importierten Feed-Dateien aufbauen"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--source", type=int, help="Nur diese Quelle bearbeiten")
        parser.add_argument("--file", help="JSON-Datei des letzten Imports (erfordert --source)")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        if options.get("file"):
            if not options.get("source"):
                raise CommandError("--file erfordert --source")
            mapping = {options["source"]: options["file"]}
        else:
            mapping = feed_files()
            if options.get("source"):
                mapping = {k: v for k, v in mapping.items() if k == options["source"]}
        total_owned = total_manual = 0
        for source_id, path in sorted(mapping.items()):
            source = Source.objects.filter(id=source_id).first()
            if source is None or not os.path.exists(path):
                self.stdout.write(f"{source_id}: übersprungen (Quelle oder Datei fehlt: {path})")
                continue
            if ImportRecord.objects.filter(source=source).exists():
                self.stdout.write(f"{source_id} {source.name}: hat bereits eine Buchführung, übersprungen")
                continue
            try:
                data = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError) as e:
                self.stdout.write(f"{source_id} {source.name}: Datei unlesbar ({e})")
                continue
            items = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
            owned, manual = init_import_records(items, source, dry_run=dry)
            total_owned += len(owned)
            total_manual += len(manual)
            note = ", ".join(f"{e.id} '{e.title[:40]}'" for e in manual[:5])
            self.stdout.write(
                f"{source_id} {source.name[:35]}: {len(owned)} eigene, {len(manual)} von Hand angelegt"
                + (f" ({note}{' …' if len(manual) > 5 else ''})" if manual else "")
            )
        self.stdout.write(f"{'PROBELAUF – ' if dry else ''}{len(mapping)} Quellen, {total_owned} eigene Events, "
                          f"{total_manual} von Hand angelegte")
