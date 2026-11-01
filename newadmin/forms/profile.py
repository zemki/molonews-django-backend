from django import forms
from django.contrib.auth.forms import PasswordChangeForm

from content.import_tools.url_safety import check_url
from content.models import User, Organization, Source, Area, Tag


class UserProfileForm(forms.ModelForm):
    """User profile form for self-service editing."""

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'phone', 'display_name', 'profile_image']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-input'}),
            'last_name': forms.TextInput(attrs={'class': 'form-input'}),
            'email': forms.EmailInput(attrs={'class': 'form-input'}),
            'phone': forms.TextInput(attrs={'class': 'form-input'}),
            'display_name': forms.TextInput(attrs={'class': 'form-input'}),
            'profile_image': forms.ClearableFileInput(attrs={'class': 'form-file'}),
        }


class CustomPasswordChangeForm(PasswordChangeForm):
    """Password change form with newadmin styling."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-input'


class OrganizationForm(forms.ModelForm):
    """Organization edit form. Contributors can only edit some fields."""

    class Meta:
        model = Organization
        fields = [
            'name', 'title', 'type', 'description', 'homepage',
            'image', 'image_source',
            'address', 'street', 'zip', 'town',
            'area', 'active',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input'}),
            'title': forms.TextInput(attrs={'class': 'form-input'}),
            'type': forms.Select(attrs={'class': 'form-select'}),
            'description': forms.Textarea(attrs={'class': 'form-input', 'rows': 4}),
            'homepage': forms.URLInput(attrs={'class': 'form-input', 'placeholder': 'https://...'}),
            'image': forms.ClearableFileInput(attrs={'class': 'form-file'}),
            'image_source': forms.TextInput(attrs={'class': 'form-input'}),
            'address': forms.TextInput(attrs={'class': 'form-input'}),
            'street': forms.TextInput(attrs={'class': 'form-input'}),
            'zip': forms.TextInput(attrs={'class': 'form-input'}),
            'town': forms.TextInput(attrs={'class': 'form-input'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

        # Area
        area_qs = Area.objects.filter(visible=True).order_by('name')
        self.fields['area'].queryset = area_qs
        self.fields['area'].widget = forms.SelectMultiple(
            attrs={'class': 'tom-select-multi'}
        )
        self.fields['area'].widget.choices = [(a.pk, a.name) for a in area_qs]
        self.fields['area'].required = False
        self.fields['active'].required = False

        # Contributors can't edit type, name, active
        if user and user.is_contributor:
            for field_name in ('type', 'name', 'active'):
                if field_name in self.fields:
                    del self.fields[field_name]


class SourceForm(forms.ModelForm):
    """Source edit form for contributors/editors."""

    class Meta:
        model = Source
        fields = [
            'name', 'link', 'default_tags', 'default_image',
            'default_image_url', 'default_published',
            'auto_detect_area_from_content', 'area',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input'}),
            'link': forms.URLInput(attrs={'class': 'form-input', 'placeholder': 'https://...'}),
            'default_image': forms.ClearableFileInput(attrs={'class': 'form-file'}),
            'default_image_url': forms.URLInput(attrs={'class': 'form-input', 'placeholder': 'https://...'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

        # Default tags: filter by source's default_category
        instance = kwargs.get('instance') or self.instance
        if instance and instance.default_category_id:
            tag_qs = Tag.objects.filter(category=instance.default_category).order_by('name')
        else:
            tag_qs = Tag.objects.none()

        self.fields['default_tags'].queryset = tag_qs
        self.fields['default_tags'].widget = forms.SelectMultiple(
            attrs={'class': 'tom-select-multi'}
        )
        self.fields['default_tags'].widget.choices = [(t.pk, t.name) for t in tag_qs]
        self.fields['default_tags'].required = False
        self.fields['default_tags'].label = 'Standard-Themen'

        # Area
        area_qs = Area.objects.filter(visible=True).order_by('name')
        self.fields['area'].queryset = area_qs
        self.fields['area'].widget = forms.SelectMultiple(
            attrs={'class': 'tom-select-multi'}
        )
        self.fields['area'].widget.choices = [(a.pk, a.name) for a in area_qs]
        self.fields['area'].required = False

        # Boolean fields (superuser only)
        if user and user.is_superuser:
            self.fields['default_published'].required = False
            self.fields['auto_detect_area_from_content'].required = False
            self.fields['auto_detect_area_from_content'].label = 'Gebiet automatisch aus Inhalt erkennen'
            self.fields['default_published'].label = 'Standardmaessig veroeffentlicht'
        else:
            del self.fields['default_published']
            del self.fields['auto_detect_area_from_content']

    def clean_link(self):
        link = self.cleaned_data.get('link')
        # Nur neue oder geänderte Links prüfen; der Server ruft sie beim Import ab
        if link and 'link' in self.changed_data:
            error = check_url(link)
            if error:
                raise forms.ValidationError(error)
        return link

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get('auto_detect_area_from_content'):
            cleaned_data['area'] = []
        return cleaned_data
