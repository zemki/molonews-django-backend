"""
⚠️  DEPRECATED - DO NOT USE THIS FILE ⚠️

This file is DEPRECATED and should NOT be used for new features.

Please use urls_v4.py instead!

The v4 API is the current version and all new routes should be added there.
File location: api/urls_v4.py
"""

from django.urls import path, include, re_path

from rest_framework import permissions
from rest_framework import routers
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

from . import views

app_name = 'molonews'

schema_view = get_schema_view(
    openapi.Info(
        title="Molonews API", default_version="v3", description="Molonews API",
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)

urlpatterns = [
    re_path(
        r"^swagger(?P<format>\.json|\.yaml)$",
        schema_view.without_ui(cache_timeout=0),
        name="schema-json",
    ),
    re_path(
        r"^swagger/$",
        schema_view.with_ui("swagger", cache_timeout=0),
        name="schema-swagger-ui",
    ),
    re_path(
        r"^redoc/$", schema_view.with_ui("redoc", cache_timeout=0), name="schema-redoc"
    ),
]


router = routers.DefaultRouter()
router.register(r"articles", views.ArticleViewSet, basename="articles")
router.register(r"articles/archive", views.ArticleArchiveViewSet, basename="article-archive")
router.register(r"articles/bookmarks", views.ArticleBookmarksViewSet, basename="article-bookmarks")
router.register(r"articles/picture-upload", views.PictureUploadArticleViewSet, basename="article-picture-upload")

router.register(r"AI/generate-article-tags", views.ArticleTagViewSet, basename="ai-generate-article-tags")
router.register(r"AI/generate-event-tags", views.EventTagViewSet, basename="ai-generate-event-tags")

router.register(r"events/picture-upload", views.PictureUploadEventViewSet, basename="event-picture-upload")
router.register(r"events/archive", views.EventArchiveViewSet, basename="event-archive")
router.register(r"events/bookmarks", views.EventBookmarksViewSet, basename="event-bookmarks")
router.register(r"events/summary", views.EventOverviewViewSet, basename="event-summary")
router.register(r"events", views.EventViewSet, basename="events")

router.register(r"categories", views.CategoryViewSet, basename="categories")
router.register(r"organizations", views.OrganizationViewSet, basename="organizations")

router.register(r"users/device", views.AppUserKnownViewSet, basename="users-device")
router.register(r"users/summary", views.SummaryViewSet, basename="users-summary")
router.register(r"users/articletags", views.AppUserArticleTagViewSet, basename="users-articletags")
router.register(r"users/eventtags", views.AppUserEventTagViewSet, basename="users-eventtags")
router.register(r"users/organizations", views.AppUserOrganizationsViewSet, basename="users-organizations")
router.register(r"users/location", views.AppUserLocationViewSet, basename="users-location")
router.register(r"users/push", views.AppUserPushViewSet, basename="users-push")
router.register(r"appurls", views.AppUrlsViewSet, basename="appurls")
router.register(r"area", views.AreaViewSet, basename="area")


router.register(r"contact/feedback", views.FeedbackContactViewSet, basename="feedback")
router.register(r"contact/participate", views.ParticipateContactViewSet, basename="participate"
                
)

urlpatterns += [
    path("", include(router.urls)),
]

urlpatterns += [
   
]
