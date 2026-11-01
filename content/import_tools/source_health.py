"""Fehlerstatus einzelner Quellen und Löschschutz für die Event-Importer.

Zwei Dinge, die jeder Importer braucht:

1. Ein Importfehler muss an der Quelle sichtbar werden. Tote Feeds (HTTP 404,
   HTML statt XML, Server nicht erreichbar) und Scraper ohne Ergebnis fielen
   vorher nirgends auf - der Importer übersprang sie ohne Logzeile. Der Status
   steht jetzt in Source.import_errors (Admin-Spalte "Fehler"), mit dem
   Zeitpunkt des ersten Fehlschlags vorangestellt. Das Monitoring liest ihn dort.

2. Die Event-Importer löschen alles, was im Feed nicht mehr vorkommt. Liefert
   ein Scraper wegen eines Abruffehlers eine leere oder abgeschnittene Liste,
   wäre das der gesamte Bestand der Quelle. Der Löschschutz verhindert das.
"""

import re
from datetime import datetime
from logging import getLogger

from django.utils import timezone
from django.utils.timezone import localtime

logger = getLogger("molonews")

# Ab dieser Bestandsgröße muss der Feed mindestens diesen Anteil der bekannten
# Events liefern, sonst wird nichts gelöscht. Ein Feed ganz ohne künftige Events
# löscht nie. Ändern sich nur die Schlüssel (Titel, Ort) bei gleicher Menge,
# greift der Schutz nicht - dann wird wie bisher abgeglichen.
DELETE_GUARD_MIN_EVENTS = 10
DELETE_GUARD_MIN_RATIO = 0.5

ERROR_SINCE_FORMAT = "%d.%m.%Y %H:%M"
_ERROR_PREFIX = re.compile(r"^\[seit (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})\] ")
_ERROR_MAX_LENGTH = 400


_FETCH_ERROR_LABELS = (
    ("timed out", "Zeitüberschreitung"),
    ("no route to host", "Server nicht erreichbar"),
    ("connection refused", "Verbindung abgelehnt"),
    ("name or service not known", "Domain nicht auflösbar"),
    ("temporary failure in name resolution", "Domain nicht auflösbar"),
    ("certificate verify failed", "TLS-Zertifikat ungültig"),
    ("ssl", "TLS-Fehler"),
)


def describe_fetch_error(exc):
    """Kurze, lesbare Fassung eines Abruffehlers für die Admin-Spalte "Fehler".

    Die Meldungen von requests sind mehrere hundert Zeichen lang und tragen die
    eigentliche Ursache ganz am Ende.
    """
    text = str(exc)
    lowered = text.lower()
    for needle, label in _FETCH_ERROR_LABELS:
        if needle in lowered:
            return label
    return f"{type(exc).__name__}: {text}"


def record_source_error(source, message):
    """Fehler an der Quelle vermerken.

    Der Zeitpunkt des ersten Fehlschlags bleibt stehen, solange die Quelle
    gestört ist - daran erkennt das Monitoring, wie lange der Ausfall dauert.
    Geschrieben wird nur bei einer Änderung und nur dieses eine Feld, damit ein
    Importlauf nicht bei jedem Durchgang hunderte Quellen speichert und keine
    zwischenzeitliche Änderung aus dem Admin überschreibt.
    """
    try:
        text = " ".join(str(message).split())[:_ERROR_MAX_LENGTH]
        current = source.import_errors or ""
        match = _ERROR_PREFIX.match(current)
        since = match.group(1) if match else localtime().strftime(ERROR_SINCE_FORMAT)
        new_value = f"[seit {since}] {text}"
        if new_value != current:
            source.import_errors = new_value
            source.save(update_fields=["import_errors"])
    except Exception as e:
        logger.error(f"Fehlerstatus der Quelle {getattr(source, 'id', '?')} nicht speicherbar: {e}")


