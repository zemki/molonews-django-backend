from django.dispatch import Signal
from django.db.models.signals import post_delete, post_save, pre_save
from django.utils.timezone import localtime

EVENT_SAVED = Signal()

# source_id -> organization_id. Sources sind wenige und stabil; ohne Cache würde
# jeder Löschvorgang eine Extra-Query kosten (Importe löschen Events im Bulk).
_SOURCE_ORGANIZATION_CACHE = {}


def fix_null_strings(sender, instance, **kwargs):
    """Convert None to empty string for fields with null=False, blank=True"""
    for field in ['street', 'town', 'content']:
        if getattr(instance, field) is None:
            setattr(instance, field, '')


HTML_FIELDS = {
    "Article": ("abstract", "content"),
    "EventV4": ("content",),
    "Event": ("content",),
}


def sanitize_html_fields(sender, instance, **kwargs):
    """HTML-Felder vor dem Speichern bereinigen (Schutz vor Stored XSS).

    Greift für Admin, newadmin, API und Importe. bulk_create/update() umgehen
    Signale; deshalb geben die Templates Inhalte zusätzlich bereinigt aus.
    """
    from content.utils.html import sanitize_html

    # Proxy-Modelle (z. B. ArticleDraft) wie ihr echtes Modell behandeln
    for field in HTML_FIELDS.get(sender._meta.concrete_model.__name__, ()):
        value = getattr(instance, field, None)
        if value:
            setattr(instance, field, sanitize_html(value))


LINK_FIELDS = {
    "Article": ("link",),
    "EventV4": ("link", "ticket_url"),
    "Event": ("link",),
    "Organization": ("homepage",),
}


def neutralize_unsafe_links(sender, instance, **kwargs):
    """Links mit unsicherem Schema (z. B. ``javascript:``) vor dem Speichern leeren.

    Gilt für API, Oberflächen und Feed-Importe. Die API lehnt solche Links
    zusätzlich mit 400 ab, die Templates prüfen sie bei der Ausgabe erneut.
    """
    from content.utils.urls import is_safe_link

    for field in LINK_FIELDS.get(sender._meta.concrete_model.__name__, ()):
        value = getattr(instance, field, None)
        if value and not is_safe_link(value):
            setattr(instance, field, "")


def remember_publication(sender, instance, **kwargs):
    """Erste Veröffentlichung dauerhaft vermerken.

    Damit weiß der Delta-Abruf später, ob ein depublizierter Eintrag überhaupt je
    für Clients sichtbar war - nie veröffentlichte Importe gehören nicht in die
    removed-Liste.
    """
    if instance.published and not instance.was_published:
        instance.was_published = True


def _organization_id_for_source(source_id):
    """Organisation zu einer Source-ID, gecacht."""
    if source_id is None:
        return None
    if source_id not in _SOURCE_ORGANIZATION_CACHE:
        from content.models import Source

        _SOURCE_ORGANIZATION_CACHE[source_id] = (
            Source.objects.filter(pk=source_id)
            .values_list("organization_id", flat=True)
            .first()
        )
    return _SOURCE_ORGANIZATION_CACHE[source_id]


def touch_event_from_occurrence(sender, instance, **kwargs):
    """Occurrence-Änderung als Event-Änderung vermerken.

    Importe legen Termine oft neu an oder löschen sie, ohne das Event selbst zu
    speichern (z.B. duplicate_handler, manual_import_*). Ohne dieses Signal
    fielen reine Termin-Verschiebungen aus dem changed_since-Delta heraus.

    update() statt save(), um Rekursion und ein volles Schreiben aller Felder zu
    vermeiden - dabei greift auto_now nicht, updated_at wird deshalb explizit gesetzt.
    """
    from content.models import EventV4

    event_id = getattr(instance, "event_id", None)
    if event_id is None:
        return

    # Beim Löschen eines Events kaskadieren dessen Occurrences. Das Event dann noch
    # anzufassen wäre eine Query pro Termin für einen Datensatz, der gleich weg ist.
    origin = kwargs.get("origin")
    if origin is not None:
        origin_model = getattr(origin, "model", type(origin))
        if getattr(getattr(origin_model, '_meta', None), 'concrete_model', origin_model) is EventV4:
            return

    EventV4.objects.filter(pk=event_id).update(updated_at=localtime())


def _write_tombstone(content_type, instance):
    from content.models import DeletedContent

    if instance.pk is None:
        return
    # Nie veröffentlichte Inhalte (Entwürfe, ungeprüfte Importe) waren für Clients
    # nie sichtbar - ein öffentlicher Grabstein würde nur ihren Titel verraten.
    if not (getattr(instance, "published", False) or getattr(instance, "was_published", False)):
        return
    # Der Titel wird mitgeschrieben, weil er nach dem Löschen nirgends mehr
    # steht - Delta-Clients zeigen ihn im Löschprotokoll statt der bloßen ID.
    title = getattr(instance, "title", "") or ""
    DeletedContent.objects.create(
        content_type=content_type,
        object_id=instance.pk,
        organization_id=_organization_id_for_source(getattr(instance, "source_id", None)),
        language=getattr(instance, "language", None),
        title=title[:350],
    )


def article_deleted(sender, instance, **kwargs):
    _write_tombstone("article", instance)


def event_deleted(sender, instance, **kwargs):
    _write_tombstone("event", instance)


def register_signals():
    """Signale registrieren: Null-String-Fix, Occurrence-Touch und Grabsteine.

    Django sendet Signale mit dem Proxy-Modell als sender (z. B. ArticleDraft im
    Admin); die Entwurfs-Proxys werden deshalb jeweils mit verbunden.
    """
    from content.models import (
        Article, ArticleDraft, Event, EventDraft, EventDraftV4, EventV4, Event_Occurrence, Organization,
    )

    articles = (Article, ArticleDraft)
    events_v4 = (EventV4, EventDraftV4)
    legacy_events = (Event, EventDraft)

    # Use dispatch_uid to prevent duplicate registration
    for model in articles + events_v4 + legacy_events:
        pre_save.connect(
            sanitize_html_fields, sender=model,
            dispatch_uid=f'sanitize_html_{model.__name__.lower()}',
        )
    for model in articles + events_v4 + legacy_events + (Organization,):
        pre_save.connect(
            neutralize_unsafe_links, sender=model,
            dispatch_uid=f'safe_links_{model.__name__.lower()}',
        )
    for model in events_v4:
        pre_save.connect(fix_null_strings, sender=model, dispatch_uid=f'fix_null_strings_{model.__name__.lower()}')

    for model in articles + events_v4:
        pre_save.connect(
            remember_publication, sender=model, dispatch_uid=f'remember_publication_{model.__name__.lower()}'
        )

    post_save.connect(
        touch_event_from_occurrence,
        sender=Event_Occurrence,
        dispatch_uid='touch_event_occurrence_saved',
    )
    post_delete.connect(
        touch_event_from_occurrence,
        sender=Event_Occurrence,
        dispatch_uid='touch_event_occurrence_deleted',
    )

    for model in articles:
        post_delete.connect(article_deleted, sender=model, dispatch_uid=f'tombstone_{model.__name__.lower()}')
    for model in events_v4:
        post_delete.connect(event_deleted, sender=model, dispatch_uid=f'tombstone_{model.__name__.lower()}')
