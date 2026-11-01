from datetime import datetime, timedelta
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.timezone import localtime, make_aware
from django.utils.translation import gettext_lazy as _
from versatileimagefield.fields import VersatileImageField
import re

from recurrence.fields import RecurrenceField

from .choices import ORGANIZATION_TYPE_CHOICES, RECURRING_EVENT_CHOICES, LANGUAGE_CHOICES
from .signals import EVENT_SAVED


def override_field_propertys(**property_dict):
    """Decorator to override model field properties.

    Args:
        property_dict (dict): dict containing property infos

    Returns:

    """

    def wrap(cls):
        for fieldname, property in property_dict.items():
            for prop_name, prop_value in property.items():
                setattr(cls._meta.get_field(fieldname), prop_name, prop_value)
        return cls

    return wrap

class Area(models.Model):

    name = models.CharField(
        max_length=100, blank=False, null=False, unique=True
    )
    latitude = models.DecimalField(
        max_digits=22, decimal_places=16, blank=True, null=True
    )
    longitude = models.DecimalField(
        max_digits=22, decimal_places=16, blank=True, null=True
    )
    zip = models.CharField(
        max_length=10, blank=True, null=True
    )

    created_at = models.DateTimeField(auto_now_add=True, null=True)

    visible = models.BooleanField(default=True)

    def __str__(self):
        return self.name
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            # Add other fields as needed
        }

    class Meta:
        verbose_name = _("area")
        verbose_name_plural = _("areas")


class App_urls(models.Model):

    name  = models.CharField(
        max_length=100, blank=False, null=False
    )
    url  = models.CharField(
        max_length=100, blank=False, null=False
    )
    def __str__(self):
        return self.name

    class Meta:
        verbose_name = _("app_url")
        verbose_name_plural = _("app_urls")



class Organization(models.Model):
    name = models.CharField(
        max_length=100, blank=False, null=False, verbose_name=_("name")
    )
    title = models.CharField(
        max_length=100, blank=True, null=True, verbose_name=_("title")
    )
    related_user = models.ForeignKey('User', null=True, blank=True, on_delete=models.CASCADE)
    description = models.TextField(null=True, blank=True, verbose_name=_("description"))
    address = models.TextField(null=True, blank=True, verbose_name=_("address"))
    street = models.TextField(null=True, blank=True, verbose_name=_("street"))
    zip = models.TextField(null=True, blank=True, verbose_name=_("zip"))
    town = models.TextField(null=True, blank=True, verbose_name=_("town"))

    type = models.CharField(
        max_length=10, choices=ORGANIZATION_TYPE_CHOICES, verbose_name=_("type")
    )
    image = models.ImageField(null=True, blank=True, verbose_name=_("image"))
    image_source = models.CharField(
        max_length=200, null=True, blank=True, verbose_name=_("image_source")
    )

    homepage = models.URLField(max_length=300, null=True, blank=True, verbose_name=_("homepage"))
    active = models.BooleanField(default=True, verbose_name=_("active"))

    area = models.ManyToManyField(Area, verbose_name=_("area"))
    created_at = models.DateTimeField(null=True, blank=True, verbose_name=_("created at"))

    class Meta:
        verbose_name = _("organization")
        verbose_name_plural = _("organizations")
        indexes = [
            models.Index(fields=["type"], name="org_type_idx"),
            models.Index(fields=["active"], name="org_active_idx"),
        ]


    def save(self, *args, **kwargs):
        if self.pk is None and self.created_at is None:
            from django.utils import timezone
            self.created_at = timezone.now()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Category(models.Model):

    name = models.CharField(max_length=100, blank=False, null=False)
    title = models.CharField(max_length=100, null=False, blank=False)
    description = models.TextField(null=False, blank=True)
    rank = models.IntegerField()

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = _("category")
        verbose_name_plural = _("categories")

class TagCategories(models.Model):

    name = models.CharField(max_length=100, blank=False, null=False)
    description = models.TextField(null=False, blank=True)
    order = models.IntegerField(default=0, verbose_name=_("order"))

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = _("tagcategory")
        verbose_name_plural = _("tagcategories")

# the link model is used to store links between events to connect
# them to each other
class EventLinks(models.Model):

    uuid = models.CharField(max_length=40)
    name = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        verbose_name = _("link")
        verbose_name_plural = _("links")

    def __str__(self):
        return self.name
    