def clear_source_error(source):
    """Fehlerstatus nach einem erfolgreichen Lauf zurücknehmen."""
    try:
        if source.import_errors:
            source.import_errors = ""
            source.save(update_fields=["import_errors"])
    except Exception as e:
        logger.error(f"Fehlerstatus der Quelle {getattr(source, 'id', '?')} nicht rücksetzbar: {e}")


def parse_source_error(text):
    """Zerlegt Source.import_errors in (Beginn, Meldung).

    Der Beginn ist None, wenn der Eintrag nicht von record_source_error stammt
    (z.B. Statusmeldungen der Script-Generierung im Admin).
    """
    text = text or ""
    match = _ERROR_PREFIX.match(text)
    if not match:
        return None, text
    try:
        since = timezone.make_aware(datetime.strptime(match.group(1), ERROR_SINCE_FORMAT))
    except ValueError:
        return None, text
    return since, text[match.end():]


def upcoming_count(events, now=None):
    """Wie viele dieser Bestands-Events würde ein gesunder Feed noch liefern?

    Viele Feeds führen Termine nicht mehr, die schon begonnen haben (z. B.
    Scraper von Seiten, die nur Kommendes zeigen). Im Bestand
    gilt ein Event aber bis zu seinem Ende als nicht vergangen. Für den
    Löschschutz zählt deshalb nur, was noch einen Termin in der Zukunft hat -
    sonst stünde einem laufenden mehrtägigen Event ein "leerer" Feed gegenüber
    und der Schutz schlüge bei einer gesunden Quelle an.

    Sind die Termine eines Events vorgeladen (prefetch_related("occurrences")),
    zählt der späteste Terminbeginn, sonst start_date.
    """
    now = now or timezone.now()
    count = 0
    for event in events:
        occurrences = getattr(event, "_prefetched_objects_cache", {}).get("occurrences")
        starts = [o.start_datetime for o in occurrences if o.start_datetime] if occurrences else []
        latest_start = max(starts) if starts else event.start_date
        if latest_start and latest_start > now:
            count += 1
    return count


def delete_guard_blocks(source, db_count, feed_count, allow_mass_delete=False):
    """Soll der Löschabgleich dieses Laufs ausbleiben?

    Die Funktion vermerkt nur den Fehlerfall. Den Fehlerstatus zurückzunehmen ist
    Sache des Aufrufers am Ende eines Laufs ohne Befund (clear_source_error):
    Nähme sie ihn hier zurück, bekäme ein später im selben Lauf festgestellter
    Fehler bei jedem Lauf einen neuen Beginn, und das Monitoring sähe nie eine
    Störung, die länger als einen Lauf dauert.

    Args:
        source: Quelle des Imports
        db_count: Events der Quelle im Bestand, die der Feed noch liefern müsste
            (siehe upcoming_count)
        feed_count: Events, die der Feed dieses Laufs geliefert hat
        allow_mass_delete: Schutz bewusst aufheben (Option --allow-mass-delete),
            z.B. wenn eine Quelle absichtlich verkleinert wurde

    Returns:
        True, wenn nichts gelöscht werden darf. Der Vorfall steht dann als
        Fehler im Log und an der Quelle.
    """
    suspicious = bool(db_count) and (
        feed_count == 0
        or (db_count >= DELETE_GUARD_MIN_EVENTS and feed_count < db_count * DELETE_GUARD_MIN_RATIO)
    )
    if not suspicious:
        return False

    if allow_mass_delete:
        logger.warning(
            f"Löschschutz für '{source.name}' (ID {source.id}) aufgehoben: Feed liefert "
            f"{feed_count} künftige Events zu {db_count} im Bestand, es wird trotzdem abgeglichen"
        )
        return False

    message = (
        f"Kein Löschabgleich: Feed liefert nur {feed_count} künftige Events zu "
        f"{db_count} im Bestand - vermutlich Abruffehler oder unvollständige Daten"
    )
    logger.error(f"{message} (Quelle '{source.name}', ID {source.id}); der Bestand bleibt erhalten")
    record_source_error(source, message)
    return True
