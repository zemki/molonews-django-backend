"""Abgleich der einfachen Event-Feeds (JSON, XML, ICS, RSS) nach dem Anlegen.

Ohne Abgleich würden die Feeds, die der Artikel-Import nebenbei einliest, nur
angelegt: Was die Quelle später änderte, absagte oder verschob, bliebe unverändert
im Bestand stehen - ein verschobener Termin als zweites Event neben dem alten.

Die Importpfade legen weiterhin selbst an. Danach übergeben sie alle gültigen
Feed-Einträge an sync_feed_events(); der Abgleich zieht geänderte Felder nach und
löscht, was der Feed nicht mehr enthält. Wiedererkannt wird wie beim Anlegen
über Titel + Startzeit.

Gelöscht wird nur, was nachweislich aus dem Feed stammt. Organisationen können
über die App eigene Events anlegen, und die landen unter derselben Quelle wie der
Feed (api/views/event_v4.py ordnet über Source.related_user zu). Solche Events
standen nie im Feed und dürfen deshalb nicht als "entfallen" gelten. Der Abgleich
merkt sich dazu je Quelle, welche Events er im Feed gesehen hat
(import_data/feed_state/events_<Quelle>.json).
"""

import json
import os
from datetime import timezone as dt_timezone
from logging import getLogger

from django.conf import settings
from django.utils import timezone
from django.utils.timezone import localtime

from content.models import EventV4, Event_Occurrence
from content.import_tools.source_health import clear_source_error, delete_guard_blocks

logger = getLogger("molonews")

# Felder, die am bestehenden Event nachgezogen werden, wenn der Feed-Eintrag sie
# mitbringt. Ein Pfad liefert nur die Felder, die er auch beim Anlegen aus dem
# Feed füllt - alles andere (z.B. ein Ort, der aus der Organisation stammt)
# bleibt unangetastet.
SYNC_FIELDS = ("content", "event_location", "image_url", "link", "street", "zip_code", "town")
# Endzeiten weichen durch Rundung um Sekunden ab; erst darüber ist es eine Änderung.
END_TOLERANCE_SECONDS = 60

# import_data/ steht in .gitignore und wird von keinem Importer aufgeräumt.
FEED_STATE_DIR = os.path.join(str(settings.BASE_DIR), "import_data", "feed_state")


def _state_key(title, start):
    """Titel + Startzeit als JSON-taugliche Zeichenkette (Startzeit in UTC)."""
    return f"{start.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}|{title}"


def _state_path(source):
    return os.path.join(FEED_STATE_DIR, f"events_{source.id}.json")


