from django import forms


class CantModifyRelatedMixin(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if hasattr(self, 'dont_modify_related'):
            for field_name in self.dont_modify_related:
                field = self.fields.get(field_name, None)
                if field:
                    w = field.widget
                    w.can_add_related = \
                        w.can_change_related = \
                        w.can_delete_related = False


class OwnContentAdminMixin:
    """Klassischer Admin für Artikel und Events: gleiche Regeln wie API und Redaktionsoberfläche.

    * Nicht-Superuser sehen und bearbeiten nur Inhalte ihrer eigenen Quellen und
      können nur diese als Quelle wählen.
    * Veröffentlichen nur mit ``can_publish`` (sonst wird zur Prüfung eingereicht).
    * In der Listenansicht sind "published" (ohne Recht) und "reviewed" (ohne
      Redaktionsrolle) gesperrt.
    """

    def restrict_to_own_sources(self, request, qs):
        from content.permissions import allowed_sources

        if request.user.is_superuser:
            return qs
        return qs.filter(source__in=allowed_sources(request.user))

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        from content.permissions import allowed_sources

        if db_field.name == "source" and not request.user.is_superuser:
            kwargs["queryset"] = allowed_sources(request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def enforce_content_rules(self, request, obj):
        """Vor dem Speichern aufrufen: Quelle prüfen, Veröffentlichung nur mit Recht."""
        from django.core.exceptions import PermissionDenied

        from content.permissions import can_publish, can_use_source

        user = request.user
        if not user.is_superuser and not can_use_source(user, obj.source):
            raise PermissionDenied("Nur eigene Quellen sind erlaubt.")
        if not can_publish(user):
            obj.published = False
            obj.up_for_review = True

    def lock_changelist_fields(self, request, form_class):
        """Changelist-Formular, in dem nicht erlaubte Felder gesperrt sind."""
        from content.permissions import can_publish

        may_publish = can_publish(request.user)
        may_review = request.user.is_superuser or request.user.is_editor

        class LockedChangeListForm(form_class):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                if not may_publish and "published" in self.fields:
                    self.fields["published"].disabled = True
                if not may_review and "reviewed" in self.fields:
                    self.fields["reviewed"].disabled = True

        return LockedChangeListForm
