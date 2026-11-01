from logging import getLogger
from django.contrib import admin
from django.conf import settings
from django.forms import CharField
from django.utils.translation import gettext_lazy as _
from django.forms import Textarea
from django.core.exceptions import ValidationError
from django_ckeditor_5.widgets import CKEditor5Widget
from django import forms

from content.permissions import can_publish

from .article_event_shared import (
    ArticleEventForm,
    ArticleEventAdmin,
    ArticleEventChangeListForm,
)

logger = getLogger("molonews")


class ArticleChangeListForm(ArticleEventChangeListForm):
    tag_category_filter = [1, 3, 4]


def validate_image_size(image):
    max_size = 5 * 1024 * 1024  # 5MB limit
    if image.size > max_size:
        raise ValidationError(f"Das Bild ist zu groß. Maximale Größe: {max_size / (1024 * 1024)} MB.")

class ArticleForm(ArticleEventForm):
    tag_category_filter = [1, 3, 4]
    abstract = CharField(widget=CKEditor5Widget(config_name="default"), label=_('Inhalt'))
    image = forms.ImageField(required=False, validators=[validate_image_size])

    # Verlängere das Title-Feld
    title = CharField(
        widget=Textarea(attrs={"cols": 80, "rows": 1}),  
        label=_('title')
    )

    # Schließe das content-Feld explizit aus
    class Meta:
        exclude = ['content']


class ArticleAdmin(ArticleEventAdmin):
    list_display = (
        "id",
        "date",
        "title",
        "abstract",
        "tags_display",
        "areas_display",
        "reviewed",
        "published",
        "is_hot",
        "language",
        "source",
        "request_count_display",
    )
    list_filter = (
        "date",
        "published",
        "reviewed",
        "language",
        "area",
        ("source", admin.RelatedOnlyFieldListFilter),
    )
    search_fields = ("title", "abstract")
    list_editable = ("reviewed", "published")
    readonly_fields = ("date", "draft", "request_count_display")
    raw_id_fields = ("translations",)
    ordering = (
        "-draft",
        "-date",
    )
    # exclude = ()
    form = ArticleForm
    changeListForm = ArticleChangeListForm
    queryset_filter = {"draft": False}

    # Custom method to display tags
    def tags_display(self, obj):
        return ", ".join([tag.name for tag in obj.tags.all()])

    tags_display.short_description = 'Tags'

    # Custom method to display areas
    def areas_display(self, obj):
        return ", ".join([area.name for area in obj.area.all()])

    areas_display.short_description = 'Areas'

    # Custom method to return request_count with a label
    def request_count_display(self, obj):
        return obj.request_count

    # Set the custom label (short_description)
    request_count_display.short_description = 'Views'



    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if 'content' in fields:
            fields.remove('content')  
        if 'address' in fields:
            fields.remove('address')  
        if 'request_count' in fields:
            fields.remove('request_count') 
        return fields
    

    def save_hook(self, request, obj, form, change):

        published_value = form.cleaned_data.get('published')
        if published_value is not None:
            # Veröffentlichen nur mit Veröffentlichungsrecht
            obj.published = bool(published_value) and can_publish(request.user)

        if (
            obj.is_hot
            and obj.published
            and getattr(settings, 'ENABLE_PUSH', False)
            and hasattr(settings, 'FIREBASE_SERVICE_ACCOUNT_KEY')
            and not obj.push_notification_sent
        ):
            if not request.user.is_contributor:
                obj.push_notification_queued = True
                logger.error("Article {} queued for push notification".format(obj.id))
            else:
                logger.error("Contributors can't send push messages.")
        # return obj
        obj.save()

class ArticleDraftAdmin(ArticleAdmin):
    queryset_filter = {"draft": True}
    list_display = (
        "id",
        "date",
        "title",
        "areas_display",
        "source",
        "reviewed",
        "published",
    )
