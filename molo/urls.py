from django.conf import settings
from django.urls import path, include, re_path
from django.conf.urls.static import static
from django.views.generic import RedirectView
from django.views.i18n import JavaScriptCatalog

from content.admin import moloadmin

urlpatterns = [
    re_path(r"^jet/", include("jet.urls", "jet")),
    path("api/v1/", include("api.urls_v1", namespace="v1")),
    path("api/v2/", include("api.urls_v2", namespace="v2")),
    path("api/v3/", include("api.urls", namespace="v3")),
    path("api/v4/", include("api.urls_v4", namespace="v4")),
    # django-recurrence (must be before the catch-all admin pattern)
    path('jsi18n.js', JavaScriptCatalog.as_view(packages=['recurrence']), name='jsi18n'),
    path("ckeditor5/", include('django_ckeditor_5.urls')),
    re_path(r"^newadmin/[/\\]*(?P<path>.*)$", RedirectView.as_view(url="/%(path)s", permanent=False, query_string=True)),
    re_path(r"^admin/[/\\]*(?P<path>.*)$", RedirectView.as_view(url="/%(path)s", permanent=False, query_string=True)),
    path("oldadmin/", moloadmin.urls),
    path("", include("newadmin.urls")),
    path("", include("loginas.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

