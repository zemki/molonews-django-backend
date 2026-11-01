from django import forms
from django.contrib import admin
from logging import getLogger
logger = getLogger("molonews")
from ..models import AppUser, Organization
from django.db import transaction


LIST_FIELDS = ('id', 'type', 'name', 'get_areas', 'active')
FIELDS = ('id', 'type', 'name', 'active', 'description', 'image', 'title', 'homepage')


class OrganizationForm(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class OrganizationAdmin(admin.ModelAdmin):


    list_display = ('id', 'type', 'name', 'get_areas', 'active', 'created_at')
    list_filter = ('type', 'area', 'active', 'created_at')


    def get_areas(self, obj):
        return ", ".join([area.name for area in obj.area.all()])
    get_areas.short_description = 'Gebiete'

    # logger.error("running organization site")
    
    search_fields = ('name', 'area__name')
    list_editable = ()
    form = OrganizationForm
    exclude = ()

    class ContributorFields(object):
        list_editable = ()
        exclude = ('type', 'name', 'active',)

    def has_view_or_change_permission(self, request, obj=None):
        if request.user.is_contributor and request.user.can_modify_organization_info:
            return True
        return super().has_view_or_change_permission(request, obj)

    def has_view_permission(self, request, obj=None):
        if request.user.is_contributor and request.user.can_modify_organization_info:
            return True
        return super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if request.user.is_contributor and request.user.can_modify_organization_info:
            return True
        return super().has_view_permission(request, obj)

    def get_form(self, request, obj=None, change=False, **kwargs):
        if request.user.is_contributor:
            self.__dict__.update(
                {k: v for k, v in self.ContributorFields.__dict__.items() if not k.startswith('__')}
            )
        else:
            self.__dict__.update(
                {_key: getattr(OrganizationAdmin, _key) for _key in self.ContributorFields.__dict__.keys() if not _key.startswith('__')}
            )
        form = super().get_form(request, obj, change, **kwargs)

        class OrganizationUserForm(form):
            user = request.user

        return OrganizationUserForm

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        user = request.user
        if user.is_contributor:
            if not user.can_modify_organization_info:
                return Organization.objects.none()
            user_organizations = [source.organization.id for source in user.sources.all()]
            qs = qs.filter(id__in=user_organizations)

        # filter out organisations that are not located in the users area
        #user_areas = user.area.values()
        #all_user_areas = []
        #for area in user_areas:
        #    all_user_areas.append(area["id"])

        #qs = qs.filter(area__in=all_user_areas)
            
        return qs

    def save_model(self, request, obj, form, change):
        is_new = obj.pk is None

        image = None
        if is_new and obj.image is not None:
            image = obj.image
            obj.image = None

        super().save_model(request, obj, form, change)

        if image:
            obj.image = image
            #super().save_model(request, obj, change=True)
            super().save_model(request, obj, form, change)

        if is_new:

            ThroughAllTags  = AppUser.organization_all_tags.through
            appuser_ids = list(AppUser.objects.values_list('id', flat=True))
            rows_alltags  = [ThroughAllTags(appuser_id=au_id, organization_id=obj.id) for au_id in appuser_ids]
            
            with transaction.atomic():
                ThroughAllTags.objects.bulk_create(rows_alltags, ignore_conflicts=True)

