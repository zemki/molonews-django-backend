from django.db.models import Q
from django import forms
from django.core.exceptions import ValidationError
from django_ckeditor_5.widgets import CKEditor5Widget

from content.models import Article, Tag, Area
from content.permissions import allowed_sources
from ..constants import AREA_MAX_NON_ADMIN


def validate_image_size(image):
    max_size = 5 * 1024 * 1024  # 5MB
    if hasattr(image, 'size') and image.size > max_size:
        raise ValidationError("Das Bild ist zu gross. Maximale Groesse: 5 MB.")


class ArticleForm(forms.ModelForm):
    """Article form with role-based field visibility and CKEditor integration."""

    abstract = forms.CharField(
        widget=CKEditor5Widget(config_name="default"),
        label="Inhalt",
        required=True,
    )

    title = forms.CharField(
        widget=forms.Textarea(attrs={
            'rows': 2,
            'class': 'form-input form-input--title',
            'placeholder': 'Artikel-Titel eingeben...',
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
        label="Verknüpfte Übersetzung (Artikel-ID)",
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'placeholder': 'z.B. 1234 (mehrere per Komma)',
        }),
        help_text="ID des Artikels in der anderen Sprache. Die Verknüpfung gilt automatisch in beide Richtungen.",
    )

    class Meta:
        model = Article
        fields = [
            'title', 'abstract', 'link', 'image', 'image_url', 'image_source',
            'source', 'tags', 'area', 'language',
        ]
        widgets = {
            'link': forms.URLInput(attrs={'class': 'form-input', 'placeholder': 'https://...'}),
            'image_url': forms.URLInput(attrs={'class': 'form-input', 'placeholder': 'https://...'}),
            'image_source': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Bildquelle'}),
            'source': forms.Select(attrs={'class': 'form-select'}),
            'language': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.area_max = self._compute_area_max()

        # Bestehende Übersetzungs-Verknüpfungen als IDs anzeigen
        if self.instance.pk:
            linked_ids = self.instance.translations.order_by('id').values_list('id', flat=True)
            self.initial['translation_ids'] = ", ".join(str(pk) for pk in linked_ids)

        # Tag filtering: article tags only (category_id in [1, 3, 4])
        tag_qs = Tag.objects.filter(category_id__in=[1, 3, 4]).order_by('name')
        self.fields['tags'].queryset = tag_qs
        self.fields['tags'].widget = forms.SelectMultiple(
            attrs={'class': 'tom-select-multi'}
        )
        self.fields['tags'].choices = [(t.pk, t.name) for t in tag_qs]
        self.fields['tags'].required = True

        # Area
        area_qs = Area.objects.filter(visible=True).order_by('name')
        self.fields['area'].queryset = area_qs
        area_attrs = {'class': 'tom-select-multi'}
        if self.area_max is not None:
            area_attrs['data-max-items'] = str(self.area_max)
        self.fields['area'].widget = forms.SelectMultiple(attrs=area_attrs)
        self.fields['area'].choices = [(a.pk, a.name) for a in area_qs]
        self.fields['area'].required = True

        # Source: ModelForm bildet ForeignKey(null=False) bereits als Pflicht ab,
        # wir setzen es explizit zur Klarheit.
        if 'source' in self.fields:
            self.fields['source'].required = True

        if not user:
            return

        # Default area from user's areas (for new articles)
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
            for field_name in ['image_source', 'source']:
                if field_name in self.fields:
                    del self.fields[field_name]

            # Default tags from source for new articles
            if not self.instance.pk and user.sources.count() == 1:
                source = user.sources.first()
                self.initial['tags'] = list(
                    source.default_tags.values_list('id', flat=True)
                )

        # Superusers see all fields (no removal)

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
            raise ValidationError("Ein Artikel kann nicht mit sich selbst verknüpft werden.")
        candidates = Article.objects.filter(id__in=ids)
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
            raise ValidationError(f"Kein eigener Artikel mit ID {', '.join(missing)} gefunden.")
        return ids

    def _save_m2m(self):
        super()._save_m2m()
        if 'translation_ids' in self.cleaned_data:
            self.instance.translations.set(self.cleaned_data['translation_ids'])
