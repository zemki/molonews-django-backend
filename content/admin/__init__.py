import json
import threading
import uuid
import requests as http_requests

from content.import_tools.url_safety import safe_get

from django.contrib import admin
from django.contrib.auth.models import Group
from django.core.cache import caches
from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.urls import path, reverse

from ..models import (
    User,
    Article,
    ArticleDraft,
    Event,
    EventV4,
    EventDraft,
    Source,
    Category,
    Organization,
    Tag,
    Subtag,
    AppUser,
    Area,
    TagCategories
)

from .article import ArticleAdmin, ArticleDraftAdmin
from .event import EventAdmin, EventDraftAdmin
from .eventv4 import EventV4Admin
from .source import SourceAdmin
from .user import CustomUserAdmin
from .organization import OrganizationAdmin
from logging import getLogger
from django.contrib import messages

logger = getLogger(__name__)

CHECK_TIMEOUT = 15


def _get_check_cache():
    return caches['source_check']


def _run_check(job_id):
    """Background worker that checks all active source URLs."""
    cache = _get_check_cache()
    sources = list(Source.objects.filter(active=True).order_by('id'))
    total_with_link = sum(1 for s in sources if s.link)

    job = {
        'done': False,
        'total': len(sources),
        'total_with_link': total_with_link,
        'checked': 0,
        'ok_count': 0,
        'no_link_count': 0,
        'broken': [],
    }
    cache.set(job_id, json.dumps(job), 600)

    for source in sources:
        if not source.link:
            job['no_link_count'] += 1
            job['checked'] += 1
            cache.set(job_id, json.dumps(job), 600)
            continue

        try:
            resp = safe_get(
                source.link,
                timeout=CHECK_TIMEOUT,
                headers={'User-Agent': 'molo link-checker/1.0'},
            )
            if resp.status_code >= 400:
                job['broken'].append({
                    'id': source.id,
                    'name': source.name,
                    'status': f'HTTP {resp.status_code}',
                    'type': source.type,
                    'link': source.link,
                    'admin_url': reverse('admin:content_source_change', args=[source.id]),
                })
            else:
                job['ok_count'] += 1
        except http_requests.ConnectionError:
            job['broken'].append({
                'id': source.id,
                'name': source.name,
                'status': 'CONNECTION ERROR',
                'type': source.type,
                'link': source.link,
                'admin_url': reverse('admin:content_source_change', args=[source.id]),
            })
        except http_requests.Timeout:
            job['broken'].append({
                'id': source.id,
                'name': source.name,
                'status': 'TIMEOUT',
                'type': source.type,
                'link': source.link,
                'admin_url': reverse('admin:content_source_change', args=[source.id]),
            })
        except ValueError as e:
            # interne Adresse (SSRF-Schutz) oder ungültige URL
            job['broken'].append({
                'id': source.id,
                'name': source.name,
                'status': f'GESPERRT: {e}',
                'type': source.type,
                'link': source.link,
                'admin_url': reverse('admin:content_source_change', args=[source.id]),
            })
        except http_requests.RequestException as e:
            job['broken'].append({
                'id': source.id,
                'name': source.name,
                'status': f'FEHLER: {e}',
                'type': source.type,
                'link': source.link,
                'admin_url': reverse('admin:content_source_change', args=[source.id]),
            })

        job['checked'] += 1
        cache.set(job_id, json.dumps(job), 600)

    job['done'] = True
    cache.set(job_id, json.dumps(job), 600)


