# Duplikat-Erkennung mit Prioritäts-Logik

## Übersicht

Diese Dokumentation beschreibt die implementierte cross-source Duplikat-Erkennung für Event-Imports. Das System verhindert doppelte Events und entscheidet intelligent, welches Event bei Konflikten behalten wird.

---

## 1. Source-Priorität

### Neues Feld im Source-Modell

```python
# content/models.py - Source Modell
priority = models.IntegerField(
    default=50,
    verbose_name=_("priority"),
    help_text=_("100=Originalquelle, 50=Standard, 20=Aggregator")
)
```

### Prioritäts-Werte

| Wert | Bedeutung | Beispiel |
|------|-----------|----------|
| 100 | Originalquelle | Offizielle Veranstalter-Website |
| 50 | Standard (Default) | Normale Quellen |
| 20 | Aggregator | Ticket- und Veranstaltungsportale |

### Admin-Konfiguration

Das Prioritäts-Feld ist im Django Admin:
- In der Liste sichtbar
- Direkt in der Liste bearbeitbar
- Als Filter verfügbar

---

## 2. Zentrale DuplicateHandler-Klasse

**Datei:** `content/import_tools/duplicate_handler.py`

### Hauptfunktionen

#### `build_event_key(title, start_datetime, location=None)`
Erstellt einen eindeutigen Schlüssel für Event-Vergleiche.

```python
# Normales Event
"konzert abc|2024-12-20 20:00"

# Ganztages-Event (00:00 Uhr)
"stadtfest|2024-12-20|all-day"
```

#### `calculate_quality_score(event_or_dict)`
Berechnet einen Qualitäts-Score für Tiebreaker bei gleicher Priorität.

| Kriterium | Punkte |
|-----------|--------|
| Ticket-Link vorhanden | +10 |
| Bild vorhanden | +5 |
| Content-Länge | +1 pro 100 Zeichen (max +10) |

**Maximaler Score:** 25 Punkte

#### `find_cross_source_duplicate(title, start_datetime, location, exclude_source)`
Sucht nach Duplikaten über alle Sources hinweg.

#### `should_replace_existing(existing_event, new_source, new_data)`
Entscheidet ob ein bestehendes Event ersetzt werden soll.

#### `handle_duplicate(existing_event, new_source, new_data)`
Verarbeitet ein gefundenes Duplikat und gibt `'replace'` oder `'skip'` zurück.

---

## 3. Entscheidungslogik

```
Neues Event wird importiert
         │
         ▼
┌─────────────────────────────┐
│ Cross-Source Duplikat       │
│ vorhanden?                  │
└─────────────────────────────┘
         │
    Nein │        Ja
         │         │
         ▼         ▼
    ┌────────┐  ┌─────────────────────────┐
    │ Event  │  │ Prioritäten vergleichen │
    │ erstellen│ └─────────────────────────┘
    └────────┘           │
                         ▼
              ┌──────────────────────┐
              │ Neue > Bestehende?   │
              └──────────────────────┘
                    │         │
               Ja   │         │ Nein
                    ▼         ▼
              ┌──────────┐  ┌──────────────────────┐
              │ Ersetzen │  │ Neue < Bestehende?   │
              └──────────┘  └──────────────────────┘
                                  │         │
                             Ja   │         │ Nein (gleich)
                                  ▼         ▼
                            ┌──────────┐  ┌─────────────────────┐
                            │ Überspringen│ │ Quality-Score       │
                            └──────────┘  │ vergleichen          │
                                          └─────────────────────┘
                                                   │
                                                   ▼
                                          ┌──────────────────────┐
                                          │ Neuer Score > Alt?   │
                                          └──────────────────────┘
                                                │         │
                                           Ja   │         │ Nein
                                                ▼         ▼
                                          ┌──────────┐  ┌──────────┐
                                          │ Ersetzen │  │ Überspringen│
                                          └──────────┘  └──────────┘
```

---

## 4. Angepasste Import-Skripte

Alle folgenden Skripte nutzen nun die zentrale `DuplicateHandler`-Klasse:

| Skript | Pfad |
|--------|------|
| import_html_events.py | `content/management/commands/` |
| import_html_articles.py | `content/management/commands/` |
| import_events_json.py | `content/management/commands/` |

### Implementierung in den Skripten

```python
from content.import_tools.duplicate_handler import DuplicateHandler

# Vor dem Erstellen eines neuen Events:
new_event_data = {
    'ticket_url': ticket_link,
    'image_url': image,
    'content': processed_content,
}

cross_source_duplicate = DuplicateHandler.find_cross_source_duplicate(
    title, start_datetime, location, exclude_source=source
)

if cross_source_duplicate:
    action = DuplicateHandler.handle_duplicate(
        cross_source_duplicate, source, new_event_data
    )
    if action == 'skip':
        logger.info(f"Überspringe Event '{title}': Duplikat mit höherer Priorität existiert")
        continue
    # Bei 'replace' wurde das Duplikat gelöscht, neues Event wird erstellt

# Event erstellen...
```

---

## 5. Beispiele

### Beispiel 1: Höhere Priorität gewinnt

```
Bestehendes Event:
- Source: Ticketportal (Priorität 20)
- Titel: "Konzert XY"
- Datum: 2024-12-20 20:00

Neues Event:
- Source: Offizielle Website (Priorität 100)
- Titel: "Konzert XY"
- Datum: 2024-12-20 20:00

→ Ergebnis: Bestehendes wird gelöscht, neues wird erstellt
```

### Beispiel 2: Gleiche Priorität, Quality-Score entscheidet

```
Bestehendes Event:
- Source: Portal A (Priorität 50)
- Ticket-Link: Nein
- Bild: Nein
- Content: 200 Zeichen
- Score: 0 + 0 + 2 = 2

Neues Event:
- Source: Portal B (Priorität 50)
- Ticket-Link: Ja
- Bild: Ja
- Content: 500 Zeichen
- Score: 10 + 5 + 5 = 20

→ Ergebnis: Bestehendes wird gelöscht, neues wird erstellt
```

### Beispiel 3: Niedrigere Priorität wird übersprungen

```
Bestehendes Event:
- Source: Offizielle Website (Priorität 100)
- Titel: "Stadtfest"

Neues Event:
- Source: Aggregator (Priorität 20)
- Titel: "Stadtfest"

→ Ergebnis: Neues Event wird übersprungen
```

---

## 6. Migration

Die Migration `0123_add_source_priority.py` wurde erstellt und ausgeführt:
- Fügt das `priority`-Feld zum Source-Modell hinzu
- Setzt alle bestehenden Sources auf Priorität 50 (Default)

---

## 7. Dateien

| Datei | Beschreibung |
|-------|--------------|
| `content/models.py` | Source-Modell mit `priority`-Feld |
| `content/admin/source.py` | Admin-Konfiguration für Priorität |
| `content/import_tools/duplicate_handler.py` | Zentrale Duplikat-Logik |
| `content/migrations/0123_add_source_priority.py` | Migration |