class Tag(models.Model):

    name = models.CharField(max_length=30)
    # Englische Fassung von name. Leer = es gibt (noch) keine, dann greift der
    # deutsche Name als Fallback. name bleibt der kanonische Bezeichner.
    name_en = models.CharField(
        max_length=60, blank=True, default="", verbose_name=_("name (english)")
    )
    color = models.CharField(max_length=10, blank=True, default="")
    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="tags"
    )
    type = models.CharField(
        max_length=10, choices=(("article", "Article"), ("event", "Event")), default="article"
    )
    tagcategories = models.ForeignKey(
        TagCategories, on_delete=models.SET_NULL, related_name="tagcategory", null=True, blank=True
    )
    # Regionale Themen: Themenliste und Feed der App zeigen ein Tag mit Gebieten
    # nur Geräten, die eines davon unter ihren Gebieten haben.
    areas = models.ManyToManyField(
        Area, blank=True, related_name="regional_tags", verbose_name="Nur in Gebieten",
        help_text="Leer = überall. Sonst sehen das Thema nur Nutzer mit einem dieser Gebiete.",
    )
    # Tags, die ein eigener Abgleich nach einer festen Regel setzt. Die KI bekommt
    # sie nicht angeboten und lässt sie beim Neuverschlagworten stehen.
    assigned_by_rule = models.BooleanField(
        default=False, verbose_name="Per Regel vergeben",
        help_text="Wird automatisch vergeben, nicht von der KI.",
    )

    class Meta:
        verbose_name = _("tag")
        verbose_name_plural = _("tags")

    def __str__(self):
        return self.name

    @staticmethod
    def hidden_ids_for_areas(area_ids):
        """IDs der regionalen Tags, die in keinem dieser Gebiete gelten."""
        return set(
            Tag.objects.filter(areas__isnull=False)
            .exclude(areas__in=area_ids)
            .values_list("id", flat=True)
        )

    def get_categories(self):
        return ", ".join([cat.name for cat in self.category.all()])

class Subtag(models.Model):

    name = models.CharField(max_length=130)
    name_en = models.CharField(
        max_length=200, blank=True, default="", verbose_name=_("name (english)")
    )

    organizations = models.ManyToManyField(
        Organization, blank=True, related_name="subtags"
    )

    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        verbose_name = "Weiteres Thema"
        verbose_name_plural = "Weitere Themen"

    def __str__(self):
        return self.name

def source_image_path(instance, filename):
    path = "upload/source/{:08d}-{}".format(instance.id, filename)
    return path


class Source(models.Model):
    name = models.CharField(max_length=100, blank=False, null=False)
    type = models.CharField(
        max_length=100, choices=(("local", "Local"), ("rss", "RSS"), ("ai-article", "AI-ARTICLE-IMPORT"), ("ai-event", "AI-EVENT-IMPORT"), ("html-event", "HTML-EVENT"), ("json-event", "JSON-EVENT"), ("xml-event", "XML-EVENT"), ("ics", "ICS"))
    )
    active = models.BooleanField(default=False, verbose_name=_("active"))
    priority = models.IntegerField(
        default=50,
        verbose_name=_("priority"),
        help_text=_("100=Originalquelle, 50=Standard, 20=Aggregator")
    )
    parser = models.CharField(max_length=20, blank=True, null=True)
    link = models.URLField(max_length=300, blank=True, null=True)
    result = models.URLField(max_length=300, blank=True, null=True)
    import_script = models.CharField(max_length=100, blank=True, null=True)
    import_script_approved_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name=_("Freigabe des Import-Scripts"),
        help_text=_(
            "SHA-256 des freigegebenen Import-Scripts. Generierte Scripts werden "
            "erst nach der Freigabe durch einen Superuser ausgeführt."
        ),
    )

    organization = models.ForeignKey(Organization, null=True, on_delete=models.CASCADE)
    related_user = models.ForeignKey('User', null=True, blank=True, on_delete=models.CASCADE)
    default_category = models.ForeignKey(
        Category, null=False, blank=False, on_delete=models.CASCADE
    )
    default_tags = models.ManyToManyField(Tag, blank=True)
    default_published = models.BooleanField(default=False)
    default_image_url = models.URLField(max_length=300, null=True, blank=True)
    default_image = VersatileImageField(
        null=True, blank=True, upload_to=source_image_path
    )
    default_image_detail = VersatileImageField(
        null=True, blank=True, upload_to=source_image_path
    )
    import_date = models.DateTimeField(blank=True, null=True)
    import_errors = models.TextField(blank=True, null=True)

    auto_detect_area_from_content = models.BooleanField(
        default=False,
        verbose_name=_("Gebiete aus den Inhalten der Artikel ermitteln")
    )

    area = models.ManyToManyField(Area, verbose_name=_("area"), blank=True)
    created_at = models.DateTimeField(null=True, blank=True, verbose_name=_("created at"))

    class Meta:
        verbose_name = _("source")
        verbose_name_plural = _("sources")
        indexes = [
            models.Index(fields=['type'], name='source_type_idx'),
            models.Index(fields=['organization'], name='source_org_idx'),
        ]

    def save(self, *args, **kwargs):
        if self.pk is None and self.created_at is None:
            from django.utils import timezone
            self.created_at = timezone.now()
        if self._link_or_type_changed(kwargs.get("update_fields")):
            # Ein freigegebenes Script wurde für genau eine Seite geprüft. Zeigt
            # die Quelle auf eine andere URL oder hat einen anderen Typ, muss es
            # erneut freigegeben werden.
            self.import_script_approved_hash = ""
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"import_script_approved_hash"}
        super().save(*args, **kwargs)

    def _link_or_type_changed(self, update_fields) -> bool:
        if self.pk is None or not self.import_script_approved_hash:
            return False
        if update_fields is not None and not ({"link", "type"} & set(update_fields)):
            return False
        previous = Source.objects.filter(pk=self.pk).values("link", "type").first()
        if previous is None:
            return False
        return previous["link"] != self.link or previous["type"] != self.type

    def __str__(self):
        return self.name


