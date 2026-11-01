"""
Zentrale Duplikat-Erkennung für Artikel-Imports.

Diese Klasse implementiert cross-source Duplikat-Erkennung mit Prioritäts-basierter Entscheidungslogik:
1. Höhere Source-Priorität gewinnt (100=Original > 50=Standard > 20=Aggregator)
2. Bei gleicher Priorität: Bild → Content-Länge → Abstract-Länge
3. Fuzzy Matching: Erkennt ähnliche Titel bei gleichem Publikationsdatum

Hauptunterschiede zu Event-DuplicateHandler:
- Zeitfenster: ±48 Stunden statt ±5 Minuten
- Keine Location, statt dessen Abstract/Content-Vergleich
- Quality-Score basiert auf Bild, Content- und Abstract-Länge
"""

from logging import getLogger
from django.utils.timezone import localtime
from datetime import timedelta
from difflib import SequenceMatcher

logger = getLogger("import_articles")


def normalize_string(s):
    """
    Normalize string by removing whitespace, line breaks, and converting to lowercase.

    Also removes common article prefixes like "Breaking:", "Update:", etc.
    """
    if not s:
        return ''

    # Convert to string and basic normalization
    normalized = ' '.join(str(s).replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').split()).strip().lower()

    # Remove common article prefixes
    prefixes = [
        # English/International
        'breaking:', 'update:', 'exklusiv:', 'news:', 'aktuell:',
        # German political parties
        'cdu:', 'cdu-bürgerschaftsfraktion:', 'cdu-fraktion:',
        'spd:', 'spd-bürgerschaftsfraktion:', 'spd-fraktion:',
        'grüne:', 'bündnis 90/die grünen:', 'die grünen:',
        'fdp:', 'fdp-fraktion:',
        'afd:', 'afd-fraktion:',
        'linke:', 'die linke:', 'linke-fraktion:',
        # Institutions
        'bundestag:', 'bürgerschaftsfraktion:', 'bürgerschaft:',
        'landtag:', 'stadtrat:', 'senat:',
        # German cities
        'hamburg:', 'berlin:', 'münchen:', 'köln:', 'frankfurt:',
        'stuttgart:', 'düsseldorf:', 'dortmund:', 'essen:', 'leipzig:',
        'bremen:', 'dresden:', 'hannover:', 'nürnberg:',
    ]
    for prefix in prefixes:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
            break

    return normalized


class ArticleDuplicateHandler:
    """Zentrale Klasse für cross-source Artikel-Duplikat-Erkennung"""

    # Fuzzy Matching Schwellenwerte
    SIMILARITY_THRESHOLD = 0.80  # 80% String-Ähnlichkeit (höher als Events wegen kürzeren Texten)
    TOKEN_THRESHOLD = 0.65       # 65% gemeinsame Wörter
    TIME_TOLERANCE_HOURS = 48    # ± 48 Stunden Zeittoleranz (Artikel werden oft verzögert publiziert)

    # Abstract/Content Ähnlichkeit
    ABSTRACT_SIMILARITY_THRESHOLD = 0.70  # 70% für Abstract-Vergleich
    CONTENT_MIN_LENGTH = 100     # Minimale Content-Länge für Vergleich

    @staticmethod
    def calculate_title_similarity(title1, title2):
        """
        Berechnet String-Ähnlichkeit zwischen zwei Titeln.

        Args:
            title1: Erster Titel
            title2: Zweiter Titel

        Returns:
            Float zwischen 0.0 und 1.0 (1.0 = identisch)
        """
        normalized1 = normalize_string(title1)
        normalized2 = normalize_string(title2)

        if not normalized1 or not normalized2:
            return 0.0

        return SequenceMatcher(None, normalized1, normalized2).ratio()

    @staticmethod
    def calculate_token_similarity(title1, title2):
        """
        Berechnet Wort-basierte Ähnlichkeit (Jaccard-Ähnlichkeit).

        Args:
            title1: Erster Titel
            title2: Zweiter Titel

        Returns:
            Float zwischen 0.0 und 1.0 (1.0 = alle Wörter identisch)
        """
        normalized1 = normalize_string(title1)
        normalized2 = normalize_string(title2)

        if not normalized1 or not normalized2:
            return 0.0

        # Wörter extrahieren (min. 2 Zeichen)
        words1 = set(w for w in normalized1.split() if len(w) >= 2)
        words2 = set(w for w in normalized2.split() if len(w) >= 2)

        if not words1 or not words2:
            return 0.0

        # Jaccard-Ähnlichkeit: Schnittmenge / Vereinigung
        intersection = len(words1 & words2)
        union = len(words1 | words2)

        return intersection / union if union > 0 else 0.0

    @staticmethod
    def calculate_abstract_similarity(abstract1, abstract2):
        """
        Berechnet Ähnlichkeit zwischen zwei Abstracts/Summaries.

        Args:
            abstract1: Erster Abstract
            abstract2: Zweiter Abstract

        Returns:
            Float zwischen 0.0 und 1.0
        """
        if not abstract1 or not abstract2:
            return 0.0

        normalized1 = normalize_string(abstract1)
        normalized2 = normalize_string(abstract2)

        if not normalized1 or not normalized2:
            return 0.0

        # Für längere Texte: kombiniere String- und Token-Ähnlichkeit
        string_sim = SequenceMatcher(None, normalized1, normalized2).ratio()

        # Token-Ähnlichkeit
        words1 = set(w for w in normalized1.split() if len(w) >= 3)
        words2 = set(w for w in normalized2.split() if len(w) >= 3)

        if words1 and words2:
            token_sim = len(words1 & words2) / len(words1 | words2)
        else:
            token_sim = 0.0

        # Gewichteter Durchschnitt: 60% String, 40% Token
        return (string_sim * 0.6) + (token_sim * 0.4)

    @staticmethod
    def are_titles_similar(title1, title2):
        """
        Prüft ob zwei Titel ähnlich genug sind um als Duplikat zu gelten.

        Args:
            title1: Erster Titel
            title2: Zweiter Titel

        Returns:
            True wenn ähnlich genug, sonst False
        """
        string_sim = ArticleDuplicateHandler.calculate_title_similarity(title1, title2)
        token_sim = ArticleDuplicateHandler.calculate_token_similarity(title1, title2)

        # Titel sind ähnlich wenn ENTWEDER:
        # - String-Ähnlichkeit >= 80% ODER
        # - Token-Ähnlichkeit >= 65%
        return (string_sim >= ArticleDuplicateHandler.SIMILARITY_THRESHOLD or
                token_sim >= ArticleDuplicateHandler.TOKEN_THRESHOLD)

    @staticmethod
    def build_article_key(title, pub_date):
        """
        Erstellt eindeutigen Schlüssel für Artikel-Vergleich.

        Args:
            title: Artikel-Titel
            pub_date: Publikationsdatum (datetime object)

        Returns:
            String-Key für Duplikat-Vergleich
        """
        normalized_title = normalize_string(title)

        # Handle timezone-aware datetimes
        if hasattr(pub_date, 'tzinfo') and pub_date.tzinfo is not None:
            pub_date = localtime(pub_date)

        # Nur Datum verwenden (Uhrzeit ignorieren für Artikel)
        date_part = pub_date.strftime('%Y-%m-%d')
        key = f"{normalized_title}|{date_part}"

        return key

    @staticmethod
    def calculate_quality_score(article_or_dict):
        """
        Berechnet Qualitäts-Score für Tiebreaker bei gleicher Priorität.

        Scoring für Artikel:
        - +10 wenn image oder image_url vorhanden
        - +1 pro 200 Zeichen content (max +15)
        - +1 pro 100 Zeichen abstract (max +10)
        - +5 wenn author/image_source vorhanden

        Max Score: 40 Punkte

        Args:
            article_or_dict: Article Objekt oder Dictionary mit Artikel-Daten

        Returns:
            Integer Score (0-40)
        """
        score = 0

        # Handle both Article objects and dictionaries
        if isinstance(article_or_dict, dict):
            image = article_or_dict.get('image')
            image_url = article_or_dict.get('image_url')
            content = article_or_dict.get('content', '')
            abstract = article_or_dict.get('abstract', '')
            image_source = article_or_dict.get('image_source')
        else:
            # versatileimagefield's descriptor raises KeyError instead of lazy-loading
            # when the field was deferred via .only(), so wrap attribute access defensively.
            def _safe_attr(obj, name, default=None):
                try:
                    return getattr(obj, name, default)
                except KeyError:
                    return default
            image = _safe_attr(article_or_dict, 'image')
            image_url = _safe_attr(article_or_dict, 'image_url')
            content = _safe_attr(article_or_dict, 'content', '') or ''
            abstract = _safe_attr(article_or_dict, 'abstract', '') or ''
            image_source = _safe_attr(article_or_dict, 'image_source')

        # Bild: +10 Punkte
        if image or image_url:
            score += 10

        # Content-Länge: +1 pro 200 Zeichen (max 15)
        if content:
            score += min(len(content) // 200, 15)

        # Abstract-Länge: +1 pro 100 Zeichen (max 10)
        if abstract:
            score += min(len(abstract) // 100, 10)

        # Author/Bildquelle: +5 Punkte
        if image_source:
            score += 5

        return score

    @staticmethod
    def should_replace_existing(existing_article, new_source, new_data):
        """
        Entscheidet ob bestehender Artikel durch neuen ersetzt werden soll.

        Logik:
        1. new_source.priority > existing.source.priority → ersetzen
        2. new_source.priority < existing.source.priority → nicht ersetzen
        3. Gleiche Priorität → Quality-Score vergleichen

        Args:
            existing_article: Bestehendes Article Objekt
            new_source: Source Objekt der neuen Daten
            new_data: Dictionary mit neuen Artikel-Daten

        Returns:
            True wenn ersetzt werden soll, False sonst
        """
        existing_priority = existing_article.source.priority
        new_priority = new_source.priority

        # Höhere Priorität gewinnt
        if new_priority > existing_priority:
            logger.debug(f"Ersetze Artikel '{existing_article.title}': neue Priorität {new_priority} > {existing_priority}")
            return True

        if new_priority < existing_priority:
            logger.debug(f"Behalte Artikel '{existing_article.title}': bestehende Priorität {existing_priority} > {new_priority}")
            return False

        # Gleiche Priorität: Quality-Score vergleichen
        existing_score = ArticleDuplicateHandler.calculate_quality_score(existing_article)
        new_score = ArticleDuplicateHandler.calculate_quality_score(new_data)

        if new_score > existing_score:
            logger.debug(f"Ersetze Artikel '{existing_article.title}': neuer Score {new_score} > {existing_score}")
            return True

        # Bei gleichem Score: Älterer Artikel behält (stabiler)
        logger.debug(f"Behalte Artikel '{existing_article.title}': bestehender Score {existing_score} >= {new_score}")
        return False

    @staticmethod
    def check_same_source_duplicate(title, pub_date, source, abstract=None, use_fuzzy=True):
        """
        Prüft ob Artikel von derselben Quelle bereits existiert.

        Vierstufige Prüfung:
        1. DB-level exakter Titel-Match (schnell, kein Python-Vergleich)
        2. Normalisierter Titel-Key-Match (mit Prefix-Stripping)
        3. Identischer Abstract (case-insensitive)
        4. Fuzzy Matching mit niedrigeren Schwellen (75% String, 60% Token)

        Args:
            title: Artikel-Titel
            pub_date: Publikationsdatum
            source: Source-Objekt
            abstract: Optional - Abstract für Identitätsprüfung und Fuzzy Matching
            use_fuzzy: Optional - Fuzzy Matching aktivieren (Standard: True)

        Returns:
            Article Objekt wenn gefunden, sonst None
        """
        from content.models import Article

        article_key = ArticleDuplicateHandler.build_article_key(title, pub_date)

        # Handle timezone-aware datetimes
        if hasattr(pub_date, 'tzinfo') and pub_date.tzinfo is not None:
            pub_date_local = localtime(pub_date)
        else:
            pub_date_local = pub_date

        # Suche nach Artikeln im ±48h Fenster von derselben Quelle
        date_start = pub_date_local - timedelta(hours=ArticleDuplicateHandler.TIME_TOLERANCE_HOURS)
        date_end = pub_date_local + timedelta(hours=ArticleDuplicateHandler.TIME_TOLERANCE_HOURS)

        # STUFE 1: Schneller DB-level exakter Titel-Match (fängt identische Titel ab)
        stripped_title = title.strip() if title else ''
        if stripped_title:
            exact_match = Article.objects.filter(
                source=source,
                title__iexact=stripped_title,
                date__range=(date_start, date_end)
            ).select_related('source').first()

            if exact_match:
                logger.debug(f"Same-Source DB-level Duplikat gefunden: '{exact_match.title}' (ID: {exact_match.id})")
                return exact_match

        # STUFE 2+3: Normalisierter Key-Match und Abstract-Match
        existing = Article.objects.filter(
            source=source,
            date__range=(date_start, date_end)
        ).select_related('source').only(
            'id', 'title', 'date', 'abstract', 'image', 'image_url', 'image_source',
            'source__id', 'source__name', 'source__priority'
        )

        for article in existing:
            try:
                existing_key = ArticleDuplicateHandler.build_article_key(
                    article.title,
                    localtime(article.date)
                )
                if existing_key == article_key:
                    logger.debug(f"Same-Source Duplikat gefunden (exakter Titel): '{article.title}' (ID: {article.id})")
                    return article
            except Exception as e:
                logger.warning(f"Fehler beim Same-Source Key-Vergleich mit Artikel ID {getattr(article, 'id', '?')}: {e}")
                continue

        # Abstract-Match
        if abstract and abstract.strip():
            normalized_abstract = normalize_string(abstract)
            for article in existing:
                try:
                    if article.abstract:
                        existing_abstract = normalize_string(article.abstract)
                        if normalized_abstract == existing_abstract:
                            logger.debug(
                                f"Same-Source Duplikat gefunden (identischer Abstract): '{article.title}' (ID: {article.id})\n"
                                f"  Neuer Titel: '{title}'\n"
                                f"  Abstract: '{abstract[:100]}...'"
                            )
                            return article
                except Exception as e:
                    logger.warning(f"Fehler beim Abstract-Vergleich mit Artikel ID {getattr(article, 'id', '?')}: {e}")
                    continue

        # STUFE 4: Fuzzy Matching für Same-Source (niedrigere Schwellen als Cross-Source)
        if not use_fuzzy:
            return None

        # Niedrigere Schwellen für Same-Source (höhere Wahrscheinlichkeit echtes Duplikat)
        SAME_SOURCE_STRING_THRESHOLD = 0.75  # 75% statt 80%
        SAME_SOURCE_TOKEN_THRESHOLD = 0.60   # 60% statt 65%

        for article in existing:
            try:
                # Berechne Ähnlichkeiten
                string_sim = ArticleDuplicateHandler.calculate_title_similarity(title, article.title)
                token_sim = ArticleDuplicateHandler.calculate_token_similarity(title, article.title)

                # Match wenn EINE der Schwellen erreicht wird
                if string_sim >= SAME_SOURCE_STRING_THRESHOLD or token_sim >= SAME_SOURCE_TOKEN_THRESHOLD:
                    # Optional: Abstract als zusätzliche Validierung
                    abstract_sim = 0.0
                    if abstract and article.abstract:
                        abstract_sim = ArticleDuplicateHandler.calculate_abstract_similarity(abstract, article.abstract)

                        # Bei sehr hoher Titel-Ähnlichkeit (>=90%) Abstract-Check überspringen
                        if string_sim < 0.90 and abstract_sim < 0.3:
                            logger.debug(
                                f"Same-Source Fuzzy Match abgelehnt (Abstract zu unterschiedlich): '{article.title}' (ID: {article.id})\n"
                                f"  String-Ähnlichkeit: {string_sim:.2%}, Token: {token_sim:.2%}, Abstract: {abstract_sim:.2%}"
                            )
                            continue

                    logger.debug(
                        f"Same-Source Fuzzy Duplikat gefunden: '{article.title}' (ID: {article.id})\n"
                        f"  Neuer Titel: '{title}'\n"
                        f"  String-Ähnlichkeit: {string_sim:.2%}\n"
                        f"  Token-Ähnlichkeit: {token_sim:.2%}\n"
                        f"  Abstract-Ähnlichkeit: {abstract_sim:.2%}"
                    )
                    return article
            except Exception as e:
                logger.warning(f"Fehler beim Fuzzy-Vergleich mit Artikel ID {getattr(article, 'id', '?')}: {e}")
                continue

        return None

    @staticmethod
    def find_cross_source_duplicate(title, pub_date, abstract=None, exclude_source=None, use_fuzzy=True):
        """
        Findet Duplikat über alle Sources hinweg.

        Dreistufige Suche:
        1. DB-level exakter Titel-Match (schnellste Prüfung, gleicher Tag)
        2. Normalisierter Key-Match (mit Prefix-Stripping, ±1 Tag)
        3. Fuzzy Match (ähnlicher Titel + Datum ± 24h + optional Abstract-Ähnlichkeit)

        Args:
            title: Artikel-Titel
            pub_date: Publikationsdatum
            abstract: Optional - Abstract/Summary für zusätzlichen Vergleich
            exclude_source: Optional - Source die ausgeschlossen werden soll
            use_fuzzy: Optional - Fuzzy Matching aktivieren (Standard: True)

        Returns:
            Article Objekt wenn Duplikat gefunden, sonst None
        """
        from content.models import Article

        # Handle timezone-aware datetimes
        if hasattr(pub_date, 'tzinfo') and pub_date.tzinfo is not None:
            pub_date_local = localtime(pub_date)
        else:
            pub_date_local = pub_date

        # STUFE 1: Schneller DB-level exakter Titel-Match (±48h Fenster)
        # Fängt den häufigsten Fall ab: identische Titel von verschiedenen Sources
        stripped_title = title.strip() if title else ''
        if stripped_title:
            date_only_start = pub_date_local - timedelta(hours=ArticleDuplicateHandler.TIME_TOLERANCE_HOURS)
            date_only_end = pub_date_local + timedelta(hours=ArticleDuplicateHandler.TIME_TOLERANCE_HOURS)

            exact_qs = Article.objects.filter(
                title__iexact=stripped_title,
                date__range=(date_only_start, date_only_end)
            ).select_related('source')

            if exclude_source:
                exact_qs = exact_qs.exclude(source=exclude_source)

            exact_match = exact_qs.first()
            if exact_match:
                logger.debug(f"DB-level exakter Duplikat-Match: '{exact_match.title}' (ID: {exact_match.id})")
                return exact_match

        # STUFE 2: Normalisierter Key-Match (±48h Fenster, mit Prefix-Stripping)
        date_start = pub_date_local - timedelta(hours=ArticleDuplicateHandler.TIME_TOLERANCE_HOURS)
        date_end = pub_date_local + timedelta(hours=ArticleDuplicateHandler.TIME_TOLERANCE_HOURS)

        queryset = Article.objects.filter(
            date__range=(date_start, date_end)
        ).select_related('source').only(
            'id', 'title', 'date', 'abstract', 'image', 'image_url', 'image_source',
            'source__id', 'source__name', 'source__priority'
        )

        if exclude_source:
            queryset = queryset.exclude(source=exclude_source)

        search_key = ArticleDuplicateHandler.build_article_key(title, pub_date_local)

        for article in queryset.iterator():
            try:
                article_key = ArticleDuplicateHandler.build_article_key(
                    article.title,
                    localtime(article.date)
                )

                if article_key == search_key:
                    logger.debug(f"Exakter Duplikat-Match: '{article.title}' (ID: {article.id})")
                    return article
            except Exception as e:
                logger.warning(f"Fehler beim Cross-Source Key-Vergleich mit Artikel ID {getattr(article, 'id', '?')}: {e}")
                continue

        # STUFE 3: Fuzzy Match (nur wenn kein exakter Match und aktiviert)
        if not use_fuzzy:
            return None

        # Zeitfenster für Fuzzy Match: ± TIME_TOLERANCE_HOURS
        time_tolerance = timedelta(hours=ArticleDuplicateHandler.TIME_TOLERANCE_HOURS)
        time_start = pub_date_local - time_tolerance
        time_end = pub_date_local + time_tolerance

        # Neues Queryset für Fuzzy (iterator() hat vorheriges verbraucht)
        fuzzy_queryset = Article.objects.filter(
            date__range=(time_start, time_end)
        ).select_related('source').only(
            'id', 'title', 'date', 'abstract', 'image', 'image_url', 'image_source',
            'source__id', 'source__name', 'source__priority'
        )

        if exclude_source:
            fuzzy_queryset = fuzzy_queryset.exclude(source=exclude_source)

        for article in fuzzy_queryset.iterator():
            try:
                # Prüfe Titel-Ähnlichkeit
                if ArticleDuplicateHandler.are_titles_similar(title, article.title):
                    string_sim = ArticleDuplicateHandler.calculate_title_similarity(title, article.title)
                    token_sim = ArticleDuplicateHandler.calculate_token_similarity(title, article.title)

                    # Optional: Abstract-Ähnlichkeit als zusätzliche Validierung
                    abstract_sim = 0.0
                    if abstract and article.abstract:
                        abstract_sim = ArticleDuplicateHandler.calculate_abstract_similarity(abstract, article.abstract)

                    article_datetime = localtime(article.date)
                    logger.debug(
                        f"Fuzzy Duplikat-Match: '{article.title}' (ID: {article.id})\n"
                        f"  Neuer Titel: '{title}'\n"
                        f"  String-Ähnlichkeit: {string_sim:.2%}\n"
                        f"  Token-Ähnlichkeit: {token_sim:.2%}\n"
                        f"  Abstract-Ähnlichkeit: {abstract_sim:.2%}\n"
                        f"  Zeit-Differenz: {abs((article_datetime - pub_date_local).total_seconds() / 3600):.1f} h"
                    )

                    # Wenn Abstract vorhanden und stark unterschiedlich, kein Match
                    # ABER: Bei sehr hoher Titel-Ähnlichkeit (>=90%) Abstract-Check überspringen
                    if abstract and article.abstract and abstract_sim < 0.3 and string_sim < 0.90:
                        logger.debug(f"  -> Abgelehnt: Abstract zu unterschiedlich ({abstract_sim:.2%})")
                        continue

                    return article
            except Exception as e:
                logger.warning(f"Fehler beim Fuzzy-Vergleich mit Artikel ID {getattr(article, 'id', '?')}: {e}")
                continue

        return None

    @staticmethod
    def handle_duplicate(existing_article, new_source, new_data, dry_run=False):
        """
        Verarbeitet ein gefundenes Duplikat.

        Args:
            existing_article: Bestehendes Article Objekt
            new_source: Source der neuen Daten
            new_data: Dictionary mit neuen Artikel-Daten
            dry_run: Wenn True, wird nicht tatsächlich gelöscht (nur Logging)

        Returns:
            'replace' wenn ersetzt werden soll (existing wurde gelöscht)
            'skip' wenn bestehendes behalten werden soll
        """
        from content.models import Article

        if ArticleDuplicateHandler.should_replace_existing(existing_article, new_source, new_data):
            # Lösche bestehendes Article
            try:
                if not dry_run:
                    existing_article.delete()
                    logger.info(
                        f"Duplikat gelöscht: '{existing_article.title}' (ID: {existing_article.id}, "
                        f"Source: {existing_article.source.name}, Priorität: {existing_article.source.priority})"
                    )
                else:
                    logger.info(
                        f"[DRY-RUN] Würde Duplikat löschen: '{existing_article.title}' (ID: {existing_article.id})"
                    )
                return 'replace'
            except Article.DoesNotExist:
                # Der Artikel ist zwischen Fund und Löschung verschwunden - typisch,
                # wenn cleanup_article_duplicates parallel zum Import läuft. Kein
                # Fehler: das Ziel (Duplikat weg) ist bereits erreicht, der neue
                # Artikel darf geschrieben werden.
                logger.debug(
                    f"Duplikat '{existing_article.title}' war bereits geloescht - behandelt wie geloescht."
                )
                return 'replace'
            except Exception as e:
                logger.error(f"Fehler beim Löschen des Duplikats '{existing_article.title}': {e}")
                return 'skip'

        return 'skip'

    @staticmethod
    def find_duplicate(title, pub_date, source, abstract=None, use_fuzzy=True):
        """
        Komplette Duplikatsuche: zuerst same-source, dann cross-source.

        Args:
            title: Artikel-Titel
            pub_date: Publikationsdatum
            source: Source-Objekt
            abstract: Optional - Abstract für bessere Fuzzy-Erkennung
            use_fuzzy: Optional - Fuzzy Matching aktivieren

        Returns:
            Tuple (Article|None, match_type)
            match_type: 'same-source', 'cross-source-exact', 'cross-source-fuzzy', None
        """
        # 1. Same-Source Check (inkl. Fuzzy Matching)
        same_source = ArticleDuplicateHandler.check_same_source_duplicate(
            title, pub_date, source, abstract, use_fuzzy=use_fuzzy
        )
        if same_source:
            return (same_source, 'same-source')

        # 2. Cross-Source Check
        cross_source = ArticleDuplicateHandler.find_cross_source_duplicate(
            title, pub_date, abstract, exclude_source=source, use_fuzzy=use_fuzzy
        )
        if cross_source:
            # Bestimme ob exakt oder fuzzy
            search_key = ArticleDuplicateHandler.build_article_key(title, localtime(pub_date))
            article_key = ArticleDuplicateHandler.build_article_key(cross_source.title, localtime(cross_source.date))

            if search_key == article_key:
                return (cross_source, 'cross-source-exact')
            else:
                return (cross_source, 'cross-source-fuzzy')

        # 3. Near-Miss Warnung (wenn use_fuzzy aktiviert)
        if use_fuzzy:
            ArticleDuplicateHandler._log_near_misses(title, pub_date, source, abstract)

        return (None, None)

    @staticmethod
    def _log_near_misses(title, pub_date, source, abstract=None):
        """
        Loggt "Near-Miss" Duplikate für manuelle Review.

        Findet Artikel die ähnlich sind (70-74% für same-source, 75-79% für cross-source),
        aber nicht die Schwellen erreichen.
        """
        from content.models import Article

        if hasattr(pub_date, 'tzinfo') and pub_date.tzinfo is not None:
            pub_date_local = localtime(pub_date)
        else:
            pub_date_local = pub_date

        # Suche im ±24h Fenster
        time_tolerance = timedelta(hours=24)
        time_start = pub_date_local - time_tolerance
        time_end = pub_date_local + time_tolerance

        candidates = Article.objects.filter(
            date__range=(time_start, time_end)
        ).select_related('source')[:20]  # Limit für Performance

        near_misses = []

        for article in candidates:
            string_sim = ArticleDuplicateHandler.calculate_title_similarity(title, article.title)
            token_sim = ArticleDuplicateHandler.calculate_token_similarity(title, article.title)

            # Same-Source: 70-74%
            if article.source == source:
                if (0.70 <= string_sim < 0.75) or (0.55 <= token_sim < 0.60):
                    near_misses.append((article, string_sim, token_sim, 'same-source'))
            # Cross-Source: 75-79%
            else:
                if (0.75 <= string_sim < 0.80) or (0.60 <= token_sim < 0.65):
                    near_misses.append((article, string_sim, token_sim, 'cross-source'))

        if near_misses:
            logger.warning(
                f"⚠️  NEAR-MISS Duplikate gefunden für '{title[:60]}...' - Manuelle Review empfohlen:"
            )
            for article, string_sim, token_sim, match_type in near_misses[:3]:  # Max 3 anzeigen
                logger.warning(
                    f"   - [{match_type}] '{article.title[:60]}...' (ID: {article.id}, Quelle: {article.source.name})\n"
                    f"     String: {string_sim:.1%}, Token: {token_sim:.1%}"
                )
