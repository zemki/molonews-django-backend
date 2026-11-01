"""Shared helpers for event-maintenance management commands
(convert_broad_occurrences, mark_events_past)."""

import json
from datetime import datetime
from pathlib import Path

from django.core import serializers
from django.conf import settings

from content.models import EventV4, Event_Occurrence

BACKUP_ROOT = Path(settings.BACKUP_DIR)

# Titel-Tokens die auf wiederkehrende Events hindeuten — schützen solche Events
# vor automatischer PAST-Markierung.
#
# Warum Substring-Match (nicht Wortgrenzen)? Deutsche Composita:
# "Weihnachtsmarkt", "Sonntagsspaziergang", "Stadtführung" sind ein einziges
# Wort — Wortgrenzen würden die Keywords nicht erkennen. False-Positives
# (z.B. "Messer" → "messe") werden gezielt durch _NEGATIVE_GUARDS gefiltert.
RECURRING_TITLE_KEYWORDS: tuple[str, ...] = (
    'markt', 'stammtisch', 'brunch', 'gottesdienst', 'messe',
    'spaziergang', 'rundgang', 'führung', 'tour', 'stadtführung',
    'sonntags', 'samstags', 'freitags', 'donnerstags',
    'mittwochs', 'dienstags', 'montags',
    'wöchentlich', 'monatlich', 'jährlich', 'regelmäßig',
    'treff', 'stammtreff', 'sprechstunde', 'verkauf',
    'singen', 'club', 'meeting', 'disco', 'party', 'treffen',
    'buffet', 'offene bühne', 'jam session', 'pub quiz',
)

# Wenn ein Keyword matcht, aber eines dieser Substrings steht ebenfalls drin,
# wird der Match verworfen (bekannte False-Positive-Composita).
_NEGATIVE_GUARDS: dict[str, tuple[str, ...]] = {
    'tour': ('tourette', 'tournee'),
    'messe': ('messer',),
    'party': ('partynoten',),
    'club': ('clubbing-verbot',),
}


def title_has_recurring_keyword(title: str) -> str | None:
    """Liefert das erste matchende Keyword (lower) oder None.

    Substring-Match, case-insensitive. Bewusst tolerant gegenüber Composita
    ("Weihnachtsmarkt" → "markt"). Negative Guards für bekannte False-Positives.
    """
    if not title:
        return None
    lower = title.lower()
    for kw in RECURRING_TITLE_KEYWORDS:
        if kw not in lower:
            continue
        guards = _NEGATIVE_GUARDS.get(kw)
        if guards and any(g in lower for g in guards):
            continue
        return kw
    return None


def backup_events(event_ids: list[int], dry_run: bool,
                  command_name: str = 'events') -> Path:
    """Sichert EventV4 + zugehörige Event_Occurrences als JSON
    (Django-Fixture-Format). Restore: ``python manage.py loaddata <file>``.
    """
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    events_qs = EventV4.objects.filter(id__in=event_ids)
    occs_qs = Event_Occurrence.objects.filter(event_id__in=event_ids)
    payload = {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'command': command_name,
        'dry_run': dry_run,
        'event_count': len(event_ids),
        'event_ids': sorted(event_ids),
        'events': json.loads(serializers.serialize('json', events_qs)),
        'occurrences': json.loads(serializers.serialize('json', occs_qs)),
    }
    ts = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    suffix = '_dry' if dry_run else ''
    path = BACKUP_ROOT / f'{command_name}_{ts}{suffix}.json'
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
    return path