def sanitize_filename(filename):
    # Regular expression to allow letters, numbers, hyphens, and underscores
    return re.sub(r'[^a-zA-Z0-9_-]', '_', filename)

def article_image_path(instance, filename):
    # Sanitize the filename to only include allowed characters
    filename = sanitize_filename(filename)
    
    # Check if the instance has an ID, otherwise generate a fallback path
    if instance.id is None:
        # Use a temporary ID or some other identifier to avoid the issue
        temp_id = 'temp'
        return f"upload/articles/{temp_id}-{filename}"
    
    # Once the instance has an ID, use the actual ID
    path = "upload/articles/{:08d}-{}".format(instance.id, filename)
    return path


class Article(models.Model):

    title = models.CharField(
        max_length=350, null=False, blank=False, verbose_name=_("title")
    )
    abstract = models.TextField(null=True, blank=True, verbose_name=_("abstract"))
    content = models.TextField(null=False, blank=True, verbose_name=_("content"))
    date = models.DateTimeField(default=localtime, blank=True, verbose_name=_("date"))
    moddate = models.DateTimeField(
        default=localtime, blank=True, verbose_name=_("moddate")
    )
    link = models.URLField(
        max_length=600, null=True, blank=True, verbose_name=_("link")
    )
    foreign_id = models.CharField(
        max_length=600, null=True, blank=True, unique=True, verbose_name=_("foreign_id")
    )
    image_url = models.URLField(
        max_length=1000, null=True, blank=True, verbose_name=_("image_url")
    )
    image = VersatileImageField(
        null=True,
        blank=True,
        upload_to=article_image_path,
        verbose_name=_("image_feed"),
    )
    image_detail = VersatileImageField(
        null=True,
        blank=True,
        upload_to=article_image_path,
        verbose_name=_("image_detail"),
    )
    image_source = models.CharField(
        max_length=600, null=True, blank=True, verbose_name=_("image_source")
    )
    address = models.CharField(
        max_length=300, null=False, blank=True, verbose_name=_("address")
    )
    tags = models.ManyToManyField(Tag, verbose_name=_("tags"))
    source = models.ForeignKey(
        Source, null=False, on_delete=models.CASCADE, verbose_name=_("source")
    )
    published = models.BooleanField(default=False, verbose_name=_("published"))
    reviewed = models.BooleanField(default=False, verbose_name=_("reviewed"))
    up_for_review = models.BooleanField(default=False, verbose_name=_("up for review"))
    draft = models.BooleanField(default=False, verbose_name=_("draft"))

    is_hot = models.BooleanField(default=False, verbose_name=_("is hot"))
    push_notification_sent = models.BooleanField(
        default=False, verbose_name=_("push notification sent")
    )
    push_notification_queued = models.BooleanField(
        default=False, verbose_name=_("push notification queued")
    )
    request_count = models.PositiveIntegerField(default=0, verbose_name=_("request count"))
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    # Technische Änderungsspur für Delta-Abrufe (changed_since). Bewusst getrennt
    # von moddate, das ein redaktionelles bzw. aus dem Feed übernommenes Datum ist.
    updated_at = models.DateTimeField(auto_now=True, null=True, db_index=True)
    # Einmal veröffentlicht, bleibt True. Nur solche Einträge kann ein Client je
    # gesehen haben - ohne die Unterscheidung würde jede Berührung eines nie
    # veröffentlichten Imports als "entfernt" an alle Clients gemeldet.
    was_published = models.BooleanField(default=False, db_index=True)
    ai_tagged = models.BooleanField(default=False, verbose_name=_("AI tagged"))
    area_defined = models.BooleanField(default=False, verbose_name=_("area defined"))

    area = models.ManyToManyField(Area, verbose_name=_("area"))
    language = models.CharField(
        max_length=5, choices=LANGUAGE_CHOICES, default="de", verbose_name=_("language")
    )
    translations = models.ManyToManyField(
        "self", symmetrical=True, blank=True, verbose_name=_("translations")
    )

    class Meta:
        verbose_name = _("article")
        verbose_name_plural = _("articles")
        indexes = [
            models.Index(fields=['published'], name='article_pub_idx'),
            models.Index(fields=['-date'], name='article_date_idx'),
  			models.Index(fields=['source'], name='article_source_idx'),
            models.Index(fields=["published", "source"], name="article_pub_source_idx"),
            models.Index(fields=["published", "-date", "id"], name="article_pub_date_id_idx"),
            models.Index(fields=["source", "published", "-date"], name="article_src_pub_date_idx"),
        ]

    def __str__(self):
        return self.title

    @staticmethod
    def classname():
        return "article"


