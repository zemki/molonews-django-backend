from django.urls import path, include, re_path
from rest_framework import permissions
from rest_framework import routers
from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from rest_framework_simplejwt.views import (
    TokenRefreshView,
)
from . import views
from .views.event_ai_search import AIEventSearchView, AIEventSearchAPIView, EventDetailView, SpeechTranscribeView
from .views import ai_tagging
from .views import ai_area_assignment

import logging

app_name = 'molonews'

logger = logging.getLogger("molonews")

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

# Article Routes
router.register(r"articles", views.ArticleViewSet_V4, basename="articles")
router.register(r"users/article-archive", views.ArticleArchiveViewSet_V4, basename="users-article-archive")
router.register(r"users/article-bookmarks", views.ArticleBookmarksViewSet_V4, basename="users-article-bookmarks")
router.register(r"articles/picture-upload", views.PictureUploadArticleViewSet, basename="article-picture-upload")

# AI Routes
router.register(r"AI/generate-article-tags", views.ArticleTagViewSet, basename="ai-generate-article-tags")
router.register(r"AI/generate-event-tags", views.EventTagViewSet, basename="ai-generate-event-tags")
router.register(r'ai-tagging/articles', ai_tagging.AIArticleTaggingViewSet, basename='ai-tagging-articles')
router.register(r'ai-tagging/events', ai_tagging.AIEventTaggingViewSet, basename='ai-tagging-events')
router.register(r'ai-area/articles', ai_area_assignment.AIArticleAreaAssignmentViewSet, basename='ai-area-articles')

# Event Routes
router.register(r"events", views.EventViewSet_V4, basename="events")
router.register(r"users/event-archive", views.EventArchiveViewSet_V4, basename="users-event-archive")
router.register(r'users/event-bookmarks', views.EventBookmarksViewSet_V4, basename='bookmarked-events')

#router.register(r"events/summary", views.EventOverviewViewSet_V4)
router.register(r"events/picture-upload", views.PictureUploadEventViewSet, basename="event-picture-upload")

# Category Routes
router.register(r"categories", views.CategoryViewSet, basename="categories")

# Organization Routes
router.register(r"organizations", views.OrganizationViewSet, basename="organizations")

# Source Routes
router.register(r"sources", views.SourceViewSet, basename="sources")

# User Routes
router.register(r"users/device", views.AppUserKnownViewSet, basename="users-device")
router.register(r"users/summary", views.SummaryViewSet, basename="users-summary")
router.register(r"users/article-tags", views.AppUserArticleTagViewSet, basename="users-article-tags")
router.register(r"users/event-tags", views.AppUserEventTagViewSet, basename="users-event-tags")
router.register(r"users/organizations", views.AppUserOrganizationsViewSet, basename="users-organizations")
router.register(r"users/location", views.AppUserLocationViewSet, basename="users-location")
router.register(r"users/push", views.AppUserPushViewSet, basename="users-push")

# create admin user
router.register(r"users", views.AdminUserViewSet, basename="users")

# General Routes
router.register(r"general/appurls", views.AppUrlsViewSet, basename="general-appurls")
router.register(r"area", views.AreaViewSet, basename="area")

# Contact Routes
router.register(r"contact/feedback", views.FeedbackContactViewSet, basename="feedback")
router.register(r"contact/participate", views.ParticipateContactViewSet, basename="participate"
                
)

