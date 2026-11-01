import csv
from datetime import datetime
from typing import List

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from content.models import EventV4, Tag
from molo import llm

SYSTEM_PROMPT = """Du bist ein Experte für Event-Kategorisierung. Analysiere das gegebene Event und wähle passende Tags aus der verfügbaren Tag-Liste aus.

WICHTIGE Regeln:
1. Wähle nur Tags aus der verfügbaren Liste aus
2. Wähle MINDESTENS 1 und maximal 5 relevante Tags pro Event - NIEMALS 0 Tags!
3. Berücksichtige Titel, Inhalt und Veranstaltungsort
4. Bevorzuge spezifische vor allgemeinen Tags
5. Falls keine perfekten Tags verfügbar sind, wähle die am besten passenden aus
6. Begründe die Auswahl in einem kurzen Satz im Feld reasoning"""


def tagging_schema(available_tags: List[str]) -> dict:
    """JSON-Schema der Modellantwort; die Tag-Namen sind auf die vorhandenen Tags beschränkt."""
    return {
        "type": "object",
        "properties": {
            "recommended_tags": {
                "type": "array",
                "items": {"type": "string", "enum": available_tags},
                "minItems": 1,
                "maxItems": 5,
            },
            "reasoning": {"type": "string"},
        },
        "required": ["recommended_tags", "reasoning"],
        "additionalProperties": False,
    }


