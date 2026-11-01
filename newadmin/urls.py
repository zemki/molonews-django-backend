from django.urls import path
from .views import dashboard, article, event, profile, stats

app_name = 'newadmin'

urlpatterns = [
    path('login/', dashboard.LoginView.as_view(), name='login'),
    path('login/resend-activation/', dashboard.ResendActivationView.as_view(), name='resend-activation'),
    path('', dashboard.IndexView.as_view(), name='index'),

    # Articles
    path('artikel/', article.ArticleListView.as_view(), name='article-list'),
    path('artikel/neu/', article.ArticleCreateView.as_view(), name='article-create'),
    path('artikel/<int:pk>/', article.ArticleUpdateView.as_view(), name='article-edit'),
    path('artikel/<int:pk>/loeschen/', article.ArticleDeleteView.as_view(), name='article-delete'),

    # Events
    path('events/', event.EventListView.as_view(), name='event-list'),
    path('events/neu/', event.EventCreateView.as_view(), name='event-create'),
    path('events/<int:pk>/', event.EventUpdateView.as_view(), name='event-edit'),
    path('events/<int:pk>/loeschen/', event.EventDeleteView.as_view(), name='event-delete'),
    path('events/termine-generieren/', event.GenerateOccurrencesView.as_view(), name='event-generate-occurrences'),

    # Profile, Organization, Source
    path('profil/', profile.ProfileView.as_view(), name='profile'),
    path('organisation/<int:pk>/', profile.OrganizationEditView.as_view(), name='organization-edit'),
    path('quelle/<int:pk>/', profile.SourceEditView.as_view(), name='source-edit'),

    # Stats
    path('stats/', stats.StatsView.as_view(), name='stats'),
    path('stats/partial/articles/', stats.StatsArticlesPartialView.as_view(), name='stats-partial-articles'),
    path('stats/partial/events/', stats.StatsEventsPartialView.as_view(), name='stats-partial-events'),
    path('stats/partial/areas/', stats.StatsAreasPartialView.as_view(), name='stats-partial-areas'),

    # Logout
    path('logout/', dashboard.LogoutView.as_view(), name='logout'),
]
