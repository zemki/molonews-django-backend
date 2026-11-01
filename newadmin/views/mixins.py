from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.urls import reverse_lazy

from content.permissions import allowed_sources, can_publish, has_content_role


class NewAdminMixin(LoginRequiredMixin):
    """Base mixin for all newadmin views.

    Requires login and a content role (superuser, editor or contributor) and
    provides role context. Accounts without a role get 403.
    """
    login_url = reverse_lazy('newadmin:login')

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not has_content_role(request.user):
            raise PermissionDenied("Kein Zugriff auf die Redaktionsoberfläche.")
        return super().dispatch(request, *args, **kwargs)

    def get_user_role(self):
        user = self.request.user
        if user.is_superuser:
            return 'superuser'
        if user.is_editor:
            return 'editor'
        if user.is_contributor:
            return 'contributor'
        return 'staff'

    def get_user_avatar_url(self):
        user = self.request.user
        if user.profile_image:
            return user.profile_image.url
        # Fallback: organization image from user's first source
        for source in user.sources.select_related('organization').all():
            if source.organization and source.organization.image:
                return source.organization.image.url
        return None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['user_role'] = self.get_user_role()
        ctx['current_user'] = self.request.user
        ctx['current_section'] = getattr(self, 'current_section', '')
        ctx['user_avatar_url'] = self.get_user_avatar_url()
        ctx['user_can_publish'] = can_publish(self.request.user)
        return ctx


class ContentSaveMixin:
    """Gemeinsame Regeln beim Speichern von Artikeln und Events."""

    def apply_source_and_publish_rules(self, obj, form):
        """Quelle prüfen und Veröffentlichung nur mit Recht zulassen.

        Gibt False zurück (und setzt einen Formularfehler), wenn die Quelle
        nicht zu den eigenen Quellen gehört.
        """
        user = self.request.user
        sources = allowed_sources(user)
        if obj.source_id is None and sources.count() == 1:
            obj.source = sources.first()
        if obj.source_id is None or not sources.filter(pk=obj.source_id).exists():
            form.add_error(None, 'Bitte eine eigene Quelle auswählen.')
            return False

        publish_requested = '_save_publish' in self.request.POST
        obj.published = publish_requested and can_publish(user)
        self.publish_denied = publish_requested and not obj.published
        return True


class ArticlePermissionMixin(NewAdminMixin):
    """Applies article queryset filtering based on user role."""

    def get_queryset(self):
        from content.models import Article
        qs = Article.objects.select_related('source').prefetch_related('tags', 'area').all()
        user = self.request.user

        if user.is_superuser:
            return qs
        return qs.filter(source__in=allowed_sources(user)).distinct()
