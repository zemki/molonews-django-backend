from django.urls import path, include, re_path

from rest_framework import permissions
from rest_framework import routers
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

from . import views

app_name = 'molonews'

schema_view = get_schema_view(
    openapi.Info(
        title="Molonews API", default_version="v2", description="Molonews API",
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
router.register(r"articles/archive", views.ArticleArchiveViewSet_V2, basename="article-archive")
router.register(r"articles/bookmarks", views.ArticleBookmarksViewSet_V2, basename="article-bookmarks")
router.register(r"articles", views.ArticleViewSet_V2, basename="articles")
router.register(r"categories", views.CategoryViewSet, basename="categories")
router.register(r"organizations", views.OrganizationViewSet, basename="organizations")

router.register(r"users/device", views.AppUserKnownViewSet, basename="users-device")
router.register(r"users/summary", views.SummaryViewSet, basename="users-summary")
router.register(r"users/tags", views.AppUserTagViewSet, basename="users-tags")
router.register(r"users/organizations", views.AppUserOrganizationsViewSet, basename="users-organizations")
router.register(r"users/location", views.AppUserLocationViewSet, basename="users-location")
router.register(r"users/push", views.AppUserPushViewSet, basename="users-push")

router.register(r"contact/feedback", views.FeedbackContactViewSet, basename="feedback")
router.register(
    r"contact/participate", views.ParticipateContactViewSet, basename="participate"
)

urlpatterns += [
    path("", include(router.urls)),
]