def load_seen_keys(source):
    """Events, die frühere Läufe im Feed dieser Quelle gesehen haben.

    Returns:
        Menge von Schlüsseln (siehe _state_key) oder None, wenn es noch keinen
        Stand gibt - dann wird in diesem Lauf nichts gelöscht.
    """
    try:
        with open(_state_path(source), encoding="utf-8") as f:
            return set(json.load(f)["seen"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.error(f"Feed-Stand der Quelle {source.id} nicht lesbar ({e}) - in diesem Lauf wird nichts gelöscht")
        return None


def save_seen_keys(source, keys):
    """Stand atomar schreiben (erst temporäre Datei, dann umbenennen).

    Die temporäre Datei trägt die Prozess-ID, damit sich ein Cron-Lauf und ein
    gleichzeitiger Handlauf (--source-id) nicht gegenseitig hineinschreiben. Das
    JSON ist reines ASCII: so scheitert das Schreiben nicht an Titeln mit
    kaputten Zeichen. Ein Fehler hier darf den Abgleich nicht abbrechen.
    """
    try:
        os.makedirs(FEED_STATE_DIR, exist_ok=True)
        path = _state_path(source)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="ascii") as f:
            json.dump({"source": source.id, "saved": localtime().isoformat(), "seen": sorted(keys)}, f)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.error(f"Feed-Stand der Quelle {source.id} nicht speicherbar: {e}")


def _stored_value(field, value):
    """Feed-Wert so, wie ihn das Modellfeld speichern würde (None -> '', Länge)."""
    if value is None:
        value = ""
    max_length = EventV4._meta.get_field(field).max_length
    if max_length and isinstance(value, str) and len(value) > max_length:
        value = value[:max_length]
    return value


def _apply_feed_changes(event, item):
    """Geänderte Feed-Felder am Event nachziehen. Returns True bei einer Änderung."""
    changed = []
    for field in SYNC_FIELDS:
        if item.get(field) is None:
            continue
        new_value = _stored_value(field, item[field])
        if (getattr(event, field) or "") != new_value:
            setattr(event, field, new_value)
            changed.append(field)

    end_changed = False
    new_end = item.get("end")
    if new_end is not None:
        occurrences = list(event.occurrences.all())
        # Nur der eine Termin, den der Importpfad selbst angelegt hat
        if len(occurrences) == 1 and occurrences[0].end_datetime is not None:
            difference = abs((occurrences[0].end_datetime - new_end).total_seconds())
            if difference > END_TOLERANCE_SECONDS:
                occurrences[0].end_datetime = new_end
                occurrences[0].save(update_fields=["end_datetime"])
                end_changed = True

    if not changed and not end_changed:
        return False

    event.moddate = localtime()
    event.save(update_fields=changed + ["moddate", "updated_at"])
    logger.debug(
        f"Event '{event.title}' (ID {event.id}) aus dem Feed aktualisiert: "
        f"{', '.join(changed + (['Endzeit'] if end_changed else []))}"
    )
    return True


def sync_feed_events(source, feed_events, protected_titles=()):
    """Bestand einer Quelle mit dem gerade gelesenen Feed abgleichen.

    Args:
        source: Quelle des Feeds
        feed_events: ein dict je gültigem Feed-Eintrag mit mindestens
            "title" und "start" (zeitzonenbewusst), dazu optional "end" und die
            Felder aus SYNC_FIELDS - genau so, wie der Pfad sie beim Anlegen speichert
        protected_titles: Titel von Feed-Einträgen ohne lesbaren Termin. Events mit
            diesen Titeln stehen noch im Feed, lassen sich aber nicht über die
            Startzeit zuordnen - sie werden weder geändert noch gelöscht.

    Returns:
        (aktualisiert, gelöscht)
    """
    now = timezone.now()
    feed_by_key = {}
    for item in feed_events:
        start = item.get("start")
        if start is None:
            continue
        # Feeds ohne Zeitzonenangabe liefern naive Zeiten; die Datenbank speichert
        # sie in der Projekt-Zeitzone - genauso wird hier verglichen.
        if timezone.is_naive(start):
            start = timezone.make_aware(start)
            item = {**item, "start": start}
        end = item.get("end")
        if end is not None and timezone.is_naive(end):
            item = {**item, "end": timezone.make_aware(end)}
        feed_by_key.setdefault((item["title"], start), item)
    future_feed_keys = {key for key in feed_by_key if key[1] > now}

    db_events = list(
        EventV4.objects.filter(source=source, is_past=False, start_date__gt=now)
        .prefetch_related("occurrences")
    )

    # Manche Feeds liefern nur ein Fenster (z.B. die nächsten 1000 Termine). Was hinter dem letzten Feed-Termin liegt - oder genau auf ihm,
    # wo die Liste abgeschnitten sein kann -, kann im Feed fehlen, ohne entfallen
    # zu sein. Preis dieser Vorsicht: Fällt der zeitlich letzte Termin einer
    # Quelle weg, bleibt er stehen, bis er vorbei ist oder ein späterer dazukommt.
    window_end = max((start for _title, start in future_feed_keys), default=None)

    seen_before = load_seen_keys(source)
    protected_titles = set(protected_titles)
    updated = 0
    matched = 0
    stale = []
    for event in db_events:
        key = (event.title, event.start_date)
        item = feed_by_key.get(key)
        if item is not None:
            matched += 1
            try:
                if _apply_feed_changes(event, item):
                    updated += 1
            except Exception as e:
                logger.error(f"Event '{event.title}' (ID {event.id}) nicht aktualisierbar: {e}")
            continue
        if window_end is not None and event.start_date >= window_end:
            continue
        if event.title in protected_titles:
            continue
        # Nur was ein früherer Lauf im Feed gesehen hat, kann daraus entfallen sein
        if seen_before is not None and _state_key(*key) in seen_before:
            stale.append(event)

    # Der Löschschutz vergleicht den Feed mit dem, was aus dem Feed stammt: den
    # wiedergefundenen Events und denen, die jetzt entfielen. In der App angelegte
    # Events zählen nicht mit - sonst blockierten sie bei einer Quelle mit kleinem
    # Feed jeden Lauf.
    deleted_ids = set()
    blocked = delete_guard_blocks(source, matched + len(stale), len(future_feed_keys))
    if not blocked:
        for event in stale:
            try:
                event_id = event.id
                Event_Occurrence.objects.filter(event=event).delete()
                event.delete()
                deleted_ids.add(event_id)
            except Exception as e:
                logger.error(f"Event '{event.title}' (ID {event.id}) nicht löschbar: {e}")

    # Stand fortschreiben. Nach einem unverdächtigen Lauf: was der Feed jetzt
    # liefert, dazu früher Gesehenes, das noch im Bestand steht (hinter dem
    # Feed-Fenster oder ohne lesbaren Termin im Feed). Nach einem blockierten Lauf
    # wird nur ergänzt, nie entfernt - sonst ginge mit einem abgeschnittenen Abruf
    # das Wissen verloren, was aus dem Feed stammt.
    if future_feed_keys:
        current = {_state_key(title, start) for title, start in future_feed_keys}
        if blocked:
            new_seen = current | seen_before if seen_before is not None else None
        else:
            still_there = {
                _state_key(event.title, event.start_date)
                for event in db_events
                if event.id not in deleted_ids
            }
            new_seen = current | (still_there & (seen_before or set()))
        if new_seen is not None and new_seen != seen_before:
            save_seen_keys(source, new_seen)

    # Lauf ohne Befund: einen früheren Fehlervermerk an der Quelle zurücknehmen.
    # Dafür genügt, dass der Feed überhaupt Einträge geliefert hat - auch ein Feed
    # mit nur vergangenen Terminen ist gesund.
    if not blocked and feed_by_key:
        clear_source_error(source)

    deleted = len(deleted_ids)
    if updated or deleted:
        logger.info(
            f"Feed-Abgleich {source.name}: {updated} Events aktualisiert, "
            f"{deleted} nicht mehr gelieferte gelöscht"
        )
    return updated, deleted
