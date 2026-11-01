"""Gemeinsame Berechtigungsregeln für API, Redaktionsoberfläche und Django-Admin.

Rollen:

* Superuser dürfen alles.
* Redakteur*innen (Gruppe "Editors") und Autor*innen (Gruppe "Contributors")
  arbeiten nur mit ihren eigenen Quellen: denen, die ihrem Konto zugeordnet sind
  (``User.sources``) oder deren ``related_user`` sie sind.
* Konten ohne eine dieser Rollen dürfen keine Inhalte anlegen oder ändern.

Veröffentlichen dürfen nur Superuser und Konten mit ``can_publish``. Die Angaben
einer Organisation ändern nur Superuser und Konten mit
``can_modify_organization_info``, und nur für die eigenen Organisationen.
"""

from django.db.models import Q


def has_content_role(user) -> bool:
    """True für aktive Superuser, Redakteur*innen und Autor*innen."""
    if not user or not user.is_authenticated or not user.is_active:
        return False
    return bool(user.is_superuser or user.is_editor or user.is_contributor)


def allowed_sources(user):
    """Quellen, unter denen ``user`` Inhalte anlegen und bearbeiten darf."""
    from content.models import Source

    if not has_content_role(user):
        return Source.objects.none()
    if user.is_superuser:
        return Source.objects.all()
    return Source.objects.filter(
        Q(pk__in=user.sources.values("pk")) | Q(related_user=user)
    ).distinct()


def can_use_source(user, source) -> bool:
    if source is None:
        return False
    return allowed_sources(user).filter(pk=source.pk).exists()


def can_publish(user) -> bool:
    """Darf ``user`` Inhalte veröffentlichen (oder veröffentlicht lassen)?"""
    if not user or not user.is_authenticated or not user.is_active:
        return False
    return bool(user.is_superuser or user.can_publish)


def user_organizations(user):
    """Organisationen der eigenen Quellen und Organisationen, deren related_user ``user`` ist."""
    from content.models import Organization

    if not user or not user.is_authenticated or not user.is_active:
        return Organization.objects.none()
    if user.is_superuser:
        return Organization.objects.all()
    return Organization.objects.filter(
        Q(pk__in=allowed_sources(user).values("organization_id")) | Q(related_user=user)
    ).distinct()


def editable_organizations(user):
    """Organisationen, deren Angaben ``user`` ändern darf."""
    from content.models import Organization

    if not user or not user.is_authenticated or not user.is_active:
        return Organization.objects.none()
    if user.is_superuser:
        return Organization.objects.all()
    if not user.can_modify_organization_info:
        return Organization.objects.none()
    return user_organizations(user)


def can_edit_organization(user, organization) -> bool:
    if organization is None:
        return False
    return editable_organizations(user).filter(pk=organization.pk).exists()
