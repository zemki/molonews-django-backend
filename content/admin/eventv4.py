from django.conf import settings
from django.contrib import admin, messages
from ..models import EventV4, Tag, Event_Occurrence, Genre, FilmLanguage
from ..utils.area_creation import get_or_create_area
from django.utils.translation import gettext_lazy as _
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut
from django import forms
from django_ckeditor_5.widgets import CKEditor5Widget
import time
from logging import getLogger
from django.db.models import Q, OuterRef, Subquery, DateTimeField
from django.forms import CharField, Textarea
from django.utils import timezone
from django.core.exceptions import ValidationError

from .mixins import OwnContentAdminMixin

logger = getLogger(__name__)


class UpcomingEventFilter(admin.SimpleListFilter):
    title = 'Hat zukünftige Termine'
    parameter_name = 'upcoming_occurrence'

    def lookups(self, request, model_admin):
        return (
            ('yes', 'Nur Events mit zukünftigen Terminen'),
            ('no',  'Nur Events ohne zukünftige Termine'),
        )

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == 'yes':
            return queryset.filter(
                occurrences__end_datetime__gte=now
            ).distinct()
        elif self.value() == 'no':
            return queryset.filter(
                occurrences__end_datetime__lt=now
            ).distinct()
        return queryset


class EventOccurrenceInline(admin.TabularInline):
    model = Event_Occurrence
    extra = 1
    fields = ('start_datetime', 'end_datetime')
    show_change_link = True
    verbose_name = "Termin"
    verbose_name_plural = "Termine"

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == 'start_datetime':
            formfield.label = "Startdatum und Uhrzeit"
        if db_field.name == 'end_datetime':
            formfield.label = "Enddatum und Uhrzeit"
        return formfield


def validate_image_size(image):
    max_size = 5 * 1024 * 1024  # 5MB limit
    if image.size > max_size:
        raise ValidationError(f"Das Bild ist zu groß. Maximale Größe: {max_size / (1024 * 1024)} MB.")


class EventV4Form(forms.ModelForm):
    content = forms.CharField(widget=CKEditor5Widget(config_name="default"), label=_('Inhalt'))
    image = forms.ImageField(required=False, validators=[validate_image_size])

    class Meta:
        model = EventV4
        fields = '__all__'

    title = CharField(
        widget=Textarea(attrs={"cols": 80, "rows": 1}),
        label=_('title')
    )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)
        self.fields['subtags'].required = False
        self.fields['tags'].required = False
        self.fields['area'].required = False
        self.fields['area'].help_text = (
            "Wird automatisch aus Stadt/PLZ ermittelt, wenn leer gelassen."
        )

    def clean(self):
        cleaned_data = super().clean()
        if self.request and self.request.user.groups.filter(name='Contributors').exists():
            user_source = self.request.user.sources.first()
            if user_source:
                cleaned_data['source'] = user_source
        return cleaned_data