class ArticleDraft(Article):
    class Meta:
        proxy = True
        verbose_name = _("article draft")
        verbose_name_plural = _("article drafts")


EVENT_BASE_PROPERTIES = [
    "title",
    "content",
    "date",
    "moddate",
    "image_url",
    "image",
    "image_source",
    "tags",
    "source",
    "zip_code",
    "address",
    "event_location",
    "published",
    "reviewed",
    "up_for_review",
    "recurring"
]



class Genre(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name=_("name"))

    class Meta:
        verbose_name = _("genre")
        verbose_name_plural = _("genres")
        ordering = ['name']

    def __str__(self):
        return self.name


class FilmLanguage(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name=_("name"))

    class Meta:
        verbose_name = _("film language")
        verbose_name_plural = _("film languages")
        ordering = ['name']

    def __str__(self):
        return self.name


SUBTITLE_CHOICES = [
    ('Deutsch', 'Deutsch'),
    ('Englisch', 'Englisch'),
    ('nicht gelistet', 'nicht gelistet'),
    ('ohne Untertitel', 'ohne Untertitel'),
]


class EventV4(models.Model):

    title = models.CharField(max_length=300, null=False, blank=False, verbose_name=_("title"))
    content = models.TextField(null=False, blank=True, verbose_name=_("content"))
    date = models.DateTimeField(default=localtime, blank=True, verbose_name=_("date"))
    start_date = models.DateTimeField(default=localtime, blank=True, verbose_name=_("start_date"))
    moddate = models.DateTimeField(auto_now=True, verbose_name=_("moddate"))
    link = models.URLField(max_length=600, null=True, blank=True, verbose_name=_("link"))
    foreign_id = models.CharField(max_length=300, null=True, blank=True, unique=True, verbose_name=_("foreign_id"))
    image_url = models.URLField(max_length=600, null=True, blank=True, verbose_name=_("image_url"))
    ticket_url = models.URLField(max_length=600, null=True, blank=True, verbose_name=_("ticket_url"))
    image = VersatileImageField(null=True,blank=True,upload_to=article_image_path,verbose_name=_("image_upload"),)
    image_source = models.CharField(max_length=200, null=True, blank=True, verbose_name=_("image_source"))
    tags = models.ManyToManyField(Tag, verbose_name=_("themes"))
    subtags = models.ManyToManyField(Subtag, verbose_name=_("subthemes"))
    is_past = models.BooleanField(default=False, db_index=True)
    event_links = models.ManyToManyField(EventLinks, verbose_name=_("links"))
    is_child = models.BooleanField(default=False)
    source = models.ForeignKey(Source, null=False, on_delete=models.CASCADE, verbose_name=_("source"))
    zip_code = models.CharField(max_length=10,null=True,blank=True,verbose_name=_("zip code"))
    street = models.CharField(max_length=300, null=False, blank=True, verbose_name=_("street"))
    town = models.CharField(max_length=300, null=False, blank=True, verbose_name=_("town"))
    event_location = models.CharField(max_length=100, null=True, blank=True, verbose_name=_("event location"))
    published = models.BooleanField(default=False, verbose_name=_("published"))
    reviewed = models.BooleanField(default=False, verbose_name=_("reviewed"))
    up_for_review = models.BooleanField(default=False, verbose_name=_("up for review"))
    draft = models.BooleanField(default=False, verbose_name=_("draft"))
    area = models.ManyToManyField(Area, verbose_name=_("area"))
    longitude = models.DecimalField(max_digits=22, decimal_places=16, blank=True, null=True)
    latitude = models.DecimalField(max_digits=22, decimal_places=16, blank=True, null=True)
    geocoding_failed = models.BooleanField(default=False, verbose_name=_("geocoding failed"))
    request_count = models.PositiveIntegerField(default=0, verbose_name=_("request count"))
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    # Technische Änderungsspur für Delta-Abrufe (changed_since). Anders als moddate
    # wird sie auch durch Occurrence-Änderungen angefasst (siehe content/signals.py).
    updated_at = models.DateTimeField(auto_now=True, null=True, db_index=True)
    # Siehe Article.was_published.
    was_published = models.BooleanField(default=False, db_index=True)
    ai_tagged = models.BooleanField(default=False, verbose_name=_("AI tagged"))
    push_notification_sent = models.BooleanField(
        default=False, verbose_name=_("push notification sent")
    )
    push_notification_queued = models.BooleanField(
        default=False, verbose_name=_("push notification queued")
    )
    area_defined = models.BooleanField(
        default=False, verbose_name=_("area defined")
    )
    genres = models.ManyToManyField(Genre, blank=True, verbose_name=_("genres"))
    film_languages = models.ManyToManyField(FilmLanguage, blank=True, verbose_name=_("film languages"))
    subtitle_language = models.CharField(
        max_length=50, choices=SUBTITLE_CHOICES, null=True, blank=True, verbose_name=_("subtitle language")
    )
    language = models.CharField(
        max_length=5, choices=LANGUAGE_CHOICES, default="de", verbose_name=_("language")
    )
    translations = models.ManyToManyField(
        "self", symmetrical=True, blank=True, verbose_name=_("translations")
    )

    class Meta:
        verbose_name = _("event")
        verbose_name_plural = _("events")
        indexes = [
            models.Index(fields=['published', 'is_past'], name='eventv4_pub_past_idx'),
        ]


