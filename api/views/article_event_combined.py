"""
Kombinierte Route: Artikel + Events einer oder mehrerer Organisationen.

GET /api/v4/organizations/content/

Liefert veröffentlichte Artikel und Events der angegebenen Organisation(en)
in einer Response. Jeder Eintrag in "results" ist ein Sprachpaar
{"de": {...}, "en": {...}}, gebildet über die translations-Verknüpfung.
Gespeicherte User-Einstellungen (abgewählte Organisationen, Tag-Präferenzen)
werden bewusst nicht angewendet - die Auswahl kommt ausschließlich aus dem
organizations-Parameter.

Jede Response enthält zusätzlich "taxonomies": alle Tags und Subtags der
Organisation(en), je mit deutscher und englischer Fassung unter derselben ID.
Der Block ist vollständig und filterunabhängig, damit ein importierendes System
die Begriffe vorab anlegen und ihre Sprachversionen verknüpfen kann.

Delta-Abruf: Mit "changed_since" kommen nur Einträge zurück, die sich seither
geändert haben, plus je Block eine "removed"-Liste mit gelöschten und
depublizierten Einträgen (ID, Grund und Titel). Der Client schickt beim nächsten Aufruf das "server_time"
aus der letzten Response als neues changed_since; damit spielt eine abweichende
Uhr auf dem Client keine Rolle.
"""

from datetime import datetime
from logging import getLogger

from django.db.models import Exists, OuterRef, Prefetch, Q
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.timezone import get_current_timezone, is_naive, localtime, make_aware
from drf_yasg.utils import swagger_auto_schema
from rest_framework import serializers, viewsets
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response

from content.models import (
    Article,
    DeletedContent,
    EventV4,
    Organization,
    Subtag,
    Tag,
)

from .article_v4 import ArticleRetrieveSerializer
from .event_v4 import EventRetrieveSerializer, build_fulltext_filter
from .util import (
    bad_request,
    choices_parameter,
    header_string_parameter,
    integer_parameter,
    isodate_parameter,
    string_parameter,
)


class OrganizationContentPagination(LimitOffsetPagination):
    """Kleines Limit, da pro Eintrag bis zu zwei volle Sprachversionen serialisiert werden."""

    default_limit = 10
    max_limit = 25


logger = getLogger("molonews")

# Sicherheitsventil für die removed-Liste je Block, damit eine Antwort nicht
# unbegrenzt wachsen kann. Im Normalbetrieb unerreichbar - gelöscht werden pro Tag
# einige Dutzend Einträge über alle Organisationen hinweg. Wird die Grenze doch
# einmal erreicht, steht es im Log; die Antwort selbst bleibt schlicht.
MAX_REMOVED_IDS = 1000


class OrganizationContentArticleSerializer(ArticleRetrieveSerializer):
    class Meta(ArticleRetrieveSerializer.Meta):
        ref_name = "OrganizationContentArticle"


class OrganizationContentEventSerializer(EventRetrieveSerializer):
    class Meta(EventRetrieveSerializer.Meta):
        ref_name = "OrganizationContentEvent"


class ArticleLanguagePairSerializer(serializers.Serializer):
    de = OrganizationContentArticleSerializer(allow_null=True, required=False)
    en = OrganizationContentArticleSerializer(allow_null=True, required=False)


class EventLanguagePairSerializer(serializers.Serializer):
    de = OrganizationContentEventSerializer(allow_null=True, required=False)
    en = OrganizationContentEventSerializer(allow_null=True, required=False)


class RemovedEntrySerializer(serializers.Serializer):
    """Ein Eintrag, den der Client aus seinem lokalen Bestand entfernen soll."""

    type = serializers.CharField(help_text='"article" oder "event"')
    id = serializers.IntegerField()
    reason = serializers.CharField(
        help_text='"deleted" (aus der Datenbank entfernt) oder "unpublished" (depubliziert)'
    )
    title = serializers.CharField(
        allow_blank=True,
        help_text=(
            "Titel des entfernten Eintrags, damit ein Löschprotokoll nicht nur "
            "die ID zeigt. Leer bei Einträgen, die vor Einführung dieses Feldes "
            "gelöscht wurden - dann bleibt nur die ID"
        ),
    )


class PaginatedArticlePairSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    previous = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    results = ArticleLanguagePairSerializer(many=True)
    removed = RemovedEntrySerializer(many=True, required=False)


class PaginatedEventPairSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    previous = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    results = EventLanguagePairSerializer(many=True)
    removed = RemovedEntrySerializer(many=True, required=False)


class TermSerializer(serializers.Serializer):
    """Ein Tag bzw. Subtag mit beiden Sprachfassungen."""

    id = serializers.IntegerField(
        help_text="Molo-ID des Begriffs - identisch für beide Sprachfassungen"
    )
    de = serializers.CharField(help_text="Deutscher Name")
    en = serializers.CharField(
        help_text="Englischer Name; fällt auf den deutschen zurück, wenn keiner gepflegt ist"
    )


class TaxonomiesSerializer(serializers.Serializer):
    tags = TermSerializer(many=True)
    subtags = TermSerializer(many=True)


class OrganizationContentResponseSerializer(serializers.Serializer):
    server_time = serializers.DateTimeField(
        help_text="Beim nächsten Delta-Abruf als changed_since mitschicken"
    )
    taxonomies = TaxonomiesSerializer()
    articles = PaginatedArticlePairSerializer()
    events = PaginatedEventPairSerializer()


class OrganizationTaxonomiesResponseSerializer(serializers.Serializer):
    server_time = serializers.DateTimeField()
    taxonomies = TaxonomiesSerializer()


def _parse_int_list(value):
    return [int(part) for part in value.split(",") if part.strip().isdigit()] if value else []


def _visible_translations(model, org_ids):
    """Übersetzungen, die ausgeliefert werden dürfen: veröffentlicht und aus den angefragten Organisationen."""
    return model.objects.filter(published=True, source__organization__in=org_ids)


def _has_visible_translation(model, language, org_ids):
    return Exists(
        _visible_translations(model, org_ids).filter(translations=OuterRef("pk"), language=language)
    )


def _language_pair_anchor_filter(model, languages, org_ids):
    """Q-Filter für die Anker-Datensätze der Sprachpaare.

    Anker ist immer die deutsche Version; englische Inhalte ohne (sichtbare)
    deutsche Übersetzung sind ihr eigener Anker (Eintrag mit "de": null). So
    erscheint jedes Sprachpaar genau einmal, auch wenn beide Versionen den Filter
    treffen. Unveröffentlichte Übersetzungen zählen nicht - sie dürfen weder
    ausgeliefert werden noch einen öffentlichen Eintrag verdrängen.
    """
    has_de = _has_visible_translation(model, "de", org_ids)
    if languages == {"de"}:
        return Q(language="de")
    if languages == {"en"}:
        # Nur Paare, zu denen es eine englische Version gibt
        return (Q(language="de") & Q(_has_visible_translation(model, "en", org_ids))) | (
            Q(language="en") & ~Q(has_de)
        )
    return Q(language="de") | (Q(language="en") & ~Q(has_de))