@admin.register(EventV4)
class EventV4Admin(OwnContentAdminMixin, admin.ModelAdmin):
    form = EventV4Form
    queryset_filter = None

    change_form_template = "admin/change_eventv4_form.html"

    list_display = ('id', 'title', 'start_date', 'next_occurrence', 'source', 'event_location', 'language', 'published', 'reviewed', 'draft' , "request_count_display")
    list_filter = ('published', 'source', UpcomingEventFilter, 'reviewed', 'draft', 'language', 'tags', 'area')
    search_fields = ('title', 'content', 'street', 'town', 'zip_code')
    date_hierarchy = 'start_date'
    readonly_fields = ('request_count', 'longitude', 'latitude', 'ai_tagged_display')
    filter_horizontal = ('tags', 'area', 'genres', 'film_languages')
    raw_id_fields = ('translations',)

    def request_count_display(self, obj):
        return obj.request_count
    request_count_display.short_description = "Views"
    request_count_display.admin_order_field = "request_count"

    def ai_tagged_display(self, obj):
        if obj.ai_tagged:
            return "Durch KI zugeordnet"
        return "–"
    ai_tagged_display.short_description = "KI-Tagging Status"

    # Wichtig: ordering hier nicht setzen, sondern über get_ordering!

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "tags":
            kwargs["queryset"] = Tag.objects.filter(category_id=2)
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    fieldsets = (
        (None, {
            'fields': ('title', 'content', 'source', 'link', 'ticket_url', 'image_url', 'image', 'image_source', 'published', 'language', 'translations', 'tags', 'ai_tagged_display', 'subtags', 'area')
        }),
        ('Filminformationen', {
            'fields': ('genres', 'film_languages', 'subtitle_language'),
            'classes': ('collapse',),
        }),
        ('Veranstaltungsort', {
            'fields': ('event_location', 'street', 'zip_code', 'town')
        }),
    )

    EventV4._meta.get_field("subtags").verbose_name = "Weitere Themen"

    inlines = [
        EventOccurrenceInline,
    ]

    def get_queryset(self, request):
        qs = super().get_queryset(request)

  


        # --- NEXT OCCURRENCE ANNOTATION HIER ---
        now = timezone.now()
        next_occurrence_subquery = (
            Event_Occurrence.objects
            .filter(
                event=OuterRef('pk'),
                end_datetime__gte=now
            )
            .order_by('start_datetime')
            .values('start_datetime')[:1]
        )
        qs = qs.annotate(
            next_occurrence_date=Subquery(next_occurrence_subquery, output_field=DateTimeField())
        )
        # ---------------------------------------

        if self.queryset_filter:
            qs = qs.filter(Q(**self.queryset_filter))

        # Nicht-Superuser: nur Events der eigenen Quellen
        qs = self.restrict_to_own_sources(request, qs)

        # Ordering nach annotiertem Feld – muss NACH der Annotation passieren
        qs = qs.order_by('next_occurrence_date')

        return qs.distinct()

    def get_ordering(self, request):
        # Sicheres Default-Ordering für super().get_queryset(),
        # das eigentliche Ordering nach next_occurrence_date passiert in get_queryset()
        return ('-start_date',)

    def next_occurrence(self, obj):
        # Holt das annotierte Feld (oder Fallback)
        val = getattr(obj, 'next_occurrence_date', None)
        if val:
            return val
        return "–"
    next_occurrence.admin_order_field = 'next_occurrence_date'
    next_occurrence.short_description = "Nächster Termin"

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        # Filminformationen nur anzeigen, wenn welche gepflegt sind
        has_film_info = obj and obj.pk and (
            obj.genres.exists() or obj.film_languages.exists() or getattr(obj, 'subtitle_language', None)
        )
        if not has_film_info:
            fieldsets = [fs for fs in fieldsets if fs[0] != 'Filminformationen']
        if request.user.groups.filter(name='Contributors').exists():
            new_fieldsets = []
            for fieldset in fieldsets:
                title, field_options = fieldset
                fields = field_options.get('fields', [])
                fields = tuple(field for field in fields if field != 'source')
                new_fieldsets.append((title, {'fields': fields}))
            return new_fieldsets
        return fieldsets

    def render_change_form(self, request, context, add=False, change=False, form_url='', obj=None):
        form = context['adminform'].form
        if request.user.groups.filter(name='Contributors').exists() and 'source' in form.fields:
            user_source = request.user.sources.first()
            if user_source:
                form.fields['source'].initial = user_source
                form.fields['source'].disabled = True
                form.fields['source'].required = False

        if 'source' in form.fields:
            form.fields['source'].widget.can_add_related = False
            form.fields['source'].widget.can_change_related = False
            form.fields['source'].widget.can_delete_related = False
        if 'tags' in form.fields:
            form.fields['tags'].widget.can_add_related = False
            form.fields['tags'].widget.can_change_related = False
            form.fields['tags'].widget.can_delete_related = False
        if 'subtags' in form.fields:
            form.fields['subtags'].widget.can_add_related = False
            form.fields['subtags'].widget.can_change_related = False
            form.fields['subtags'].widget.can_delete_related = False

        context.update({
            'show_save_and_continue': False,
            'show_save_as_draft': False,
            'show_delete_link': True,
            'show_save_and_add_another': False
        })
        return super().render_change_form(request, context, add=add, change=change, form_url=form_url, obj=obj)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == 'source' and request.user.groups.filter(name='Contributors').exists():
            formfield.required = False
        return formfield

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.base_fields['event_location'].label = "Name Veranstaltungsort"
        return form

    def save_model(self, request, obj, form, change):
        address = f"{obj.street}, {obj.town}, {obj.zip_code}"
        obj.latitude = 0.00
        obj.longitude = 0.00
        attempts = 5
        for attempt in range(attempts):
            try:
                geolocator = Nominatim(user_agent=settings.GEOCODER_USER_AGENT)
                location = geolocator.geocode(address, timeout=10)
                if location:
                    obj.latitude = location.latitude
                    obj.longitude = location.longitude
                    break
            except GeocoderTimedOut:
                logger.error("Geocoding TimeOut for address: %s, attempt: %d", address, attempt + 1)
                if attempt < attempts - 1:
                    time.sleep(2)
            except Exception as e:
                logger.error("Geocoding failed for address: %s, error: %s", address, str(e))
                if attempt < attempts - 1:
                    time.sleep(2)

        if request.user.groups.filter(name='Contributors').exists() and not request.user.is_superuser:
            user_source = request.user.sources.first()
            if user_source:
                obj.source = user_source

        # Nur eigene Quellen; veröffentlichen nur mit Veröffentlichungsrecht
        self.enforce_content_rules(request, obj)

        temp_image = form.cleaned_data.get('image', None)
        if obj.pk is None:
            form.cleaned_data['image'] = None
            super().save_model(request, obj, form, change)
            if temp_image:
                obj.image = temp_image
                obj.save()
        else:
            super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        if obj.area.exists():
            return
        if not (obj.town or obj.zip_code):
            messages.warning(
                request,
                "Kein Gebiet zugewiesen: weder Stadt noch PLZ angegeben. "
                "Bitte Adresse ergänzen oder Gebiet manuell auswählen.",
            )
            return
        try:
            area, _ = get_or_create_area(
                obj.town,
                zip_code=obj.zip_code,
                latitude=obj.latitude,
                longitude=obj.longitude,
                create_if_not_found=False,
            )
        except Exception as e:
            logger.error("Auto-area assignment failed for event %s: %s", obj.pk, e)
            messages.warning(
                request,
                "Gebiet konnte nicht automatisch ermittelt werden. "
                "Bitte manuell auswählen.",
            )
            return
        if area:
            obj.area.add(area)
            obj.area_defined = True
            obj.save(update_fields=["area_defined"])
            messages.info(request, f'Gebiet automatisch zugewiesen: {area.name}')
        else:
            messages.warning(
                request,
                f'Kein passendes Gebiet für "{obj.town}" '
                f'(PLZ {obj.zip_code or "—"}) gefunden. Bitte manuell auswählen.',
            )


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)


@admin.register(FilmLanguage)
class FilmLanguageAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)