class Event_Occurrence(models.Model):
    event = models.ForeignKey(EventV4, related_name='occurrences', on_delete=models.CASCADE, default='1')
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=['event', 'start_datetime'], name='occ_event_start_idx'),
            models.Index(fields=['start_datetime', 'end_datetime'], name='occ_daterange_idx'),
        ]

    def __str__(self):
        return f"{self.event.title} on {self.start_datetime}"


class ImportRecord(models.Model):
    """Buchführung eines Feed-Importers über die Events, die er selbst angelegt hat.

    Events ohne Eintrag stammen nicht aus dem Feed (in der App oder im Admin
    angelegt); der Importer ändert und löscht sie nicht. field_state hält je Feld
    den zuletzt importierten Stand. Weicht der aktuelle Wert davon ab, hat ihn
    jemand von Hand geändert: Das Feld steht dann in manual_fields und wird vom
    Feed nicht mehr überschrieben.
    """

    event = models.OneToOneField(EventV4, on_delete=models.CASCADE, related_name="import_record")
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="+")
    # Schlüssel des Feed-Eintrags beim letzten Import (Titel + Ort bzw. + Termin)
    key = models.CharField(max_length=500)
    field_state = models.JSONField(default=dict)
    manual_fields = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["source", "key"], name="importrec_source_key_idx")]

    def __str__(self):
        return f"ImportRecord {self.event_id} ({self.key})"


class EventDraftV4(EventV4):
    class Meta:
        proxy = True
        verbose_name = _("event draft")
        verbose_name_plural = _("event drafts")


DELETED_CONTENT_TYPE_CHOICES = (
    ("article", _("article")),
    ("event", _("event")),
)

# Wie lange Löschmarker aufbewahrt werden. Bestimmt zugleich, wie lange ein
# Delta-Client offline sein darf, bevor die API einen Vollsync verlangt.
TOMBSTONE_RETENTION_DAYS = 90


class DeletedContent(models.Model):
    """Grabstein für gelöschte Artikel/Events.

    Delta-Clients (GET /api/v4/organizations/content/?changed_since=...) erfahren
    sonst nie, dass ein Eintrag verschwunden ist. Die Organisation wird mitgeschrieben,
    weil sie sich nach dem Löschen nicht mehr über source ermitteln lässt - ohne sie
    ließen sich die Grabsteine nicht nach dem organizations-Parameter filtern.

    Einträge werden von "manage.py purge_tombstones" nach Ablauf der
    Aufbewahrungsfrist entfernt.
    """

    content_type = models.CharField(
        max_length=10, choices=DELETED_CONTENT_TYPE_CHOICES, verbose_name=_("content type")
    )
    object_id = models.IntegerField(verbose_name=_("object id"))
    organization = models.ForeignKey(
        "Organization",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        verbose_name=_("organization"),
    )
    language = models.CharField(
        max_length=5, null=True, blank=True, verbose_name=_("language")
    )
    # Titel des gelöschten Eintrags, damit Delta-Clients im Protokoll nicht nur
    # die Molo-ID anzeigen müssen. Grabsteine aus der Zeit vor diesem Feld
    # tragen einen leeren Titel - nachtragen lässt er sich nicht mehr.
    title = models.CharField(
        max_length=350, blank=True, default="", verbose_name=_("title")
    )
    deleted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("deleted content")
        verbose_name_plural = _("deleted content")
        indexes = [
            models.Index(
                fields=["organization", "content_type", "deleted_at"],
                name="deleted_org_type_date_idx",
            ),
        ]

    def __str__(self):
        return "{} {} (deleted {})".format(self.content_type, self.object_id, self.deleted_at)




