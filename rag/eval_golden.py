"""
Golden-Set-Evaluation für die KI-Event-Suche.

Testet den LLM-Parser (und optional die komplette Suche) gegen typische
Anfragen mit erwarteten Parse-Ergebnissen. Vor jeder Prompt- oder
Modelländerung laufen lassen, um Regressionen zu erkennen.

Verwendung (pro Lauf ~20 Aufrufe an das lokale Sprachmodell):
    python rag/eval_golden.py             # nur Parser testen
    python rag/eval_golden.py --search    # komplette Suche inkl. DB-Query

Exit-Code 0 wenn alle Checks bestehen, sonst 1.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'molo.settings.dev')

import django
django.setup()

from molo import llm


def _kw_contains(params, substring):
    """True wenn ein content_keyword oder eine Variante den Substring enthält."""
    for kw in (params.content_keywords or []):
        if substring in kw.lower():
            return True
    for group in (params.keyword_variants or []):
        for variant in group:
            if substring in variant.lower():
                return True
    return False


def _has_synonyms(params):
    """True wenn mindestens eine Variantengruppe mehr als 2 Einträge hat (Singular+Plural+Synonym)."""
    return any(len(group) > 2 for group in (params.keyword_variants or []))


def _tag_request_contains(params, substring):
    return any(substring in t.lower() for t in (params.tag_request or []))


def _same_day(params):
    return (params.start_date and params.end_date
            and params.start_date.date() == params.end_date.date()
            or params.start_date and params.end_date
            and (params.end_date - params.start_date).total_seconds() <= 86400)


# Jeder Fall: (Query, {Check-Name: Prädikat über EventQueryParams})
GOLDEN_CASES = [
    ("Punk Konzerte in Hamburg", {
        "area Hamburg": lambda p: "Hamburg" in (p.areas or []),
        "keyword punk": lambda p: _kw_contains(p, "punk"),
        "tag_request musik (Oberkategorie)": lambda p: _tag_request_contains(p, "musik"),
        "keine not_event_search": lambda p: not p.not_event_search,
    }),
    ("Technokonzert in Lübeck", {
        "area Lübeck": lambda p: "Lübeck" in (p.areas or []),
        "keyword techno": lambda p: _kw_contains(p, "techno"),
        "tag_request musik (Oberkategorie)": lambda p: _tag_request_contains(p, "musik"),
        "Synonyme in Varianten": lambda p: _has_synonyms(p),
    }),
    ("flohmärkte in bremen", {
        "area Bremen": lambda p: "Bremen" in (p.areas or []),
        "keyword flohmarkt": lambda p: _kw_contains(p, "flohmarkt"),
        "Synonyme in Varianten": lambda p: _has_synonyms(p),
    }),
    ("Konzerte in Bremen am Wochenende", {
        "area Bremen": lambda p: "Bremen" in (p.areas or []),
        "tag_request musik": lambda p: _tag_request_contains(p, "musik"),
        "Zeitraum gesetzt": lambda p: p.start_date and p.end_date,
    }),
    ("was geht in bremen am sonntag um 14:00", {
        "area Bremen": lambda p: "Bremen" in (p.areas or []),
        "enges Zeitfenster (max 4h)": lambda p: (
            p.start_date and p.end_date
            and (p.end_date - p.start_date).total_seconds() <= 4 * 3600
        ),
    }),
    ("Musik heute Abend", {
        "tag_request musik": lambda p: _tag_request_contains(p, "musik"),
        "Start am Abend": lambda p: p.start_date and p.start_date.hour >= 14,  # UTC (Berlin -2h)
        "gleicher Tag": lambda p: _same_day(p),
    }),
    ("Was geht in Greifswald heute", {
        "area Greifswald": lambda p: "Greifswald" in (p.areas or []),
        "gleicher Tag": lambda p: _same_day(p),
    }),
    ("Märkte in Lübeck", {
        "area Lübeck": lambda p: "Lübeck" in (p.areas or []),
        "Thema erkannt (tag_request, keyword oder exakter Tag)": lambda p: (
            _tag_request_contains(p, "markt") or _tag_request_contains(p, "märkte")
            or _kw_contains(p, "markt") or _kw_contains(p, "märkte")
            or any("markt" in t.lower() or "märkte" in t.lower() for t in (p.tags or []))
        ),
    }),
    ("Theater in Lübeck", {
        "area Lübeck": lambda p: "Lübeck" in (p.areas or []),
        "tag_request theater": lambda p: _tag_request_contains(p, "theater"),
    }),
    ("mit meiner Oma etwas in Bremen unternehmen", {
        "area Bremen": lambda p: "Bremen" in (p.areas or []),
        "companion elderly": lambda p: p.companion_context == "elderly",
        "intent contextual": lambda p: p.query_intent == "contextual",
    }),
    ("Ich will heute auf eine Party gehen was ist in Bremen los?", {
        "area Bremen": lambda p: "Bremen" in (p.areas or []),
        "Thema party": lambda p: (
            _tag_request_contains(p, "party") or _kw_contains(p, "party")
            or any("party" in t.lower() for t in (p.tags or []))
        ),
        "gleicher Tag": lambda p: _same_day(p),
    }),
    ("I am looking for a calm event that I can go, free of charge, with 2 people, something arsty. Berlin.", {
        "area Berlin": lambda p: "Berlin" in (p.areas or []),
        "als Event-Suche erkannt": lambda p: not p.not_event_search,
    }),
    # Intent-Gate: KEINE Event-Suchen
    ("Hitzewarnung in Krefeld", {
        "not_event_search": lambda p: p.not_event_search is True,
    }),
    ("Wie bekomme ich die adresse für ein event eingegeben?", {
        "not_event_search": lambda p: p.not_event_search is True,
    }),
    ("Wie ist das Wetter morgen?", {
        "not_event_search": lambda p: p.not_event_search is True,
    }),
]


def main():
    from rag.rag_query import NaturalLanguageEventQuery, EventSearchService

    if not llm.is_available():
        print(f"FEHLER: Sprachmodell-Server nicht erreichbar ({llm.base_url()})")
        return 1
    print(f"Modell: {llm.default_model()} @ {llm.base_url()}")

    run_search = '--search' in sys.argv
    parser = NaturalLanguageEventQuery()
    service = EventSearchService() if run_search else None

    total_checks = 0
    failed_checks = 0
    failed_queries = []

    for query, checks in GOLDEN_CASES:
        print(f"\n=== '{query}'")
        try:
            params, _ = parser.parse_natural_query(query)
        except Exception as e:
            print(f"  PARSE-FEHLER: {e}")
            failed_checks += len(checks)
            total_checks += len(checks)
            failed_queries.append(query)
            continue

        query_failed = False
        for check_name, predicate in checks.items():
            total_checks += 1
            try:
                ok = bool(predicate(params))
            except Exception as e:
                ok = False
                check_name += f" (Exception: {e})"
            status = "PASS" if ok else "FAIL"
            if not ok:
                failed_checks += 1
                query_failed = True
            print(f"  [{status}] {check_name}")

        if query_failed:
            failed_queries.append(query)
            print(f"  → geparst: {params.to_dict()}")

        if run_search and not params.not_event_search:
            result = service.search(query, parsed_params=params)
            print(f"  Suche: {result.get('results_count', '?')} Events")

    print("\n" + "=" * 60)
    print(f"ERGEBNIS: {total_checks - failed_checks}/{total_checks} Checks bestanden")
    if failed_queries:
        print("Fehlgeschlagene Queries:")
        for q in failed_queries:
            print(f"  - {q}")
    return 1 if failed_checks else 0


if __name__ == "__main__":
    sys.exit(main())