class MoloAdminSite(admin.AdminSite):

    def get_urls(self):
        custom_urls = [
            path(
                'check-source-urls/',
                self.admin_view(self.check_source_urls_view),
                name='check_source_urls',
            ),
            path(
                'check-source-urls/start/',
                self.admin_view(self.check_source_urls_start),
                name='check_source_urls_start',
            ),
            path(
                'check-source-urls/status/',
                self.admin_view(self.check_source_urls_status),
                name='check_source_urls_status',
            ),
        ]
        return custom_urls + super().get_urls()

    def check_source_urls_view(self, request):
        if not request.user.is_superuser:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden("Nur für Superuser.")
        context = {**self.each_context(request), 'title': 'Quellen-Link-Check'}
        return TemplateResponse(request, 'admin/check_source_urls.html', context)

    def check_source_urls_start(self, request):
        if not request.user.is_superuser:
            return JsonResponse({'error': 'Nur für Superuser.'}, status=403)

        job_id = str(uuid.uuid4())
        cache = _get_check_cache()
        cache.set(job_id, json.dumps({
            'done': False, 'total': 0, 'total_with_link': 0,
            'checked': 0, 'ok_count': 0, 'no_link_count': 0, 'broken': [],
        }), 600)
        thread = threading.Thread(target=_run_check, args=(job_id,), daemon=True)
        thread.start()
        return JsonResponse({'job_id': job_id})

    def check_source_urls_status(self, request):
        if not request.user.is_superuser:
            return JsonResponse({'error': 'Nur für Superuser.'}, status=403)

        job_id = request.GET.get('job_id')
        cache = _get_check_cache()
        raw = cache.get(job_id)
        if not raw:
            return JsonResponse({'error': 'Job nicht gefunden.'}, status=404)

        job = json.loads(raw)

        if job['done']:
            cache.delete(job_id)

        return JsonResponse(job)

    def get_app_list(self, request, app_label=None):
        """
        Return a sorted list of all the installed apps that have been
        registered in this site.
        """
        app_dict = self._build_app_dict(request, app_label)

        # Sort the apps alphabetically.
        app_list = sorted(app_dict.values(), key=lambda x: x['name'].lower())

        # Sort the models alphabetically within each app, ignore succeeding 's' and '-'.
        for app in app_list:
            app['models'].sort(key=lambda x: x['name'].rstrip('s').strip('-').lower())

        return app_list

class CategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "title", "description")

class CategoriesAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "name", "description")

class TagAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "name_en", "tagcategories", "type", "category", "get_areas", "assigned_by_rule")
    list_editable = ("name_en",)
    list_filter = ("category", "type", "assigned_by_rule")
    filter_horizontal = ("areas",)

    def get_areas(self, obj):
        """Gebiete eines regionalen Themas, leer = überall"""
        return ", ".join(area.name for area in obj.areas.all())

    get_areas.short_description = "Nur in Gebieten"

class SubTagAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "name_en", "get_organizations", "created_at")
    list_editable = ("name_en",)
    list_filter = ("name", "organizations")
    
    def get_organizations(self, obj):
        """Zeigt alle verknüpften Organisationen als kommaseparierte Liste an"""
        return ", ".join([org.name for org in obj.organizations.all()])
    
    get_organizations.short_description = "Organizations"  # Spaltenüberschrift
    get_organizations.admin_order_field = "organizations"  # Ermöglicht Sortierung

class AppUserAdmin(admin.ModelAdmin):
    list_display = ("id", "date_joined", "device_id")
    # Allow sorting by date_joined (default ascending order)
    ordering = ('-date_joined',)  # Change to 'date_joined' for ascending order

moloadmin = MoloAdminSite(name='Moloadmin')
moloadmin.register(Group)
moloadmin.register(Article, ArticleAdmin)
moloadmin.register(ArticleDraft, ArticleDraftAdmin)
moloadmin.register(EventV4, EventV4Admin)
moloadmin.register(Source, SourceAdmin)
moloadmin.register(User, CustomUserAdmin)
moloadmin.register(Organization, OrganizationAdmin)
moloadmin.register(Category, CategoryAdmin)
moloadmin.register(TagCategories, CategoriesAdmin)
moloadmin.register(Tag, TagAdmin)
moloadmin.register(Subtag, SubTagAdmin)
moloadmin.register(AppUser, AppUserAdmin)

def prohibit_delete(modeladmin, request, queryset):
    messages.error(request, "Deletion of areas is prohibited.")

class AreaAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "latitude", "longitude", "created_at")
    search_fields = ("name",)
    actions = ["Keine"]

    def has_delete_permission(self, request, obj=None):
        return False

moloadmin.register(Area, AreaAdmin)