class Event(models.Model):

    title = models.CharField(
        max_length=300, null=False, blank=False, verbose_name=_("title")
    )
    content = models.TextField(null=False, blank=True, verbose_name=_("content"))
    date = models.DateTimeField(default=localtime, blank=True, verbose_name=_("date"))
    moddate = models.DateTimeField(
        default=localtime, blank=True, verbose_name=_("moddate")
    )
    link = models.URLField(
        max_length=600, null=True, blank=True, verbose_name=_("link")
    )
    foreign_id = models.CharField(
        max_length=300, null=True, blank=True, unique=True, verbose_name=_("foreign_id")
    )
    image_url = models.URLField(
        max_length=300, null=True, blank=True, verbose_name=_("image_url")
    )
    image = VersatileImageField(
        null=True,
        blank=True,
        upload_to=article_image_path,
        verbose_name=_("image_upload"),
    )
    image_source = models.CharField(
        max_length=200, null=True, blank=True, verbose_name=_("image_source")
    )
    tags = models.ManyToManyField(Tag, verbose_name=_("themes"))

    # using the links events can be connected to other events
    event_links = models.ManyToManyField(EventLinks, verbose_name=_("links"))

    source = models.ForeignKey(
        Source, null=False, on_delete=models.CASCADE, verbose_name=_("source")
    )
    zip_code = models.CharField(
        max_length=5,
        null=True,
        blank=True,
        verbose_name=_("zip code")
    )

    address = models.CharField(
        max_length=300, null=False, blank=True, verbose_name=_("event address")
    )
    street = models.CharField(
        max_length=300, null=False, blank=True, verbose_name=_("event address")
    )
    town = models.CharField(
        max_length=300, null=False, blank=True, verbose_name=_("event address")
    )

    event_location = models.CharField(
        max_length=100, null=True, blank=True, verbose_name=_("event location")
    )

    event_date = models.DateTimeField(default=localtime, verbose_name=_("event date"))
    event_end_date = models.DateTimeField(
        null=True, blank=True, verbose_name=_("event end date")
    )

    recurring = models.IntegerField(default=0, choices=RECURRING_EVENT_CHOICES, verbose_name=_("recurring"))

    recurrences = RecurrenceField(null=True, blank=True, verbose_name=_('recurrences'))

    recurring_event_end_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("recurring_event_end_date"),
        default=(localtime() + timedelta(days=365)).date(),
    )

    # TODO migrate is_child to EventChild
    is_child = models.BooleanField(default=False)
    auto_generated = models.BooleanField(default=False)
    # parent_id = models.IntegerField(null=True, blank=True)

    published = models.BooleanField(default=False, verbose_name=_("published"))
    reviewed = models.BooleanField(default=False, verbose_name=_("reviewed"))
    up_for_review = models.BooleanField(default=False, verbose_name=_("up for review"))
    draft = models.BooleanField(default=False, verbose_name=_("draft"))

    area = models.ManyToManyField(Area, verbose_name=_("area"))


    class Meta:
        verbose_name = _("event")
        verbose_name_plural = _("events")

    @staticmethod
    def classname():
        return "event"

    def clean(self):
        errors = {}
        # TODO needed?
        if self.is_child:
            # obey unique constraints
            self.link = None

        if self.recurring == 1:
            if not self.recurring_event_end_date:
                errors["recurring_event_end_date"] = _(
                    "An end date for a recurring event must be set."
                )
            elif not self.recurring_event_end_date > self.event_date.date():
                errors["recurring_event_end_date"] = _(
                    "An end date must be bigger than a start date."
                )

        if errors:
            raise ValidationError(errors)

    def save_related_changes(self, changes):
        _changes = False
        for property, value in changes.items():
            if not getattr(self, property) == value:
                _changes = True
                if property == "tags":
                    self.tags.set(value.all())
                elif property == "auto_generated" and self.is_child and getattr(changes, 'recurring') == 1:
                    self.auto_generated = True
                else:
                    setattr(self, property, value)
        if _changes:
            self.save()


class EventDraft(Event):
    class Meta:
        proxy = True
        verbose_name = _("event draft")
        verbose_name_plural = _("event drafts")


class EventChild(models.Model):

    class Meta:
        verbose_name = _("sub event")
        verbose_name_plural = _("sub events")

    parent = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="event_child_parent"
    )
    event = models.ForeignKey(
        Event, null=True, on_delete=models.CASCADE, related_name="event_child_event"
    )
    event_date = models.DateTimeField(default=localtime, verbose_name=_("event date"))
    event_end_date = models.DateTimeField(
        null=True, blank=True, verbose_name=_("event end date")
    )
    auto_generated = models.BooleanField(default=False)

    def save_related_changes(self, changes):
        _changes = False
        self.parent_changes = changes

        if not self.event:
            _changes = True
            self.event = Event(
                **{prop: value for prop, value in changes.items() if not prop == 'tags'},
                auto_generated=True,
                is_child=True,
                draft=False,
            )
            self.event.save()
            self.event.tags.set(changes['tags'].all())
            self.event.save()
        for event_property, value in changes.items():
            if hasattr(self, event_property) and not getattr(self, event_property) == value:
                _changes = True
                setattr(self, event_property, value)
        if _changes:
            super().save()
        else:
            # propagate other parent changes
            self.event.save_related_changes(changes)