def _parse_changed_since(raw_value):
    """changed_since parsen. Gibt (datetime, None) oder (None, Fehlertext) zurück.

    Akzeptiert einen vollen ISO-8601-Timestamp (bevorzugt: das server_time der
    letzten Response) und der Bequemlichkeit halber auch ein reines Datum, das
    dann als Tagesbeginn gilt. Zeitzonenlose Angaben werden in der Serverzeitzone
    interpretiert.

    Die ValueError-Zweige sind nötig: Bei formal passenden, aber unmöglichen
    Angaben (2026-13-01, 2026-02-30, Stunde 25) geben Djangos Parser nicht None
    zurück, sondern werfen - ungefangen wäre das ein 500 statt eines 400.
    """
    try:
        parsed = parse_datetime(raw_value)
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            as_date = parse_date(raw_value)
        except ValueError:
            as_date = None
        if as_date is None:
            return None, (
                "changed_since invalid - expected ISO 8601 timestamp "
                "(e.g. 2026-07-15T09:00:00Z) or date YYYY-MM-DD"
            )
        parsed = datetime.combine(as_date, datetime.min.time())
    if is_naive(parsed):
        parsed = make_aware(parsed, get_current_timezone())
    return parsed, None


def _changed_since_filter(changed_since):
    """Paare, bei denen sich irgendeine Sprachversion geändert hat.

    Der translations-Zweig ist wichtig: Anker eines Paares ist die deutsche
    Version, geändert wurde aber vielleicht nur die englische. Ohne ihn fiele das
    Paar aus dem Delta.
    """
    return Q(updated_at__gte=changed_since) | Q(
        translations__updated_at__gte=changed_since
    )


def _build_removed_entries(model, content_type, org_ids, changed_since):
    """Gelöschte und depublizierte Einträge, die der Client entfernen soll.

    Beides landet in einer Liste, weil es für den Client derselbe Vorgang ist -
    unterschieden wird nur über "reason".
    """
    deleted = DeletedContent.objects.filter(
        content_type=content_type,
        organization_id__in=org_ids,
        deleted_at__gte=changed_since,
    ).values_list("object_id", "title")[:MAX_REMOVED_IDS]

    # title kann leer sein: Grabsteine aus der Zeit vor dem Titel-Feld haben
    # keinen, und nachtragen lässt er sich nicht - der Eintrag ist ja weg.
    removed = [
        {"type": content_type, "id": object_id, "reason": "deleted", "title": title or ""}
        for object_id, title in deleted
    ]

    remaining = MAX_REMOVED_IDS - len(removed)
    if remaining > 0:
        # was_published grenzt auf Einträge ein, die überhaupt je ausgeliefert
        # wurden. Ohne das würde jeder Batch-Lauf über die unveröffentlichten
        # Importe (allein bei Artikeln ein fünfstelliger Bestand) als Schwall
        # angeblicher Löschungen bei allen Clients landen.
        unpublished = model.objects.filter(
            published=False,
            was_published=True,
            source__organization__in=org_ids,
            updated_at__gte=changed_since,
        ).values_list("id", "title")[:remaining]
        removed.extend(
            {"type": content_type, "id": object_id, "reason": "unpublished", "title": title or ""}
            for object_id, title in unpublished
        )

    return removed


def _resolve_org_ids(request):
    """Organisations-IDs aus der Query lesen und prüfen.

    Gibt (org_ids, None) zurück oder (None, Fehler-Response).
    """
    org_ids = _parse_int_list(request.query_params.get("organizations", ""))
    if not org_ids:
        return None, bad_request("organizations missing or invalid")

    existing_ids = set(
        Organization.objects.filter(id__in=org_ids).values_list("id", flat=True)
    )
    missing_ids = [str(org_id) for org_id in org_ids if org_id not in existing_ids]
    if missing_ids:
        return None, Response(
            {"detail": "Organization(s) not found: {}".format(", ".join(missing_ids))},
            status=404,
        )
    return org_ids, None


def _terms_as_pairs(model, ids):
    """Term-Objekte als {"id", "de", "en"}, alphabetisch nach deutschem Namen."""
    return [
        {"id": term_id, "de": name, "en": name_en or name}
        for term_id, name, name_en in model.objects.filter(id__in=ids)
        .order_by("name")
        .values_list("id", "name", "name_en")
    ]


