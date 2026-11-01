import requests
from django.core.management.base import BaseCommand
from content.import_tools.url_safety import safe_get
from content.models import Source
from logging import getLogger

logger = getLogger("molonews")

TIMEOUT = 15


class Command(BaseCommand):
    help = "Prüft alle aktiven Quellen ob deren URLs erreichbar sind"

    def add_arguments(self, parser):
        parser.add_argument(
            "--type",
            type=str,
            default=None,
            help="Nur Quellen eines bestimmten Typs prüfen (z.B. rss, ics, json)",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Auch inaktive Quellen prüfen",
        )

    def handle(self, *args, **options):
        qs = Source.objects.all() if options["all"] else Source.objects.filter(active=True)

        if options["type"]:
            qs = qs.filter(type=options["type"])

        qs = qs.order_by("id")

        sources = list(qs)
        total = len(sources)
        self.stdout.write(f"\n{'='*80}")
        self.stdout.write(f"  Prüfe {total} {'alle' if options['all'] else 'aktive'} Quellen ...")
        self.stdout.write(f"{'='*80}\n")

        broken = []
        no_link = []
        ok_count = 0

        for i, source in enumerate(sources, 1):
            url = source.link
            label = f"[{i}/{total}] ID={source.id} | {source.name}"

            if not url:
                no_link.append(source)
                self.stdout.write(self.style.WARNING(f"  {label} — KEIN LINK gesetzt"))
                continue

            try:
                # safe_get sperrt interne Adressen, auch nach Weiterleitungen (SSRF)
                resp = safe_get(url, timeout=TIMEOUT, headers={
                    "User-Agent": "molo link-checker/1.0",
                })
                code = resp.status_code
                if code >= 400:
                    broken.append({
                        "source": source,
                        "status": f"HTTP {code}",
                        "detail": url,
                    })
                    self.stdout.write(self.style.ERROR(f"  {label} — HTTP {code} — {url}"))
                else:
                    ok_count += 1
                    self.stdout.write(self.style.SUCCESS(f"  {label} — OK ({code})"))
            except ValueError as e:
                broken.append({
                    "source": source,
                    "status": "GESPERRT",
                    "detail": f"{e} — {url}",
                })
                self.stdout.write(self.style.ERROR(f"  {label} — GESPERRT: {e}"))
            except requests.ConnectionError:
                broken.append({
                    "source": source,
                    "status": "CONNECTION ERROR",
                    "detail": url,
                })
                self.stdout.write(self.style.ERROR(f"  {label} — CONNECTION ERROR — {url}"))
            except requests.Timeout:
                broken.append({
                    "source": source,
                    "status": "TIMEOUT",
                    "detail": f"Keine Antwort nach {TIMEOUT}s — {url}",
                })
                self.stdout.write(self.style.ERROR(f"  {label} — TIMEOUT — {url}"))
            except requests.RequestException as e:
                broken.append({
                    "source": source,
                    "status": "FEHLER",
                    "detail": f"{e} — {url}",
                })
                self.stdout.write(self.style.ERROR(f"  {label} — FEHLER: {e}"))

        # Zusammenfassung
        self.stdout.write(f"\n{'='*80}")
        self.stdout.write(f"  ERGEBNIS: {ok_count} OK, {len(broken)} fehlerhafte Links, {len(no_link)} ohne Link (von {total} geprüft)")
        self.stdout.write(f"{'='*80}\n")

        if broken:
            self.stdout.write(self.style.ERROR(f"  {len(broken)} QUELLEN MIT FEHLERHAFTEN LINKS:\n"))
            for item in broken:
                s = item["source"]
                self.stdout.write(
                    f"  ID={s.id}  {item['status']}  Typ={s.type}  Aktiv={'ja' if s.active else 'nein'}  {s.name}  {s.link}"
                )
            self.stdout.write("")
        else:
            self.stdout.write(self.style.SUCCESS("  Alle Quellen mit Links sind erreichbar!\n"))