class Command(BaseCommand):
    help = 'Automatisches Tagging von Events mit einem lokalen Sprachmodell'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.available_tags = []
        self.csv_results = []
        self.model_name = None

    def add_arguments(self, parser):
        parser.add_argument(
            '--test',
            action='store_true',
            help='Testmodus: Verarbeitet nur wenige Events und speichert Ergebnisse in CSV',
        )
        parser.add_argument(
            '--max-events',
            type=int,
            default=20,
            help='Maximale Anzahl Events im Testmodus (Standard: 20)',
        )
        parser.add_argument(
            '--model',
            type=str,
            default=None,
            help='Modellname (Standard: LLM_MODEL aus den Settings)',
        )

    def handle(self, *args, **options):
        """Hauptmethode des Commands"""
        try:
            self.model_name = options['model'] or llm.default_model()
            if not llm.is_available():
                raise ValueError(f"Sprachmodell-Server nicht erreichbar: {llm.base_url()}")
            self.stdout.write(self.style.SUCCESS(
                f'Sprachmodell: {self.model_name} @ {llm.base_url()}'
            ))
            self.load_available_tags()

            if options['test']:
                self.run_test_mode(options['max_events'])
            else:
                self.run_production_mode()

        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Fehler beim Ausführen des Commands: {str(e)}')
            )

    def load_available_tags(self):
        """Lädt alle verfügbaren Event-Tags aus der Datenbank"""
        event_tags = Tag.objects.filter(type='event', assigned_by_rule=False).values_list('name', flat=True)
        self.available_tags = list(event_tags)
        
        self.stdout.write(
            self.style.SUCCESS(
                f'{len(self.available_tags)} Event-Tags aus der Datenbank geladen'
            )
        )
        
        # Debug: Erste 10 Tags anzeigen
        if self.available_tags:
            self.stdout.write(f'Beispiel-Tags: {self.available_tags[:10]}')

    def get_untagged_events(self, limit=None):
        """Holt ungetaggte Events aus der Datenbank"""
        queryset = EventV4.objects.filter(
            is_past=False,
            ai_tagged=False
        ).select_related('source').prefetch_related('tags')
        
        if limit:
            queryset = queryset[:limit]
            
        return queryset

    def generate_tags_for_event(self, event):
        """Generiert Tags für ein einzelnes Event mit dem Sprachmodell"""
        try:
            content = (event.content or "")[:500]
            user_prompt = f"""Event-Titel: {event.title}
Event-Inhalt: {content}...
Veranstaltungsort: {event.event_location or 'Nicht angegeben'}

Verfügbare Tags: {', '.join(self.available_tags)}"""

            parsed_json = llm.generate_json(
                SYSTEM_PROMPT,
                user_prompt,
                schema=tagging_schema(self.available_tags),
                model=self.model_name,
                temperature=0.3,
            )
            recommended_tags = [t for t in parsed_json.get("recommended_tags", []) if isinstance(t, str)]
            reasoning = parsed_json.get("reasoning")

            # Nur verfügbare Tags zurückgeben
            valid_tags = [
                tag for tag in recommended_tags
                if tag in self.available_tags
            ]
            
            # Sicherstellen, dass mindestens ein Tag vorhanden ist
            if not valid_tags:
                # Fallback 1: Source default_tags verwenden
                source_default_tags = event.source.default_tags.filter(type='event').values_list('name', flat=True)
                if source_default_tags:
                    valid_tags = list(source_default_tags)
                    reasoning = f"Source Default-Tags verwendet: {', '.join(valid_tags)}. Original reasoning: {reasoning}"
                # Fallback 2: Ersten verfügbaren Tag als letzten Notfall verwenden
                elif self.available_tags:
                    valid_tags = [self.available_tags[0]]
                    reasoning = f"Allgemeiner Fallback-Tag verwendet, da keine Source Default-Tags verfügbar. Original reasoning: {reasoning}"
                
            return valid_tags, reasoning

        except Exception as e:
            self.stdout.write(
                self.style.WARNING(
                    f'Fehler beim Tagging von Event {event.id}: {str(e)}'
                )
            )
            # Mehrfacher Fallback bei API-Fehlern
            # Fallback 1: Source default_tags verwenden
            source_default_tags = event.source.default_tags.filter(type='event').values_list('name', flat=True)
            if source_default_tags:
                return list(source_default_tags), f"API-Fehler: Source Default-Tags verwendet: {str(e)}"
            # Fallback 2: Ersten verfügbaren Tag als letzten Notfall
            elif self.available_tags:
                return [self.available_tags[0]], f"API-Fehler: Allgemeiner Fallback-Tag verwendet: {str(e)}"
            else:
                return [], f"Kritischer Fehler: Keine Tags verfügbar: {str(e)}"

    def run_test_mode(self, max_events):
        """Testmodus: Verarbeitet Events und speichert Ergebnisse in CSV"""
        self.stdout.write(
            self.style.WARNING(f'TESTMODUS: Verarbeite maximal {max_events} Events')
        )
        
        events = self.get_untagged_events(limit=max_events)
        self.csv_results = []
        
        if not events:
            self.stdout.write(
                self.style.WARNING('Keine ungetaggten Events gefunden')
            )
            return

        self.stdout.write(f'Verarbeite {len(events)} Events...')
        
        for i, event in enumerate(events, 1):
            self.stdout.write(f'Verarbeite Event {i}/{len(events)}: {event.title}')
            
            recommended_tags, reasoning = self.generate_tags_for_event(event)
            
            # Aktuelle Tags des Events
            current_tags = list(event.tags.values_list('name', flat=True))
            
            # Ergebnis für CSV sammeln
            result = {
                'event_id': event.id,
                'event_title': event.title,
                'event_location': event.event_location or '',
                'current_tags': ', '.join(current_tags),
                'recommended_tags': ', '.join(recommended_tags),
                'reasoning': reasoning or '',
                'content_preview': (event.content or '')[:100] + '...' if event.content else ''
            }
            self.csv_results.append(result)
            
            # Progress ausgeben
            self.stdout.write(
                f'  → Empfohlene Tags: {", ".join(recommended_tags) if recommended_tags else "Keine"}'
            )

        # CSV speichern
        self.save_results_to_csv()
        self.print_summary()

    def run_production_mode(self):
        """Produktionsmodus: Taggt Events und speichert in der Datenbank"""
        self.stdout.write(
            self.style.SUCCESS('PRODUKTIONSMODUS: Starte automatisches Tagging')
        )
        
        events = self.get_untagged_events()
        
        if not events:
            self.stdout.write(
                self.style.WARNING('Keine ungetaggten Events gefunden')
            )
            return

        successful_updates = 0
        failed_updates = 0
        
        self.stdout.write(f'Verarbeite {len(events)} Events...')
        
        for i, event in enumerate(events, 1):
            self.stdout.write(f'Verarbeite Event {i}/{len(events)}: {event.title}')
            
            try:
                with transaction.atomic():
                    recommended_tags, reasoning = self.generate_tags_for_event(event)
                    
                    if recommended_tags:
                        # WICHTIG: Zuerst alle bestehenden Tags entfernen (außer per Regel vergebenen)
                        event.tags.remove(*event.tags.filter(assigned_by_rule=False))
                        
                        # Dann neue Tags hinzufügen
                        tag_objects = Tag.objects.filter(
                            name__in=recommended_tags, 
                            type='event'
                        )
                        
                        event.tags.add(*tag_objects)
                        event.ai_tagged = True
                        event.save()
                        
                        successful_updates += 1
                        
                        # Ausgabe je nach verwendetem Fallback-Typ
                        if "Source Default-Tags verwendet" in reasoning:
                            self.stdout.write(
                                f'  ↻ Source Default-Tags verwendet: {", ".join(recommended_tags)}'
                            )
                        elif "Fallback-Tag verwendet" in reasoning or "Allgemeiner Fallback-Tag" in reasoning:
                            self.stdout.write(
                                f'  ⚠ Fallback-Tag verwendet: {", ".join(recommended_tags)}'
                            )
                        else:
                            self.stdout.write(
                                f'  ✓ AI-Tags zugewiesen: {", ".join(recommended_tags)}'
                            )
                    else:
                        # Das sollte mit den neuen Fallbacks nicht mehr passieren
                        self.stdout.write(
                            '  ❌ Kritischer Fehler: Keine Tags trotz aller Fallbacks!'
                        )
                        failed_updates += 1
                        
            except Exception as e:
                failed_updates += 1
                self.stdout.write(
                    self.style.ERROR(
                        f'  ✗ Fehler beim Speichern: {str(e)}'
                    )
                )

        # Zusammenfassung
        self.stdout.write(
            self.style.SUCCESS(
                f'\nFertig! {successful_updates} Events erfolgreich getaggt, '
                f'{failed_updates} Fehler'
            )
        )

    def save_results_to_csv(self):
        """Speichert Testergebnisse in CSV-Datei"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = str(settings.LOG_DIR / f'event_tagging_test_{timestamp}.csv')
        
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            if self.csv_results:
                fieldnames = self.csv_results[0].keys()
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self.csv_results)
        
        self.stdout.write(
            self.style.SUCCESS(f'Ergebnisse in {filename} gespeichert')
        )

    def print_summary(self):
        """Gibt eine Zusammenfassung der Testergebnisse aus"""
        if not self.csv_results:
            return
            
        self.stdout.write('\n' + '='*80)
        self.stdout.write('ZUSAMMENFASSUNG DER TESTERGEBNISSE')
        self.stdout.write('='*80)
        
        for i, result in enumerate(self.csv_results[:5], 1):  # Nur erste 5 anzeigen
            self.stdout.write(f'\nEvent {i}:')
            self.stdout.write(f'  Titel: {result["event_title"]}')
            self.stdout.write(f'  Aktuelle Tags: {result["current_tags"] or "Keine"}')
            self.stdout.write(f'  Empfohlene Tags: {result["recommended_tags"] or "Keine"}')
            if result["reasoning"]:
                self.stdout.write(f'  Begründung: {result["reasoning"][:100]}...')
        
        if len(self.csv_results) > 5:
            self.stdout.write(f'\n... und {len(self.csv_results) - 5} weitere Events')
        
        # Statistiken
        events_with_recommendations = sum(
            1 for r in self.csv_results if r["recommended_tags"]
        )
        
        self.stdout.write('\nSTATISTIKEN:')
        self.stdout.write(f'  Gesamte Events: {len(self.csv_results)}')
        self.stdout.write(f'  Events mit Empfehlungen: {events_with_recommendations}')
        self.stdout.write(f'  Events ohne Empfehlungen: {len(self.csv_results) - events_with_recommendations}')
        
        if events_with_recommendations < len(self.csv_results):
            self.stdout.write(
                self.style.WARNING(
                    f'  ⚠ WARNUNG: {len(self.csv_results) - events_with_recommendations} Events '
                    f'haben keine Tag-Empfehlungen erhalten!'
                )
            )