@receiver(post_delete, sender=EventChild)
def delete_connected(sender, instance, **kwargs):
    try:
        if instance.event:
            instance.event.delete()
    except Event.DoesNotExist:
        pass


@receiver(post_save, sender=EventChild)
def save_related_changes_on_event(sender, instance, created, **kwargs):
    if instance.event:
        changes = {
            "event_date": instance.event_date,
            "event_end_date": instance.event_end_date,
        }
        if hasattr(instance, 'parent_changes'):
            changes = {**instance.parent_changes, **changes}
        instance.event.save_related_changes(changes)
    else:
        if created:
            data = {
                property: getattr(instance.parent, property)
                for property in EVENT_BASE_PROPERTIES
                if not property == "tags"
            }
            data["event_date"] = instance.event_date
            data["event_end_date"] = instance.event_end_date
            data["auto_generated"] = True
            data["is_child"] = True
            new_event = Event(**data)
            new_event.save()
            new_event.tags.set(instance.parent.tags.all())
            new_event.save()

            instance.event = new_event
            instance.save()


def get_child_events(parent):
    childs = EventChild.objects.filter(parent__pk=parent.id)

    # include last occurrences
    recurring_end = datetime.combine(
        parent.recurring_event_end_date, datetime.max.time()
    )
    recurring_start = datetime.combine(parent.event_date.date(), datetime.min.time())
    # get all occurrences, including past ones
    expected_childs = parent.recurrences.between(
        recurring_start, recurring_end, dtstart=recurring_start
    )

    missing_childs = [
        child
        for child in expected_childs
        if child.date() not in [event.event_date.date() for event in childs]
    ]
    redundant_childs = [
        event
        for event in childs
        if event.event_date.date() not in [_child.date() for _child in expected_childs]
    ]
    return childs, expected_childs, missing_childs, redundant_childs


def save_related_changes_on_child_event(sender, instance=None, **kwargs):
    if instance.is_child:
        changes = {
            "event_date": instance.event_date,
            "event_end_date": instance.event_end_date,
        }
        try:
            _child = EventChild.objects.get(event=instance)
            _child.save_related_changes(changes)
        except EventChild.DoesNotExist:
            pass
    else:
        data = {
            property: getattr(instance, property)
            for property in EVENT_BASE_PROPERTIES
        }

        if instance.recurring == 0 or instance.draft:
            _childs = EventChild.objects.filter(parent__id=instance.id)
            for _child in _childs:
                _child.delete()

        if instance.recurring == 1 and not instance.draft:
            (
                existing_childs,
                expected_childs,
                missing_childs,
                redundand_childs,
            ) = get_child_events(instance)

            if redundand_childs:
                # remove redundand child events
                for _child in redundand_childs:
                    _child.delete()
                existing_childs = EventChild.objects.filter(parent__id=instance.id)

            if existing_childs or missing_childs:
                start_time = instance.event_date.time()
                end_delta = None
                if instance.event_end_date:
                    end_delta = instance.event_end_date - instance.event_date

                if existing_childs:
                    # update existing child events
                    for child in existing_childs:
                        changes = data
                        changes['event_date'] = child.event_date.replace(
                            hour=start_time.hour, minute=start_time.minute
                        )
                        changes['event_end_date'] = None
                        if end_delta:
                            changes['event_end_date'] = child.event_date + end_delta
                        child.save_related_changes(changes)

                if missing_childs:
                    # create new child events
                    for child_date in expected_childs:
                        child_start_date = make_aware(
                            child_date.replace(
                                hour=start_time.hour, minute=start_time.minute
                            )
                        )
                        child_end_date = None
                        if end_delta:
                            child_end_date = child_start_date + end_delta
                        _child = EventChild(
                            parent=instance,
                            event_date=child_start_date,
                            event_end_date=child_end_date,
                            auto_generated=True,
                        )
                        _child.save()
        if instance.recurring == 2:
            # update existing childs
            existing_childs = EventChild.objects.filter(parent__id=instance.id)
            for child in existing_childs:
                child.save_related_changes(data)


#connect to signal
EVENT_SAVED.connect(save_related_changes_on_child_event)