def _build_taxonomies(org_ids):
    """Alle Tags und Subtags der Organisation(en), je mit beiden Sprachfassungen.

    Bewusst unabhängig von Pagination und sämtlichen Filtern (tags, subtags,
    search, date_start/date_end, language, changed_since): Der Importer soll die
    Begriffe einmal vollständig anlegen und verknüpfen können, bevor er die
    Beiträge einspielt. Eine Liste, die sich je nach Filter ändert, müsste er
    laufend nachziehen.

    Die ID ist für beide Sprachfassungen dieselbe - in Molo ist ein Begriff ein
    Datensatz mit zwei Namen, nicht zwei getrennte Begriffe. Damit ist sie der
    Schlüssel, über den sich die beiden Sprachversionen auf Zielsystemen wie
    Polylang miteinander verknüpfen lassen.

    Berücksichtigt wird nur veröffentlichter Inhalt - unveröffentlichte Beiträge
    liefert die Route auch nicht aus, ihre Begriffe gehören also nicht dazu.
    """
    published_articles = Article.objects.filter(
        published=True, source__organization__in=org_ids
    )
    published_events = EventV4.objects.filter(
        published=True, source__organization__in=org_ids
    )

    tag_ids = set(published_articles.values_list("tags__id", flat=True))
    tag_ids |= set(published_events.values_list("tags__id", flat=True))
    tag_ids.discard(None)

    # Subtags haengen nur an Events - und zusaetzlich direkt an der Organisation.
    # Der zweite Zweig ist der wichtigere: Organisationen ohne Events haetten
    # sonst gar keine Subtags, obwohl im Backend welche gepflegt sind.
    subtag_ids = set(published_events.values_list("subtags__id", flat=True))
    subtag_ids |= set(
        Subtag.objects.filter(organizations__in=org_ids).values_list("id", flat=True)
    )
    subtag_ids.discard(None)

    return {
        "tags": _terms_as_pairs(Tag, tag_ids),
        "subtags": _terms_as_pairs(Subtag, subtag_ids),
    }


def _build_language_pairs(items, serializer_class, languages, context):
    """Serialisiert Anker-Datensätze als Sprachpaare {"de": ..., "en": ...}."""
    pairs = []
    for item in items:
        if item.language == "en":
            de_item, en_item = None, item
        else:
            de_item, en_item = item, None
        if de_item is not None and "en" in languages:
            en_item = next(
                (t for t in de_item.translations.all() if t.language == "en"), None
            )
        pairs.append(
            {
                "de": serializer_class(de_item, context=context).data
                if de_item is not None and "de" in languages
                else None,
                "en": serializer_class(en_item, context=context).data
                if en_item is not None and "en" in languages
                else None,
            }
        )
    return pairs


