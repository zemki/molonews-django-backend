"""Buchführung der Feed-Importer über ihre eigenen Events (Modell ImportRecord).

Ein Importer ändert und löscht nur Events, die er selbst angelegt hat. Was in der
App oder im Admin in seiner Quelle angelegt wurde, bleibt unangetastet, und Termine,
die dort von Hand eingetragen sind, lässt er aus dem Feed weg.

Bei eigenen Events merkt er sich je Feld den zuletzt importierten Stand. Weicht der
aktuelle Wert davon ab, hat jemand das Feld von Hand geändert: Es kommt in
manual_fields und wird vom Feed nicht mehr überschrieben. Felder, die Cronjobs pflegen
(event_location, Koordinaten, start_date), sowie Tags und Subtags gehören nicht dazu.
"""
import hashlib
import re
from datetime import timedelta

from django.utils.timezone import localtime, now

from content.import_tools.duplicate_handler import DuplicateHandler, normalize_string
from content.import_tools.import_utils import effective_location
from content.models import EventV4, Event_Occurrence, ImportRecord

TRACKED_FIELDS = ("content", "image_url", "street", "zip_code", "town", "link", "ticket_url")
OCCURRENCES = "occurrences"
# Ersteinrichtung ohne Buchführung: Ein Event, das nicht im Feed steht, gilt als von
# Hand angelegt, wenn es ein App-Bild hat oder einzeln angelegt wurde - ein Import
# legt seine Events im Schub an.
BATCH_WINDOW = timedelta(minutes=5)
APP_UPLOAD_MARK = "/media/appuploads/"


def value_hash(value):
    return hashlib.sha1(normalize_string(value).encode("utf-8")).hexdigest()[:16]


def _minute(dt):
    return localtime(dt).strftime("%Y-%m-%dT%H:%M")


def occurrence_state(pairs):
    """[(start, end)] -> sortierte Liste "start|end" (minutengenau, Ortszeit)."""
    return sorted(f"{_minute(s)}|{_minute(e)}" for s, e in pairs)


def current_occurrences(event):
    """Termine frisch aus der DB (der Prefetch-Cache ist nach einem Import veraltet)."""
    return list(Event_Occurrence.objects.filter(event_id=event.id).values_list("start_datetime", "end_datetime"))


def snapshot(event):
    """Aktueller Stand der verfolgten Felder eines Events."""
    state = {field: value_hash(getattr(event, field)) for field in TRACKED_FIELDS}
    state[OCCURRENCES] = occurrence_state(current_occurrences(event))
    return state


def _upcoming(entries, t_now):
    cutoff = _minute(t_now)
    return {entry for entry in entries if entry.split("|")[0] >= cutoff}


def manually_changed_fields(record, event, t_now=None):
    """Felder, die jemand seit dem letzten Import von Hand geändert hat (inkl. früherer)."""
    t_now = t_now or now()
    changed = set(record.manual_fields or [])
    state = record.field_state or {}
    for field in TRACKED_FIELDS:
        if field in state and value_hash(getattr(event, field)) != state[field]:
            changed.add(field)
    # Termine nur ab jetzt vergleichen: vergangene fallen beim Import ohnehin weg
    if OCCURRENCES in state and (
            _upcoming(occurrence_state(current_occurrences(event)), t_now) != _upcoming(state[OCCURRENCES], t_now)):
        changed.add(OCCURRENCES)
    return changed


def create_record(event, source, key):
    return ImportRecord.objects.create(event=event, source=source, key=key[:500],
                                       field_state=snapshot(event), manual_fields=[])


def remember(record, event, key, manual_fields):
    """Stand nach dem Import festhalten; von Hand geänderte Felder behalten ihren alten Stand."""
    manual = set(manual_fields)
    state = dict(record.field_state or {})
    for field, value in snapshot(event).items():
        if field not in manual:
            state[field] = value
    record.key = key[:500]
    record.field_state = state
    record.manual_fields = sorted(manual)
    record.save()


def looks_manual(event):
    """Erstlauf/Ersteinrichtung: sieht ein Event ohne Feed-Gegenstück von Hand angelegt aus?

    Ja bei App-Bild oder wenn es einzeln angelegt wurde. Ein Import legt seine Events im
    Schub an; Reste davon (z. B. nach einer Löschsperre stehen geblieben) gehören dem
    Importer und werden beim nächsten Lauf wie bisher gelöscht.
    """
    if APP_UPLOAD_MARK in (event.image_url or ""):
        return True
    created = event.created_at
    if not created:
        return False
    return not EventV4.objects.filter(
        source_id=event.source_id,
        created_at__range=(created - BATCH_WINDOW, created + BATCH_WINDOW),
    ).exclude(id=event.id).exists()


def _plain_title(title):
    return " ".join(re.sub(r"[^\w]+", " ", normalize_string(title)).split())


def titles_match(title, other_title):
    """Titelvergleich für Termine derselben Quelle, Zeit und Ort.

    Großzügiger als der Fremd-Dubletten-Abgleich: Satzzeichen zählen nicht, und ein
    kürzerer Titel ab 5 Zeichen darf im längeren stecken ("Scrooge!" in
    "Scrooge - Die Weihnachtsgeschichte"). Vertretbar, weil Quelle, Termin und Ort
    ohnehin übereinstimmen müssen.
    """
    if DuplicateHandler.are_titles_similar(title, other_title):
        return True
    plain, other = _plain_title(title), _plain_title(other_title)
    short, long_ = sorted((plain, other), key=len)
    return len(short) >= 5 and re.search(rf"(?<!\w){re.escape(short)}(?!\w)", long_) is not None


def manual_occurrences(manual_events):
    """Termine der von Hand angelegten Events: [(titel, ort, start in Ortszeit, event_id)]."""
    result = []
    for event in manual_events:
        location = effective_location(event.event_location, event.street, event.town)
        for start, _ in current_occurrences(event):
            result.append((event.title, location, localtime(start), event.id))
    return result


def covered_by_manual(title, location, start, manual_occ):
    """ID des von Hand angelegten Events, das diesen Feed-Termin schon enthält, sonst None."""
    tolerance = timedelta(minutes=DuplicateHandler.TIME_TOLERANCE_MINUTES)
    start = localtime(start)
    for m_title, m_location, m_start, m_id in manual_occ:
        if (abs(m_start - start) <= tolerance
                and DuplicateHandler.locations_match(location, m_location)
                and titles_match(title, m_title)):
            return m_id
    return None
