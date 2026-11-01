import json
import os
import requests
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from datetime import datetime, timedelta
from django.conf import settings
from content.utils.mail import send_html_mail
from django.db.models import Count, Max, Min
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken
from logging import getLogger

from content.models import Area, Article, DeletedContent, EventV4, Source, Tag
from molo import llm
from content.import_tools.source_health import parse_source_error

logger = getLogger("molonews")

# Benutzer, mit dem die API-Schreibprüfung (Event anlegen/löschen) läuft.
# Ohne MONITORING_USER_ID in den Settings wird diese Prüfung übersprungen.
MONITORING_USER_ID = getattr(settings, "MONITORING_USER_ID", None)
MONITORING_DEVICE_ID = "monitoring-check"

# Schwellwerte der Pipeline-Checks.
# Die Werte orientieren sich an den gemessenen Lücken im Normalbetrieb
# (Import: max. ~2 h, Warteschlangen: wenige Stunden) und liegen bewusst darüber,
# damit normale Nacht- und Wochenendflauten keinen Alarm auslösen.
IMPORT_MAX_AGE_HOURS = 6
AREA_QUEUE_MAX_AGE_HOURS = 12
AREA_QUEUE_MAX_SIZE = 2000
# Das KI-Tagging wird über den Fortschritt geprüft, nicht über das Alter der
# Warteschlange: ein Massen-Import mit zehntausenden Events lässt den ältesten
# Eintrag zwangsläufig tagelang warten, obwohl der Worker durchgehend arbeitet.
TAGGING_STALL_MAX_HOURS = 12
# Events muessen weiter hereinkommen; taeglich laufen mehrere Event-Importe.
EVENT_IMPORT_MAX_AGE_HOURS = 48

TAGGING_BACKLOG_MAX_ARTICLES = 2000
TAGGING_BACKLOG_MAX_EVENTS = 50000

# Massenlöschung: Die Event-Importer löschen, was im Feed fehlt. Liefert ein
# Scraper einmal eine leere Liste, verschwinden alle kommenden Events einer
# Quelle, obwohl insgesamt weiter Events hereinkommen. Geprüft wird deshalb je Organisation der
# Netto-Verlust: Bestand vor 24 h (heutiger Bestand minus Neuanlagen plus
# Löschungen) gegen den heutigen. Löschen und Neuanlegen derselben Events
# (Umstellung eines Importers) zählt so nicht als Verlust.
MASS_DELETE_WINDOW_HOURS = 24
MASS_DELETE_MIN_EVENTS = 50
MASS_DELETE_MAX_LOSS = 0.5

# Ausfälle einzelner Quellen: Die Importer vermerken Störungen an der Quelle
# (Source.import_errors, siehe import_tools/source_health.py). Alarm gibt es nur
# für Quellen, die bis vor kurzem geliefert haben und seit einigen Stunden
# gestört sind - nicht für kurze Aussetzer und nicht dauerhaft für Quellen, die
# längst eingeschlafen sind (die bleiben im Wert des Checks aufgeführt).
SOURCE_FAILURE_MIN_HOURS = 6
SOURCE_FAILURE_ALERT_DAYS = 3
SOURCE_ALIVE_DAYS = 30

# Ein anhaltender Ausfall soll nicht stündlich mailen: erneut nur, wenn sich die
# Menge der fehlgeschlagenen Checks ändert oder so viele Stunden vergangen sind.
ALERT_REPEAT_HOURS = 24
MONITORING_RESULT_FILE = os.path.join(settings.LOG_DIR, "monitoring.json")
ALERT_STATE_FILE = os.path.join(settings.LOG_DIR, "monitoring_alert_state.json")
# Merkt sich zwischen zwei Läufen, wann die Zähler zuletzt gestiegen sind.
MONITORING_STATE_FILE = os.path.join(settings.LOG_DIR, "monitoring_state.json")