class OrganizationContentViewSet(viewsets.GenericViewSet):
    queryset = Article.objects.none()  # nur für Schema-Introspektion
    serializer_class = OrganizationContentResponseSerializer
    pagination_class = OrganizationContentPagination

    @swagger_auto_schema(
        operation_description=(
            "Liefert veröffentlichte Artikel und Events einer oder mehrerer "
            "Organisationen in einer Response.\n\n"
            "Jeder Eintrag in \"results\" ist ein Sprachpaar {\"de\": {...}, \"en\": {...}}; "
            "fehlt eine Sprachversion, ist der jeweilige Key null. "
            "Artikel sind absteigend nach Publikationsdatum sortiert, Events "
            "aufsteigend nach Veranstaltungsdatum ab date_start (Default heute); "
            "vergangene Events sind über ein date_start in der Vergangenheit abrufbar. "
            "Artikel- und Event-Block werden getrennt paginiert; limit/offset "
            "wirken auf beide Blöcke.\n\n"
            "\"taxonomies\" listet alle Tags und Subtags der Organisation(en) als "
            "{id, de, en}. Die ID ist für beide Sprachfassungen dieselbe und eignet "
            "sich damit als Schlüssel, um die Sprachversionen im Zielsystem "
            "miteinander zu verknüpfen. Der Block ist unpaginiert und unabhängig "
            "von allen Filtern - er beschreibt den vollständigen Begriffsbestand "
            "der Organisation, nicht nur den der aktuellen Seite. Enthalten sind "
            "die an veröffentlichten Inhalten verwendeten Begriffe sowie die "
            "Subtags, die direkt an der Organisation hinterlegt sind. Ist kein "
            "englischer Name gepflegt, steht in \"en\" der deutsche.\n\n"
            "Delta-Abruf: Mit \"changed_since\" kommen nur seither geänderte oder neue "
            "Einträge zurück, dazu je Block eine \"removed\"-Liste mit gelöschten "
            "(reason \"deleted\") und depublizierten (reason \"unpublished\") Einträgen. "
            "Jeder Eintrag trägt neben type, id und reason auch den \"title\" des "
            "entfernten Inhalts, damit ein Löschprotokoll lesbar bleibt und nicht "
            "nur die Molo-ID zeigt. Der Titel ist leer bei Inhalten, die vor "
            "Einführung dieses Feldes gelöscht wurden. "
            "Die Response enthält immer \"server_time\" - diesen Wert beim nächsten "
            "Abruf als changed_since mitschicken, dann muss der Client keine eigene "
            "Uhrzeit berechnen. Löschmarker werden begrenzte Zeit aufbewahrt; wer "
            "sehr lange nicht abgerufen hat, sollte einmal ohne changed_since "
            "durchpaginieren."
        ),
        manual_parameters=[
            string_parameter(
                "organizations",
                "Organisations-ID(s) - kommasepariert, z. B. '3' oder '3,7,12' (Pflicht)",
                required=True,
            ),
            string_parameter(
                "search",
                "Suchbegriff - Artikel: Titel, Abstract, Content, Tags; "
                "Events: Volltextsuche inkl. Ort und Quelle",
            ),
            string_parameter("tags", "Tag-ID(s) - kommasepariert (Artikel und Events)"),
            string_parameter("subtags", "Subtag-ID(s) - kommasepariert (nur Events)"),
            string_parameter(
                "language",
                "Sprachversion(en): 'de', 'en' oder 'de,en' (Default). Steuert, "
                "welche Sprach-Keys der Paare gefüllt werden; bei 'en' kommen nur "
                "Einträge mit englischer Version zurück",
            ),
            isodate_parameter(
                "date_start",
                "ISO 8601 - YYYY-MM-DD | frühestes Veranstaltungsdatum (nur Events), "
                "Default heute; Datum in der Vergangenheit liefert auch vergangene Events",
            ),
            isodate_parameter(
                "date_end",
                "ISO 8601 - YYYY-MM-DD | spätestes Veranstaltungsdatum (nur Events)",
            ),
            isodate_parameter(
                "changed_since",
                "ISO 8601 Timestamp (z. B. 2026-07-15T09:00:00Z) oder YYYY-MM-DD | "
                "nur seither geänderte/neue Einträge; Änderungen an einer Sprachversion "
                "bringen immer das ganze Paar zurück. Am besten das server_time der "
                "letzten Response verwenden",
            ),
            choices_parameter(
                "ordering",
                ("date", "changed"),
                'Sortierung: "date" (Default - Artikel neueste zuerst, Events '
                'chronologisch) oder "changed" (zuletzt geänderte zuerst, sinnvoll '
                'in Kombination mit changed_since)',
            ),
            integer_parameter(
                "limit", "Anzahl Einträge pro Block (Default 10, Maximum 25)"
            ),
            integer_parameter("offset", "Result-Offset, wirkt auf beide Blöcke"),
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={
            200: OrganizationContentResponseSerializer,
            400: "Ungültiger oder fehlender Parameter / Device ID fehlt",
            404: "Organisation(en) nicht gefunden",
        },
    )
    def list(self, request, *args, **kwargs):
        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return bad_request("Device ID is missing")

        org_ids, error = _resolve_org_ids(request)
        if error:
            return error

        language_param = request.query_params.get("language", "").strip().lower()
        languages = {lang.strip() for lang in language_param.split(",") if lang.strip()}
        if any(lang not in ("de", "en") for lang in languages):
            return bad_request("Invalid language. Use 'de', 'en' or 'de,en'.")
        if not languages:
            languages = {"de", "en"}

        tags_list = _parse_int_list(request.query_params.get("tags", ""))
        subtags_list = _parse_int_list(request.query_params.get("subtags", ""))
        search = request.query_params.get("search")

        changed_since = None
        raw_changed_since = request.query_params.get("changed_since")
        if raw_changed_since:
            changed_since, error = _parse_changed_since(raw_changed_since)
            if error:
                return bad_request(error)

        ordering = request.query_params.get("ordering", "date").strip().lower()
        if ordering not in ("date", "changed"):
            return bad_request("Invalid ordering. Use 'date' or 'changed'.")

        date_filters = {}
        for param_name in ("date_start", "date_end"):
            raw_value = request.query_params.get(param_name)
            if raw_value:
                try:
                    date_filters[param_name] = datetime.strptime(
                        raw_value, "%Y-%m-%d"
                    ).date()
                except ValueError:
                    return bad_request(
                        "{} invalid - expected ISO format YYYY-MM-DD".format(param_name)
                    )
        # Default: nur Events ab heute; Vergangenheit nur bei explizitem date_start
        if "date_start" not in date_filters:
            date_filters["date_start"] = localtime().date()

        article_anchor_filter = _language_pair_anchor_filter(Article, languages, org_ids)
        event_anchor_filter = _language_pair_anchor_filter(EventV4, languages, org_ids)

        # Artikel: absteigend nach Publikationsdatum
        article_qs = (
            Article.objects.filter(
                published=True, source__organization__in=org_ids
            )
            .filter(article_anchor_filter)
            .select_related("source", "source__organization")
            .prefetch_related(
                Prefetch("translations", queryset=_visible_translations(Article, org_ids)),
                "tags", "area",
            )
            .order_by("-updated_at" if ordering == "changed" else "-date")
        )
        if changed_since:
            article_qs = article_qs.filter(_changed_since_filter(changed_since))
        if tags_list:
            article_qs = article_qs.filter(tags__id__in=tags_list)
        if search:
            article_qs = article_qs.filter(
                Q(title__icontains=search)
                | Q(abstract__icontains=search)
                | Q(content__icontains=search)
                | Q(tags__name__icontains=search)
            )
        article_qs = article_qs.distinct()

        # Events: ab date_start (Default heute), aufsteigend nach Veranstaltungsdatum
        event_qs = (
            EventV4.objects.filter(
                published=True, source__organization__in=org_ids
            )
            .filter(event_anchor_filter)
            .select_related("source", "source__organization")
            .prefetch_related(
                Prefetch("translations", queryset=_visible_translations(EventV4, org_ids)),
                "tags", "genres", "film_languages", "area", "occurrences",
            )
            .order_by("-updated_at" if ordering == "changed" else "start_date")
        )
        if changed_since:
            event_qs = event_qs.filter(_changed_since_filter(changed_since))
        if tags_list:
            event_qs = event_qs.filter(tags__id__in=tags_list)
        if subtags_list:
            event_qs = event_qs.filter(subtags__id__in=subtags_list)
        if search:
            event_qs = event_qs.filter(build_fulltext_filter(search))
        if "date_start" in date_filters:
            event_qs = event_qs.filter(start_date__date__gte=date_filters["date_start"])
        if "date_end" in date_filters:
            event_qs = event_qs.filter(start_date__date__lte=date_filters["date_end"])
        event_qs = event_qs.distinct()

        context = {"request": request}
        # Vor dem Serialisieren gestempelt: alles, was währenddessen geschrieben
        # wird, gehört bereits ins nächste Delta und darf nicht übersprungen werden.
        server_time = localtime()
        response_data = {
            "server_time": server_time,
            "taxonomies": _build_taxonomies(org_ids),
        }

        for key, queryset, serializer_class, model, content_type in (
            ("articles", article_qs, OrganizationContentArticleSerializer, Article, "article"),
            ("events", event_qs, OrganizationContentEventSerializer, EventV4, "event"),
        ):
            paginator = OrganizationContentPagination()
            page = paginator.paginate_queryset(queryset, request, view=self)
            block = {
                "count": paginator.count,
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": _build_language_pairs(
                    page, serializer_class, languages, context
                ),
            }
            if changed_since:
                # Unpaginiert und gedeckelt: die Liste hängt nicht am limit/offset
                # der results, sonst müsste der Client zwei Paginierungen verheiraten.
                block["removed"] = _build_removed_entries(
                    model, content_type, org_ids, changed_since
                )
                if len(block["removed"]) >= MAX_REMOVED_IDS:
                    logger.warning(
                        "removed-Liste gedeckelt: %s, Organisationen %s, "
                        "changed_since %s - Liste ist abgeschnitten",
                        content_type, org_ids, changed_since,
                    )
            response_data[key] = block

        return Response(response_data)