class AppUser(models.Model):

    device_id = models.CharField(max_length=300, blank=False, null=False, unique=True)
    bookmarked_articles = models.ManyToManyField(
        Article,
        blank=True,
        related_name="articles_bookmarked",
    )
    archived_articles = models.ManyToManyField(
        Article, blank=True, related_name="articles_archived"
    )
    bookmarked_events = models.ManyToManyField(
        Event,
        blank=True,
        related_name="events_bookmarked",
    )
    archived_events = models.ManyToManyField(
        Event, blank=True, related_name="events_archived"
    )
    bookmarked_events_v4 = models.ManyToManyField(
        EventV4,
        blank=True,
        related_name="events_bookmarked",
    )
    archived_events_v4 = models.ManyToManyField(
        EventV4, blank=True, related_name="events_archived"
    )

    tags = models.ManyToManyField(Tag, blank=True, related_name="appusers_tags")

    organization = models.ManyToManyField(
        Organization, blank=True, related_name="appusers_organizations"
    )
    deselected_organization = models.ManyToManyField(
        Organization, blank=True, related_name="appusers_deselected_organizations"
    )

    organization_all_tags = models.ManyToManyField(
        Organization, blank=True, related_name="appusers_organization_all_tags"
    )
    deselected_organization_all_tags = models.ManyToManyField(
        Organization,
        blank=True,
        related_name="appusers_deselected_organization_all_tags",
    )

    ordering = models.CharField(max_length=100, blank=False, default="-date")

    location_latitude = models.DecimalField(
        max_digits=22, decimal_places=16, blank=True, null=True
    )
    location_longitude = models.DecimalField(
        max_digits=22, decimal_places=16, blank=True, null=True
    )
    location_radius = models.DecimalField(
        max_digits=11, decimal_places=5, blank=True, null=True
    )

    #selected area
    area = models.ForeignKey(Area, null=True, blank=True, on_delete=models.SET_NULL, verbose_name=_("area"))
    areas = models.ManyToManyField(Area, blank=True, related_name="appusers_areas")

    # push
    firebase_token = models.CharField(max_length=300, blank=True, null=True)
    push_news = models.BooleanField(default=False)
    push_collective = models.BooleanField(default=False)
    push_official = models.BooleanField(default=False, null=True)

    # event sources
    filter_events_by_source = models.BooleanField(default=True)

    # New field to store when the user joined
    date_joined = models.DateTimeField(auto_now_add=True, verbose_name=_("joined date"))


    class Meta:
        verbose_name = _("appuser")
        verbose_name_plural = _("appusers")


class User(AbstractUser):
    # for contributors
    email = models.EmailField(blank=True, null=True, unique=True)
    sources = models.ManyToManyField(Source, blank=True)
    can_publish = models.BooleanField(default=False)
    can_modify_organization_info = models.BooleanField(default=False)
    area = models.ManyToManyField(Area, verbose_name=_("area"))
    phone = models.CharField(max_length=20, blank=True, null=True)
    activation_key = models.CharField(max_length=100, blank=True, null=True)
    # SHA-256 des Reset-Tokens aus der Mail (das Token selbst wird nicht gespeichert)
    password_reset_token = models.CharField(max_length=100, blank=True, null=True)
    # Ausgabezeitpunkt des Reset-Tokens; es verfällt nach PASSWORD_RESET_TIMEOUT
    password_reset_requested_at = models.DateTimeField(null=True, blank=True)
    # API-Tokens, die vor diesem Zeitpunkt ausgestellt wurden, sind ungültig
    # (wird bei jeder Passwortänderung gesetzt)
    tokens_valid_after = models.DateTimeField(null=True, blank=True)
    # Ausnahme davon: das Access-Token (jti), mit dem das Passwort per API
    # geändert wurde, damit das aufrufende Gerät angemeldet bleibt
    token_exempt_jti = models.CharField(max_length=64, blank=True, default="")
    display_name = models.CharField(max_length=100, blank=True, null=True, unique=True)
    profile_image = models.ImageField(
        upload_to='upload/profile/', null=True, blank=True, verbose_name=_("profile image")
    )

    PASSWORD_CHANGE_RESET_FIELDS = (
        "tokens_valid_after", "token_exempt_jti", "password_reset_token", "password_reset_requested_at",
    )

    def save(self, *args, **kwargs):
        # Leere E-Mail als NULL speichern: email ist eindeutig, mehrere Konten
        # ohne Adresse (z. B. createsuperuser) dürfen nicht kollidieren
        if self.email == "":
            self.email = None
        # Echte Passwortänderung (set_password, nicht nur ein Hash-Upgrade beim
        # Login): alle API-Tokens und offenen Reset-Links widerrufen. Gilt für
        # API, Redaktionsoberfläche, Django-Admin und changepassword.
        if self.pk is not None and getattr(self, "_password", None) is not None:
            self.tokens_valid_after = timezone.now()
            self.token_exempt_jti = ""
            self.password_reset_token = None
            self.password_reset_requested_at = None
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | set(self.PASSWORD_CHANGE_RESET_FIELDS)
        super().save(*args, **kwargs)

    @property
    def is_contributor(self):
        return self.groups.filter(id=2).exists()

    @property
    def is_editor(self):
        return self.groups.filter(id=1).exists()
