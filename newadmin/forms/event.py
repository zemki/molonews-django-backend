from django.db.models import Q
from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory
from django_ckeditor_5.widgets import CKEditor5Widget

from content.models import EventV4, Event_Occurrence, Tag, Subtag, Area
from content.permissions import allowed_sources
from ..constants import AREA_MAX_NON_ADMIN


def validate_image_size(image):
    max_size = 5 * 1024 * 1024
    if hasattr(image, 'size') and image.size > max_size:
        raise ValidationError("Das Bild ist zu gross. Maximale Groesse: 5 MB.")


class EventForm(forms.ModelForm):
    """EventV4 form with role-based field visibility and CKEditor integration."""

    content = forms.CharField(
        widget=CKEditor5Widget(config_name="default"),
        label="Inhalt",
        required=True,
    )

    title = forms.CharField(
        widget=forms.Textarea(attrs={
            'rows': 2,
            'class': 'form-input form-input--title',
            'placeholder': 'Event-Titel eingeben...',
        }),
        label="Titel",
    )

    image = forms.ImageField(
        required=False,
        validators=[validate_image_size],
        label="Bild",
        widget=forms.ClearableFileInput(attrs={'class': 'form-file'}),
    )

    translation_ids = forms.CharField(
        required=False,
        label="Verknüpfte Übersetzung (Event-ID)",
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'z.B. 1234 (mehrere per Komma)',
        }),
        help_text="ID des Events in der anderen Sprache. Die Verknüpfung gilt automatisch in beide Richtungen.",
    )

    class Meta:
        model = EventV4
        fields = [
            'title', 'content', 'source', 'link', 'ticket_url',
            'image', 'image_url', 'image_source',
            'tags', 'subtags', 'area',
            'event_location', 'street', 'zip_code', 'town',
            'language',
        ]
        widgets = {
            'link': forms.URLInput(attrs={'class': 'form-input', 'placeholder': 'https://...'}),
            'ticket_url': forms.URLInput(attrs={'class': 'form-input', 'placeholder': 'https://...'}),
            'image_url': forms.URLInput(attrs={'class': 'form-input', 'placeholder': 'https://...'}),
            'image_source': forms.TextInput(attrs={'class': 'form-input'}),
            'source': forms.Select(attrs={'class': 'form-select'}),
            'event_location': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Name des Veranstaltungsortes'}),
            'street': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Strasse'}),
            'zip_code': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'PLZ'}),
            'town': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Stadt'}),
            'language': forms.Select(attrs={'class': 'form-select'}),
        }

    # Felder, die zur App-Auslieferung & Geocoding zwingend nötig sind.
    REQUIRED_EXTRA_FIELDS = (
        'event_location', 'street', 'zip_code', 'town',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.area_max = self._compute_area_max()

        # Bestehende Übersetzungs-Verknüpfungen als IDs anzeigen
        if self.instance.pk:
            linked_ids = self.instance.translations.order_by('id').values_list('id', flat=True)
            self.initial['translation_ids'] = ", ".join(str(pk) for pk in linked_ids)

        # Event tags: category_id = 2. Per Regel vergebene Tags
        # stehen nicht zur Auswahl, sie bleiben beim Speichern erhalten (_save_m2m).
        tag_qs = Tag.objects.filter(category_id=2, assigned_by_rule=False).order_by('name')
        self.fields['tags'].queryset = tag_qs
        self.fields['tags'].widget = forms.SelectMultiple(
            attrs={'class': 'tom-select-multi'}
        )
        self.fields['tags'].choices = [(t.pk, t.name) for t in tag_qs]
        self.fields['tags'].required = True

        # Subtags
        subtag_qs = Subtag.objects.all().order_by('name')
        self.fields['subtags'].queryset = subtag_qs
        self.fields['subtags'].widget = forms.SelectMultiple(
            attrs={'class': 'tom-select-multi'}
        )
        self.fields['subtags'].choices = [(s.pk, s.name) for s in subtag_qs]
        self.fields['subtags'].required = False
        self.fields['subtags'].label = "Weitere Themen"

        # Area
        area_qs = Area.objects.filter(visible=True).order_by('name')
        self.fields['area'].queryset = area_qs
        area_attrs = {'class': 'tom-select-multi'}
        if self.area_max is not None:
            area_attrs['data-max-items'] = str(self.area_max)
        self.fields['area'].widget = forms.SelectMultiple(attrs=area_attrs)
        self.fields['area'].choices = [(a.pk, a.name) for a in area_qs]
        self.fields['area'].required = True

        # Veranstaltungsort + Adresse (Geocoding-Voraussetzung)
        for field_name in self.REQUIRED_EXTRA_FIELDS:
            if field_name in self.fields:
                self.fields[field_name].required = True

        # Source explizit als Pflicht markieren (ModelForm leitet das aus null=False ab,
        # wir machen es sichtbar für Templates).
        if 'source' in self.fields:
            self.fields['source'].required = True

        if not user:
            return

        # Default area from user (for new events)
        if not self.instance.pk:
            user_area_ids = list(user.area.values_list('id', flat=True))
            if user_area_ids:
                self.initial['area'] = user_area_ids

        # Quellen: nur eigene (Superuser: alle). Wer genau eine Quelle hat, bekommt
        # sie in der View automatisch gesetzt -> Feld entfernen.
        own_sources = allowed_sources(user)
        if 'source' in self.fields and not user.is_superuser:
            self.fields['source'].queryset = own_sources
        if user.is_contributor and not user.is_superuser and own_sources.count() <= 1:
            for field_name in ['source', 'image_source']:
                if field_name in self.fields:
                    del self.fields[field_name]

    def _compute_area_max(self):
        """Effective per-user upper bound. None means no limit (superuser).

        Grandfathers existing records: if an admin previously assigned more
        than AREA_MAX_NON_ADMIN areas, the non-admin may keep (or reduce)
        that count but not add beyond it.
        """
        user = self.user
        if not user or user.is_superuser:
            return None
        existing = self.instance.area.count() if self.instance.pk else 0
        return max(AREA_MAX_NON_ADMIN, existing)

    def clean_area(self):
        areas = self.cleaned_data.get('area')
        if self.area_max is None or areas is None:
            return areas
        if len(areas) > self.area_max:
            raise ValidationError(
                f"Maximal {self.area_max} Gebiete auswählbar. "
                f"Um andere Gebiete zu wählen, entfernen Sie zuerst bestehende Gebiete."
            )
        return areas

    def clean_translation_ids(self):
        """Kommagetrennte IDs validieren und als Liste von ints zurückgeben."""
        raw = self.cleaned_data.get('translation_ids') or ''
        ids = []
        for part in raw.replace(';', ',').split(','):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise ValidationError(f"Ungültige ID: '{part}' — bitte nur Zahlen eingeben.")
            ids.append(int(part))
        if self.instance.pk and self.instance.pk in ids:
            raise ValidationError("Ein Event kann nicht mit sich selbst verknüpft werden.")
        candidates = EventV4.objects.filter(id__in=ids)
        # Nur eigene Inhalte verknüpfen (translations ist symmetrisch und würde sonst
        # auch Inhalte anderer Organisationen verändern)
        if not (self.user and self.user.is_superuser):
            already_linked = (
                self.instance.translations.values_list('id', flat=True) if self.instance.pk else []
            )
            own = Q(source__in=allowed_sources(self.user)) if self.user else Q(pk__in=[])
            candidates = candidates.filter(own | Q(pk__in=list(already_linked))).distinct()
        existing = set(candidates.values_list('id', flat=True))
        missing = [str(i) for i in ids if i not in existing]
        if missing:
            raise ValidationError(f"Kein eigener Event mit ID {', '.join(missing)} gefunden.")
        return ids

    def _save_m2m(self):
        rule_tags = list(self.instance.tags.filter(assigned_by_rule=True))
        super()._save_m2m()
        if rule_tags:
            self.instance.tags.add(*rule_tags)
        if 'translation_ids' in self.cleaned_data:
            self.instance.translations.set(self.cleaned_data['translation_ids'])


class EventOccurrenceForm(forms.ModelForm):
    """Inline form for Event_Occurrence."""

    class Meta:
        model = Event_Occurrence
        fields = ['start_datetime', 'end_datetime']
        widgets = {
            'start_datetime': forms.DateTimeInput(
                attrs={
                    'type': 'datetime-local',
                    'class': 'form-input',
                },
                format='%Y-%m-%dT%H:%M',
            ),
            'end_datetime': forms.DateTimeInput(
                attrs={
                    'type': 'datetime-local',
                    'class': 'form-input',
                },
                format='%Y-%m-%dT%H:%M',
            ),
        }


EventOccurrenceFormSet = inlineformset_factory(
    EventV4,
    Event_Occurrence,
    form=EventOccurrenceForm,
    extra=1,
    min_num=1,
    validate_min=True,
    can_delete=True,
    fields=['start_datetime', 'end_datetime'],
)