class OrganizationTaxonomiesViewSet(viewsets.GenericViewSet):
    """Nur die Begriffe einer Organisation - ohne Inhalte.

    Derselbe Block wie in /organizations/content/, aber ohne die Beiträge. Für
    importierende Systeme, die die Begriffe in einem eigenen Durchgang anlegen,
    bevor sie die Inhalte einspielen: eine Content-Seite wiegt je nach
    Organisation mehrere hundert Kilobyte, dieser Abruf ein paar hundert Byte.
    """

    queryset = Organization.objects.none()  # nur für Schema-Introspektion
    serializer_class = OrganizationTaxonomiesResponseSerializer
    pagination_class = None

    @swagger_auto_schema(
        operation_description=(
            "Liefert alle Tags und Subtags einer oder mehrerer Organisationen als "
            "{id, de, en} - ohne Artikel und Events.\n\n"
            "Die ID ist für beide Sprachfassungen dieselbe: In Molo ist ein Begriff "
            "ein Datensatz mit zwei Namen, nicht zwei getrennte Begriffe. Damit "
            "eignet sie sich als Schlüssel, um die Sprachversionen im Zielsystem "
            "miteinander zu verknüpfen (etwa als Polylang-Übersetzungspaar).\n\n"
            "Enthalten sind die an veröffentlichten Inhalten verwendeten Begriffe "
            "sowie die Subtags, die direkt an der Organisation hinterlegt sind - "
            "unabhängig davon, ob deren Events in der Vergangenheit liegen. Ist "
            "kein englischer Name gepflegt, steht in \"en\" der deutsche.\n\n"
            "Inhaltlich identisch mit dem \"taxonomies\"-Block aus "
            "/organizations/content/. Wer ohnehin Inhalte abruft, braucht diese "
            "Route nicht."
        ),
        manual_parameters=[
            string_parameter(
                "organizations",
                "Organisations-ID(s) - kommasepariert, z. B. '256' oder '256,3' (Pflicht)",
                required=True,
            ),
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={
            200: OrganizationTaxonomiesResponseSerializer,
            400: "Ungültiger oder fehlender Parameter / Device ID fehlt",
            404: "Organisation(en) nicht gefunden",
        },
    )
    def list(self, request, *args, **kwargs):
        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return bad_request("Device ID is missing")

        org_ids, error = _resolve_org_ids(request)
        if error:
            return error

        return Response(
            {
                "server_time": localtime(),
                "taxonomies": _build_taxonomies(org_ids),
            }
        )