# urls for version 4
urlpatterns += [
    # Kombinierte Artikel + Events pro Organisation(en) - muss vor dem Router
    # stehen, sonst greift die Detail-Route organizations/{pk}/
    path('organizations/content/', views.OrganizationContentViewSet.as_view({'get': 'list'}), name='organization-content'),
    path('organizations/taxonomies/', views.OrganizationTaxonomiesViewSet.as_view({'get': 'list'}), name='organization-taxonomies'),
    path("", include(router.urls)),
    path('events/<int:pk>/', views.EventViewSet_V4.as_view({'get': 'retrieve_event'}), name='retrieve_event'),
    path('users/list_events_by_organization/', views.EventViewSet_V4.as_view({'get': 'list_events_by_organization'}), name='list_events_by_organization'),
    path('users/list_articles_by_organization/', views.ArticleListViewSet_V4.as_view({'get': 'list_articles_by_organization'}), name='list_articles_by_organization'),
    #path('users/list_events_by_category/', views.EventViewSet_V4.as_view({'get': 'list_events_by_category'}), name='list_events_by_category'),
    path('users/list_future_events_by_category/', views.EventViewSet_V4.as_view({'get': 'list_future_events_by_category'}), name='list_future_events_by_category'),
    path('users/remove-organization-from-user/', views.AppUserOrganizationsViewSetSingle.as_view({'post': 'remove_organization'}), name='remove_organization'),
    path('users/add-organization-to-user/', views.AppUserOrganizationsViewSetSingle.as_view({'post': 'add_organization'}), name='add_organization'),
    path('articles/<int:id>/picture-upload/', views.PictureUploadArticleViewSet.as_view({'post': 'upload_picture'}), name='article-picture-upload'),
    path('tags/get-all-article-tags/', views.ArticleTagReceiveViewSet_V4.as_view({'get': 'get_all_tags'}), name='get_all_tags'),
    path('tags/get-all-event-tags/', views.EventTagReceiveViewSet_V4.as_view({'get': 'get_all_event_tags'}), name='get_all_event_tags'),
    path('tags/get-categories/', views.TagCategoryReceiveViewSet.as_view({'get': 'get_categories'}), name='get_categories'),
    path('tags/get-categories-full/', views.TagCategoryReceiveViewSetFull.as_view({'get': 'get_categories_full'}), name='get_categories_full'),
    path('events/<int:id>/picture-upload/', views.PictureUploadEventViewSet.as_view({'post': 'upload_picture'}), name='events-picture-upload'),
    path('general/flag-article/', views.ArticleFlagViewSet_V4.as_view({'post': 'flag_article'}), name='flag_article'),
    path('general/flag-event/', views.EventFlagViewSet_V4.as_view({'post': 'flag_event'}), name='flag_event'),
    path('users/delete/', views.AdminUserViewSet.as_view({'delete': 'delete_user'}), name='delete_user'),
    path('users/activate/', views.AdminUserViewSet.as_view({'get': 'activate_user'}), name='activate_user'),
    path('users/resend-activation/', views.AdminUserViewSet.as_view({'post': 'resend_activation'}), name='resend_activation'),
    path('users/reset-password-with-token/', views.AdminUserViewSet.as_view({'post': 'reset_password_with_token'}), name='reset_password_with_token'),
    path('users/request-password-reset/', views.AdminUserViewSet.as_view({'post': 'request_password_reset'}), name='request_password_reset'),
    path('users/redirect-user-into-app/', views.AdminUserViewSet.as_view({'get': 'redirect_user_into_app'}), name='redirect_user_into_app'),
    path('users/change-password/', views.AdminUserViewSet.as_view({'post': 'change_password'}), name='change_password'),
    path('users/update-user/', views.AdminUserViewSet.as_view({'put': 'update_user'}), name='update_user'),
    path('users/get-user-details/', views.AdminUserViewSet.as_view({'get': 'get_user_details'}), name='get_user_data'),
    path('users/list_areas_by_user/', views.AppUserAreaRadiusViewSet.as_view({'get': 'list_areas_by_user'}), name='list_areas_by_user'),
    path('users/set_areas_by_location/', views.AppUserAreaRadiusViewSet.as_view({'post': 'set_areas_by_location'}), name='set_areas_by_location'),
    path('users/list-tag-preferences/', views.TagPreferenceViewSet.as_view({'get': 'list_tag_preferences'}), name='list_tag_preferences'),
    path('users/list-tag-preferences-articles/', views.TagPreferenceViewSet.as_view({'get': 'list_tag_preferences_articles'}), name='list_tag_preferences_articles'),
    path('users/list-tag-preferences-events/', views.TagPreferenceViewSet.as_view({'get': 'list_tag_preferences_events'}), name='list_tag_preferences_events'),
    path('users/set-tag-preferences/', views.TagPreferenceViewSet.as_view({'post': 'set_tag_preferences'}), name='set_tag_preferences'),
    path('users/set-tag-preferences-articles/', views.TagPreferenceViewSet.as_view({'post': 'set_tag_preferences_articles'}), name='set_tag_preferences_articles'),
    path('users/set-tag-preferences-events/', views.TagPreferenceViewSet.as_view({'post': 'set_tag_preferences_events'}), name='set_tag_preferences_events'),
    path('users/list_articles/', views.ArticleListViewSet_V4.as_view({'get': 'list_articles'}), name='list_user_articles'),
    path('import/upload-file/', views.ImportFileUploadViewSet.as_view({'post': 'create'}), name='import_upload_file'),
    path('import/import-file/', views.ImportFileViewSet.as_view({'post': 'create'}), name='import_file'),
    path('import/status/', views.ImportStatusViewSet.as_view({'get': 'list'}), name='import_status'),
    path('import/delete-events-by-source/', views.DeleteEventsBySourceViewSet.as_view({'post': 'create'}), name='delete_events_by_source'),

    path('token/', views.CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('token/verify/', views.CustomTokenVerifyView.as_view(), name='token_verify'),
    path('redirect/', views.RedirectView.as_view(), name='redirect_to_website'),

    # Test Push
    path('admin/test-push/', views.PushTestViewSet.as_view({'post': 'create'}), name='test_push'),
    path('users/events/', views.CombinedViewSetEvent_V4.as_view({'get': 'list_user_events'}), name='list_user_events'),
    path('users/articles/', views.CombinedViewSetArticle_V4.as_view({'get': 'list_user_articles'}), name='list_user_articles'),
    path('users/picture-upload/', views.PictureUploadUserViewSet.as_view({'post': 'upload_picture'}), name='organizations-picture-upload'),
    path('area/closest/', views.AreaViewSet.as_view({'get': 'closest'}), name='closest'),
    path('area/by_distance/', views.AreaViewSet.as_view({'get': 'by_distance'}), name='by_distance'),
    path("public/articles/<int:pk>/", views.article_public_page, name="article_public_page"),
    path("monitoring/status/", views.monitoring_status_page, name="monitoring_status_page"),

    # Organization Image
    path('organization/<int:pk>/image', views.OrganizationImageViewSet.as_view({'get': 'retrieve_image'}), name='organization-image'),
    # Event AI Search
    path('event/ai-search/', AIEventSearchView.as_view(), name='event-ai-search'),
    path('api/event/ai-search/', AIEventSearchAPIView.as_view(), name='event-ai-search-api'),
    path('api/event/transcribe/', SpeechTranscribeView.as_view(), name='speech-transcribe'),
    path('event/<int:event_id>/', EventDetailView.as_view(), name='event-detail'),


]



   