class Command(BaseCommand):
    help = "Monitoring service that checks API availability for events"

    def _generate_token(self):
        """Generate a fresh JWT token for the monitoring user."""
        User = get_user_model()
        user = User.objects.get(id=MONITORING_USER_ID)
        token = AccessToken.for_user(user)
        token.set_exp(lifetime=timedelta(days=7))
        return str(token)

    def _pipeline_checks(self):
        """Prüft die Import- und KI-Pipeline anhand von Frische und Rückstau.

        Jeder Check läuft gekapselt: ein Fehler in einem Check macht ihn rot,
        stoppt aber weder die übrigen Checks noch den Mailversand.
        """
        now = timezone.now()
        checks = {}
        state = self._read_state()
        tagging_state = state.setdefault("tagging", {})

        def run(key, label, func):
            entry = {"label": label, "success": None, "value": None, "threshold": None, "error": None}
            try:
                func(entry)
            except Exception as e:
                entry["success"] = False
                entry["error"] = f"Check konnte nicht ausgeführt werden: {e}"
                logger.error(f"Monitoring: Pipeline-Check '{key}' fehlgeschlagen: {e}")
            checks[key] = entry

        def age_hours(timestamp):
            return (now - timestamp).total_seconds() / 3600

        def article_import(entry):
            """Kommen überhaupt noch Artikel herein?"""
            entry["threshold"] = f"jünger als {IMPORT_MAX_AGE_HOURS} h"
            latest = Article.objects.aggregate(d=Max("created_at"))["d"]
            if latest is None:
                entry["success"] = False
                entry["value"] = "kein einziger Artikel in der Datenbank"
                return
            age = age_hours(latest)
            entry["value"] = f"letzter Artikel vor {age:.1f} h"
            entry["success"] = age < IMPORT_MAX_AGE_HOURS

        def area_assignment(entry):
            """Wartet der älteste Artikel ohne Gebiet zu lange?

            Deckt den Ausfall des externen Gebiets-Workers ab: sobald der steht,
            wächst das Alter des ältesten Artikels in der Warteschlange monoton.
            """
            entry["threshold"] = f"jünger als {AREA_QUEUE_MAX_AGE_HOURS} h"
            oldest = self._area_queue().aggregate(d=Min("created_at"))["d"]
            if oldest is None:
                entry["value"] = "Warteschlange leer"
                entry["success"] = True
                return
            age = age_hours(oldest)
            entry["value"] = f"ältester Artikel ohne Gebiet wartet seit {age:.1f} h"
            entry["success"] = age < AREA_QUEUE_MAX_AGE_HOURS

        def area_backlog(entry):
            """Staut sich die Warteschlange der Gebietszuweisung auf?"""
            entry["threshold"] = f"höchstens {AREA_QUEUE_MAX_SIZE} Artikel"
            count = self._area_queue().count()
            entry["value"] = f"{count} Artikel ohne Gebietszuweisung"
            entry["success"] = count <= AREA_QUEUE_MAX_SIZE

        def ai_tagging(entry):
            """Kommt das KI-Tagging noch voran?

            Gemessen wird der Fortschritt, nicht das Alter der Warteschlange:
            der Zähler der fertig getaggten Objekte steigt ausschließlich durch
            den Worker. Steht er, obwohl etwas zu tun wäre, ist der Worker weg.
            """
            entry["threshold"] = f"Fortschritt in den letzten {TAGGING_STALL_MAX_HOURS} h"

            targets = (
                ("articles", "Artikel", self._tagging_queue_articles(), Article),
                ("events", "Events", self._tagging_queue_events(), EventV4),
            )

            parts = []
            ok = True
            for key, label, queue, model in targets:
                queue_empty = not queue.exists()
                done = model.objects.filter(ai_tagged=True).count()
                since = self._tagging_progress(key, done, queue_empty, tagging_state, now)
                if queue_empty:
                    parts.append(f"{label}: Warteschlange leer")
                    continue
                age = age_hours(since)
                parts.append(f"{label}: letzter Fortschritt vor {age:.1f} h")
                ok = ok and age < TAGGING_STALL_MAX_HOURS

            entry["value"] = ", ".join(parts)
            entry["success"] = ok

        def tagging_backlog(entry):
            """Wächst die Warteschlange über das hinaus, was der Worker schafft?"""
            entry["threshold"] = (
                f"höchstens {TAGGING_BACKLOG_MAX_ARTICLES} Artikel "
                f"und {TAGGING_BACKLOG_MAX_EVENTS} Events"
            )
            articles = self._tagging_queue_articles().count()
            events = self._tagging_queue_events().count()
            entry["value"] = f"{articles} Artikel, {events} Events ohne KI-Tags"
            entry["success"] = (
                articles <= TAGGING_BACKLOG_MAX_ARTICLES
                and events <= TAGGING_BACKLOG_MAX_EVENTS
            )

        def event_import(entry):
            """Kommen ueberhaupt noch Events herein?"""
            entry["threshold"] = f"juenger als {EVENT_IMPORT_MAX_AGE_HOURS} h"
            latest = EventV4.objects.aggregate(d=Max("created_at"))["d"]
            if latest is None:
                entry["success"] = False
                entry["value"] = "kein einziges Event in der Datenbank"
                return
            age = age_hours(latest)
            entry["value"] = f"letztes Event vor {age:.1f} h"
            entry["success"] = age < EVENT_IMPORT_MAX_AGE_HOURS

        def event_mass_delete(entry):
            """Hat eine Organisation binnen eines Tages den Großteil ihrer Events verloren?"""
            entry["threshold"] = (
                f"keine Organisation verliert in {MASS_DELETE_WINDOW_HOURS} h mehr als "
                f"{int(MASS_DELETE_MAX_LOSS * 100)} % ihrer kommenden Events"
            )
            since = now - timedelta(hours=MASS_DELETE_WINDOW_HOURS)
            deleted_per_org = (
                DeletedContent.objects.filter(content_type="event", deleted_at__gte=since)
                .values("organization_id", "organization__name")
                .annotate(n=Count("id"))
                .filter(n__gte=MASS_DELETE_MIN_EVENTS)
            )
            auffaellig = []
            for row in deleted_per_org:
                if row["organization_id"] is None:
                    continue
                events = EventV4.objects.filter(
                    source__organization_id=row["organization_id"], is_past=False
                )
                present = events.count()
                created = events.filter(created_at__gte=since).count()
                before = present - created + row["n"]
                if before >= MASS_DELETE_MIN_EVENTS and present < before * (1 - MASS_DELETE_MAX_LOSS):
                    auffaellig.append(
                        f"{row['organization__name']}: {before} -> {present} kommende Events "
                        f"({row['n']} gelöscht, {created} neu)"
                    )

            if auffaellig:
                entry["value"] = "; ".join(auffaellig)
                entry["success"] = False
            else:
                entry["value"] = "kein auffälliger Verlust"
                entry["success"] = True

        def source_failures(entry):
            """Sind einzelne Quellen seit Stunden gestört, die bisher geliefert haben?"""
            entry["threshold"] = (
                f"keine Quelle, die in den letzten {SOURCE_ALIVE_DAYS} Tagen geliefert hat, "
                f"länger als {SOURCE_FAILURE_MIN_HOURS} h gestört"
            )
            alive_since = now - timedelta(days=SOURCE_ALIVE_DAYS)
            neu, bekannt = [], 0
            gestoert = Source.objects.exclude(import_errors="").exclude(import_errors__isnull=True)
            for source in gestoert:
                # Abgeschaltete Quellen fasst kein Importer mehr an, ihr letzter
                # Fehlervermerk bleibt einfach stehen. Ausnahme ist der Typ ai-event:
                # Uploads und Datei-Importe per Cron laufen unabhängig vom Schalter.
                if not source.active and source.type != "ai-event":
                    continue
                since, message = parse_source_error(source.import_errors)
                if since is None:
                    # Statusmeldung ohne Zeitpunkt (z.B. Script-Generierung im Admin)
                    bekannt += 1
                    continue
                stunden = age_hours(since)
                frisch_gestoert = (
                    SOURCE_FAILURE_MIN_HOURS <= stunden <= SOURCE_FAILURE_ALERT_DAYS * 24
                )
                lieferte_zuletzt = (
                    Article.objects.filter(source=source, created_at__gte=alive_since).exists()
                    or EventV4.objects.filter(source=source, created_at__gte=alive_since).exists()
                )
                if frisch_gestoert and lieferte_zuletzt:
                    neu.append(f"{source.name} (seit {stunden:.0f} h: {message[:90]})")
                else:
                    bekannt += 1

            zusatz = f"{bekannt} weitere Quellen mit Fehlerstatus (kurzzeitig, alt oder eingeschlafen)"
            if neu:
                entry["value"] = "; ".join(neu) + f" - dazu {zusatz}"
                entry["success"] = False
            else:
                entry["value"] = f"keine neue Störung; {zusatz}"
                entry["success"] = True

        def llm_server(entry):
            """Ist der lokale Sprachmodell-Server (Ollama) erreichbar?"""
            entry["threshold"] = "Ollama antwortet unter LLM_BASE_URL"
            ok = llm.is_available()
            entry["value"] = f"{llm.base_url()} ({llm.default_model()}) " + ("erreichbar" if ok else "nicht erreichbar")
            entry["success"] = ok

        run("article_import", "Artikel-Import", article_import)
        run("event_import", "Event-Import", event_import)
        run("event_mass_delete", "Massenlöschung von Events", event_mass_delete)
        run("source_failures", "Ausfälle einzelner Quellen", source_failures)
        run("area_assignment", "Gebietszuweisung", area_assignment)
        run("area_backlog", "Rückstau Gebietszuweisung", area_backlog)
        run("ai_tagging", "KI-Tagging", ai_tagging)
        run("tagging_backlog", "Rückstau KI-Tagging", tagging_backlog)
        if getattr(settings, "USE_RAG_EVENT_SEARCH", False):
            run("llm_server", "Sprachmodell (KI-Suche)", llm_server)

        self._write_state(state)

        return checks

    def _area_queue(self):
        """Artikel, die auf eine KI-Gebietszuweisung warten.

        Gleiche Kriterien wie /api/v4/ai-area/articles/get-unassigned-batch/,
        damit der Check genau das misst, was der Worker abarbeiten würde.
        """
        return Article.objects.filter(
            source__auto_detect_area_from_content=True,
            area_defined=False,
            published=True,
            date__gte=timezone.now() - timedelta(days=30),
        )

    def _tagging_queue_articles(self):
        """Artikel, die auf KI-Tags warten.

        Gleiche Kriterien wie /api/v4/ai-tagging/articles/get-untagged-batch/.
        """
        return Article.objects.filter(
            ai_tagged=False,
            published=True,
            date__gte=timezone.now() - timedelta(days=30),
        )

    def _tagging_queue_events(self):
        """Events, die auf KI-Tags warten.

        Gleiche Kriterien wie /api/v4/ai-tagging/events/get-untagged-batch/ am
        Tag; nachts nimmt der Worker zusätzlich vergangene Events mit.
        """
        return EventV4.objects.filter(
            ai_tagged=False,
            published=True,
            is_past=False,
        ).exclude(source__type="local")

    def _tagging_progress(self, key, done, queue_empty, tagging_state, now):
        """Seit wann steht der Zähler der getaggten Objekte still?

        Gibt den Zeitpunkt des letzten Fortschritts zurück und schreibt den
        aktuellen Stand nach tagging_state. Eine leere Warteschlange gilt als
        Fortschritt, damit ein neuer Import nicht sofort mit der Stillstandszeit
        aus der Leerlaufphase in den Alarm läuft. Sinkt der Zähler (gelöschte
        Objekte), wird der neue Stand zur Basis für den nächsten Vergleich.
        """
        previous = tagging_state.get(key) or {}
        last_count = previous.get("count")
        since = None
        if not queue_empty and isinstance(last_count, int) and done <= last_count:
            try:
                since = datetime.fromisoformat(previous.get("since", ""))
            except (TypeError, ValueError):
                since = None
            if since is not None and timezone.is_naive(since):
                since = None
        if since is None:
            since = now
        tagging_state[key] = {"count": done, "since": since.isoformat()}
        return since

    def _read_state(self):
        try:
            with open(MONITORING_STATE_FILE) as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _write_state(self, state):
        try:
            with open(MONITORING_STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except OSError as e:
            logger.error(f"Monitoring: Status konnte nicht gespeichert werden: {e}")

    def _should_send_alert(self, failed):
        """Entprellt den Alarm, damit ein Dauerausfall nicht stündlich mailt."""
        signature = ",".join(sorted(failed))
        state = {}
        try:
            with open(ALERT_STATE_FILE) as f:
                state = json.load(f)
        except (OSError, ValueError):
            pass

        if state.get("signature") != signature:
            return True, signature

        last_sent = state.get("last_sent")
        if not last_sent:
            return True, signature
        try:
            age = datetime.utcnow() - datetime.fromisoformat(last_sent)
        except ValueError:
            return True, signature
        return age >= timedelta(hours=ALERT_REPEAT_HOURS), signature

    def _remember_alert(self, signature):
        try:
            with open(ALERT_STATE_FILE, "w") as f:
                json.dump({"signature": signature, "last_sent": datetime.utcnow().isoformat()}, f)
        except OSError as e:
            logger.error(f"Monitoring: Alarm-Status konnte nicht gespeichert werden: {e}")

    def _clear_alert_state(self):
        try:
            os.remove(ALERT_STATE_FILE)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.error(f"Monitoring: Alarm-Status konnte nicht zurückgesetzt werden: {e}")

    def handle(self, *args, **options):
        results = {
            "timestamp": datetime.utcnow().isoformat(),
            "last_update": None,
            "check_events": {"success": None, "status": None, "error": None},
            "create_event": {"success": None, "status": None, "error": None},
            "delete_event": {"success": None, "status": None, "error": None},
            "pipeline": {},
        }

        api_base = settings.SERVER_URL.rstrip("/") + "/api/v4"
        headers_common = {"accept": "application/json"}

        # 1. Öffentliche Event-Liste abrufen
        try:
            resp = requests.get(
                f"{api_base}/users/list_future_events_by_category/?category_id=1",
                headers={**headers_common, "X-Device-ID": MONITORING_DEVICE_ID},
                timeout=10,
            )
            results["check_events"]["success"] = resp.status_code == 200
            results["check_events"]["status"] = resp.status_code
            if not results["check_events"]["success"]:
                results["check_events"]["error"] = resp.text[:500]
        except Exception as e:
            results["check_events"]["success"] = False
            results["check_events"]["error"] = str(e)

        # 2./3. Event anlegen und wieder löschen (nur mit MONITORING_USER_ID)
        if MONITORING_USER_ID:
            self._check_write_api(api_base, headers_common, results)
        else:
            for key in ("create_event", "delete_event"):
                results[key]["error"] = "Übersprungen (MONITORING_USER_ID nicht gesetzt)"

        # 4. Import- und KI-Pipeline prüfen
        results["pipeline"] = self._pipeline_checks()

        # 5. Ergebnis für die Statusseite speichern
        results["last_update"] = datetime.utcnow().isoformat()
        with open(MONITORING_RESULT_FILE, "w") as f:
            json.dump(results, f, indent=2)
        self.stdout.write(self.style.SUCCESS(f"Monitoring-Ergebnis gespeichert: {MONITORING_RESULT_FILE}"))

        # 6. Bei Fehlern Alarm-Mail senden
        failed = []
        for key in ("check_events", "create_event", "delete_event"):
            if results[key]["success"] is False:
                failed.append(key)
        for key, check in results["pipeline"].items():
            if check["success"] is not True:
                failed.append(check["label"])

        if not failed:
            self._clear_alert_state()
            return

        should_send, signature = self._should_send_alert(failed)
        if not should_send:
            logger.info(f"Monitoring: Alarm unterdrückt (unverändert seit < {ALERT_REPEAT_HOURS} h): {signature}")
            return

        receiver_email = settings.MAIL_SENDING.get("ERROR_NOTIFICATION_EMAIL")
        if not receiver_email or not settings.MAIL_SENDING.get("EMAIL_HOST"):
            logger.warning(f"Monitoring: Fehler ohne Mail-Empfänger: {', '.join(failed)}")
            return

        try:
            sender_email = settings.MAIL_SENDING["DEFAULT_FROM_EMAIL"]
            message = (
                "<html><body>"
                "<h3>Monitoring Report</h3>"
                f"<p>Fehlgeschlagen: {', '.join(failed)}</p>"
                f"<pre>{json.dumps(results, indent=2)}</pre>"
                "</body></html>"
            )
            send_html_mail(f"Monitoring-Alarm: {', '.join(failed)}", message, receiver_email, sender_email)

            self._remember_alert(signature)
            logger.error(f"Monitoring-Alarm gesendet: {', '.join(failed)}")
        except Exception as e:
            logger.error(f"Monitoring-Mail konnte nicht gesendet werden: {e}")

    def _check_write_api(self, api_base, headers_common, results):
        """Legt über die API ein Test-Event an und löscht es wieder."""
        auth_header = {"Authorization": self._generate_token()}
        today = timezone.now().date().isoformat()
        event_data = {
            "title": "Monitoring-Test",
            "content": "Automatischer Test, wird sofort wieder gelöscht.",
            "link": "",
            "date": today,
            "street": "",
            "town": "",
            "zip_code": "",
            "image_url": "",
            "event_location": "",
            "longitude": 0,
            "latitude": 0,
            "image_source": "",
            "occurrences": [
                {"event_start_date": f"{today}T12:00:00Z", "event_end_date": f"{today}T13:00:00Z"}
            ],
            "area": list(Area.objects.order_by("id").values_list("id", flat=True)[:1]),
            "event_tags": list(Tag.objects.filter(type="event").order_by("id").values_list("id", flat=True)[:1]),
        }

        event_id = None
        try:
            resp = requests.post(
                f"{api_base}/events/", headers={**headers_common, **auth_header}, json=event_data, timeout=10,
            )
            results["create_event"]["status"] = resp.status_code
            if resp.status_code in (200, 201):
                results["create_event"]["success"] = True
                event_id = resp.json().get("id")
            else:
                results["create_event"]["success"] = False
                results["create_event"]["error"] = resp.text[:500]
        except Exception as e:
            results["create_event"]["success"] = False
            results["create_event"]["error"] = str(e)

        if not event_id:
            results["delete_event"]["error"] = "Übersprungen (kein Event angelegt)"
            return
        try:
            resp = requests.delete(
                f"{api_base}/events/{event_id}/", headers={**headers_common, **auth_header}, timeout=10,
            )
            results["delete_event"]["status"] = resp.status_code
            results["delete_event"]["success"] = resp.status_code in (200, 204)
            if not results["delete_event"]["success"]:
                results["delete_event"]["error"] = resp.text[:500]
        except Exception as e:
            results["delete_event"]["success"] = False
            results["delete_event"]["error"] = str(e)
