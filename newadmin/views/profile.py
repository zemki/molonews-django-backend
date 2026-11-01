from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.shortcuts import redirect
from django.views.generic import TemplateView, UpdateView

from content.models import Organization, Source
from content.permissions import allowed_sources, can_edit_organization, editable_organizations
from ..forms.profile import (
    UserProfileForm, CustomPasswordChangeForm,
    OrganizationForm, SourceForm,
)
from .mixins import NewAdminMixin


class ProfileView(NewAdminMixin, TemplateView):
    """User profile page with profile form, password change, and links."""
    template_name = 'newadmin/profile/index.html'
    current_section = 'profile'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user

        ctx['profile_form'] = UserProfileForm(instance=user)
        ctx['password_form'] = CustomPasswordChangeForm(user=user)

        # User's organization (single - via first source)
        org_ids = list(set(
            s.organization_id for s in user.sources.select_related('organization').all()
            if s.organization_id
        ))
        if org_ids:
            ctx['organization'] = Organization.objects.filter(id__in=org_ids).first()
            ctx['can_edit_organization'] = can_edit_organization(user, ctx['organization'])

        # User's sources
        ctx['user_sources'] = user.sources.select_related('organization').all()
        ctx['user_areas'] = user.area.all()

        return ctx

    def post(self, request, *args, **kwargs):
        action = request.POST.get('action', '')

        if action == 'profile':
            form = UserProfileForm(request.POST, request.FILES, instance=request.user)
            if form.is_valid():
                form.save()
                messages.success(request, 'Profil aktualisiert.')
                return redirect('newadmin:profile')
            else:
                ctx = self.get_context_data()
                ctx['profile_form'] = form
                return self.render_to_response(ctx)

        elif action == 'password':
            form = CustomPasswordChangeForm(user=request.user, data=request.POST)
            if form.is_valid():
                user = form.save()
                update_session_auth_hash(request, user)
                messages.success(request, 'Passwort geaendert.')
                return redirect('newadmin:profile')
            else:
                ctx = self.get_context_data()
                ctx['password_form'] = form
                return self.render_to_response(ctx)

        return redirect('newadmin:profile')


class OrganizationEditView(NewAdminMixin, UpdateView):
    """Edit the user's organization directly."""
    model = Organization
    form_class = OrganizationForm
    template_name = 'newadmin/profile/organization.html'
    current_section = 'profile'

    def get_queryset(self):
        # Nur eigene Organisationen und nur mit can_modify_organization_info
        # (Superuser: alle); sonst 404
        return editable_organizations(self.request.user)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['page_title'] = f'Organisation: {self.object.name}'
        return ctx

    def form_valid(self, form):
        obj = form.save(commit=False)
        temp_image = None
        if obj.pk is None and obj.image:
            temp_image = obj.image
            obj.image = None

        obj.save()
        form.save_m2m()

        if temp_image:
            obj.image = temp_image
            obj.save()

        messages.success(self.request, f'Organisation "{obj.name}" aktualisiert.')
        return redirect('newadmin:profile')


class SourceEditView(NewAdminMixin, UpdateView):
    """Edit a source the user has access to."""
    model = Source
    form_class = SourceForm
    template_name = 'newadmin/profile/source.html'
    current_section = 'profile'

    def get_queryset(self):
        return allowed_sources(self.request.user)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['page_title'] = f'Quelle: {self.object.name}'
        return ctx

    def form_valid(self, form):
        obj = form.save(commit=False)
        temp_image = None
        if obj.pk is None and obj.default_image:
            temp_image = obj.default_image
            obj.default_image = None

        obj.save()
        form.save_m2m()

        if temp_image:
            obj.default_image = temp_image
            obj.save()

        messages.success(self.request, f'Quelle "{obj.name}" aktualisiert.')
        return redirect('newadmin:profile')
