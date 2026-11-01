from django.conf import settings
from django.db.models import Count, Q
from datetime import datetime, timedelta
from django.forms import ValidationError
from rest_framework import serializers, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from drf_yasg.utils import swagger_auto_schema, swagger_serializer_method
from logging import getLogger
from .util import (
    UserPagination,
    choices_parameter,
    integer_parameter,
    string_parameter,
    boolean_parameter,
    header_string_parameter,
    bad_request,
)
from content.models import AppUser, Article, Tag, Organization, Area, Source, User
from django.db import transaction
from django.utils import timezone
from content.choices import ORGANIZATION_TYPE_CHOICES, LANGUAGE_CHOICES
from .article_event_shared import (
    AppUserBaseViewSet,
    BaseViewSet,
    SourceActiveFilter,
    get_source_default_image_url,
)
import django_filters as df
from rest_framework import viewsets
from rest_framework.viewsets import GenericViewSet
from drf_yasg import openapi
from django.shortcuts import get_object_or_404
from rest_framework import status
# ENTFERNT: import ml.news_article_tagging  as ml
import jwt
from api.views.util import can_view_unpublished, decode_access_token, related_source
from content.permissions import can_publish, has_content_role
from content.utils.mail import send_html_mail
from content.utils.urls import is_safe_link
from rest_framework.pagination import LimitOffsetPagination
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from threading import Thread
from api.views.util import area_filter, default_area_id


logger = getLogger("molonews")


def update_article_request_counts_async(article_ids):
    """
    Asynchronously update request_count for given article IDs.
    Runs in a separate thread after response is sent to client.
    """
    def _update():
        try:
            from django.db import connections
            with connections['default'].cursor() as cursor:
                placeholders = ','.join(['%s'] * len(article_ids))
                cursor.execute(
                    f"UPDATE content_article SET request_count = request_count + 1 "
                    f"WHERE id IN ({placeholders})",
                    article_ids
                )
            connections['default'].close()
        except Exception as e:
            logger.warning(f"Async request_count update failed: {e}")

    Thread(target=_update, daemon=True).start()

# Fields used for serialization

ARTICLE_LIST_FIELDS_NO_REQUEST_COUNT = (
    "id",
    "type",
    "language",
    "translations",
    "title",
    "abstract",
    "date",
    "created_at",
    "moddate",
    "image_url",
    "area",
    "address",
    "link",
    "is_hot",
    "tags",
    "organization",
    "organization_type",
    "organization_name",
    "organization_image",
    "image_source",
    "source",
    "content",
)

BASE_LIST_FIELDS = (
    "id",
    "type",
    "language",
    "translations",
    "title",
    "abstract",
    "date",
    "image_url",
    "area",
)

ARTICLE_LIST_FIELDS = BASE_LIST_FIELDS + (
    "created_at",
    "moddate",
    "address",
    "link",
    "is_hot",
    "tags",
    "organization",
    "organization_type",
    "organization_name",
    "organization_image",
    "image_source",
    "request_count",
)

ARTICLE_RETRIEVE_FIELDS = ARTICLE_LIST_FIELDS + (
    "source",
    "content",
)


def get_appuser(device_id):
    """Try to get an AppUser by device id

    Args:
        device_id (str): device id string

    Returns:
        AppUser or None: The AppUser object if found, otherwise None.
    """
    try:
        return AppUser.objects.get(device_id=device_id)
    except AppUser.DoesNotExist:
        return None


class ArticleFilter(df.FilterSet):
    """
    Filter class for the Article model.

    This class defines the filters that can be applied to the Article model.
    It provides filtering options based on organization, tags, and date.

    Attributes:
        organization (df.BaseInFilter): Filter for filtering by organization ID(s).
        organization_all_tags (df.BaseInFilter): Filter for filtering by organization ID(s) without considering tags.
        tags (df.BaseInFilter): Filter for filtering by tag ID(s).
        date_start (df.IsoDateTimeFilter): Filter for filtering by start date.
        date_end (df.IsoDateTimeFilter): Filter for filtering by end date.

    Methods:
        filter_queryset(queryset): Applies the defined filters to the given queryset.

    """

    organization = df.BaseInFilter(
        field_name="source__organization",
        help_text="Organization ID(s) - comma separated | defaults to user settings",
    )

    organization_all_tags = df.BaseInFilter(
        field_name="source__organization",
        help_text="Organization ID(s) - comma separated - not filtered against tags | defaults to user settings",
    )

    tags = df.BaseInFilter(
        field_name="tags",
        distinct=True,
        help_text="Tag ID(s) - comma separated | defaults to user settings",
    )

    date_start = df.IsoDateTimeFilter(
        field_name="date",
        help_text="ISO 8601 formatted",
        lookup_expr="gte",
    )

    date_end = df.IsoDateTimeFilter(
        field_name="date",
        help_text="ISO 8601 formatted",
        lookup_expr="lte",
    )

    language = df.ChoiceFilter(
        field_name="language",
        choices=LANGUAGE_CHOICES,
        help_text="Language code (de/en) | defaults to all languages",
    )

    def filter_queryset(self, queryset):
        """
        This function is used to get a queryset of articles based on various filters.
        It first fetches the user's preferences based on their device ID.
        Then it applies filters based on the user's preferences and the parameters passed via the API.
        If no user settings are found and no parameters are passed via the API, it selects all tags and organizations.
        Finally, it combines all the queries and filters the articles based on the combined query and the date.
        """

        # Fetch the user's preferences based on their device ID
        appuser = get_appuser(self.request.headers.get("X-Device-ID", None))

        # Initialize the queries
        queries = {
            "organization": Q(),
            "tags": Q(),
            "organization_all_tags": Q(),
        }
        user_queries = {}

        # If the user exists, apply filters based on the user's preferences
        if appuser:
            for name in queries:
                _filter = self.filters[name]
                lookup = "{}__{}".format(_filter.field_name, _filter.lookup_expr)
                # ignore event tags
                if name == "tags":
                    value = [
                        str(_attr.id)
                        for _attr in getattr(appuser, name).all().exclude(category_id=2)
                        if _attr
                    ]
                else:
                    value = [
                        str(_attr.id) for _attr in getattr(appuser, name).all() if _attr
                    ]
                if value is not None and value:
                    user_queries[name] = Q(**{lookup: value})

        # Overwrite filter settings if we get parameters via api
        ignore_usersettings = False
        parameter_queries = {}
        for name, value in self.form.cleaned_data.items():
            if name in ("organization", "tags", "organization_all_tags"):
                f = self.filters[name]
                lookup = "{}__{}".format(f.field_name, f.lookup_expr)
                if value is not None and value:
                    ignore_usersettings = True
                    parameter_queries[name] = Q(**{lookup: value})
            else:
                queryset = self.filters[name].filter(queryset, value)

        # Choose between user queries and parameter queries
        _queries = user_queries if not ignore_usersettings else parameter_queries

        # Update the queries
        for key, value in _queries.items():
            queries[key] = value

        # Select all tags if none is selected
        if (
            appuser is None
            and (
                "tags" not in self.form.cleaned_data.keys()
                or not self.form.cleaned_data["tags"]
            )
            and not ignore_usersettings
        ):
            queries["tags"] = Q(
                **{"tags__in": Tag.objects.filter(category_id__in=[1, 3, 4])}
            )

        # Select all organizations if none is selected
        if (
            appuser is None
            and (
                "organization" not in self.form.cleaned_data.keys()
                or not self.form.cleaned_data["organization"]
            )
            and not ignore_usersettings
        ):
            queries["organization"] = Q(
                **{"source__organization__in": Organization.objects.all()}
            )

        # Combine the queries
        if (
            len(queries["organization"] & queries["tags"]) < 2
            and len(queries["organization_all_tags"]) == 1
        ):
            queries_combined = queries["organization_all_tags"]
        elif (
            len(queries["organization"] & queries["tags"]) == 2
            and len(queries["organization_all_tags"]) < 1
        ):
            queries_combined = queries["organization"] & queries["tags"]
        else:
            queries_combined = (queries["organization"] & queries["tags"]) | queries[
                "organization_all_tags"
            ]

        # Exclude deselected organizations if user exists and is not using API parameters
        if appuser and not ignore_usersettings:
            # Get deselected organization IDs
            deselected_org_ids = list(appuser.deselected_organization.values_list('id', flat=True))
            deselected_org_all_tags_ids = list(appuser.deselected_organization_all_tags.values_list('id', flat=True))
            all_deselected_ids = set(deselected_org_ids + deselected_org_all_tags_ids)

            if all_deselected_ids:
                # Add exclude query for deselected organizations
                queries_combined = queries_combined & ~Q(source__organization__id__in=all_deselected_ids)

        # Filter the articles based on the combined query and the date
        now = timezone.now()
        queryset = queryset.filter(queries_combined).filter(date__lte=now)

        return queryset

        class Meta:
            model = Article
            fields = ["organization", "tags", "organization_all_tags", "date_start"]


class DeviceIdFilter(filters.BaseFilterBackend):
    """
    This class is a custom filter backend for Django Rest Framework.
    It filters a queryset based on the device ID provided in the request headers.
    It also annotates the queryset with the count of bookmarked and archived articles for the given device ID.
    """

    def filter_queryset(self, request, queryset, view):
        """
        This method overrides the filter_queryset method of the BaseFilterBackend class.
        It first retrieves the device ID from the request headers.
        If a device ID is provided, it annotates the queryset with the count of bookmarked and archived articles for that device ID.
        """

        # Get the device ID from the request headers
        device_id = request.headers.get("X-Device-ID", None)

        # If a device ID is provided, annotate the queryset
        if device_id is not None:
            # Annotate the queryset with the count of bookmarked articles for the device ID
            queryset = queryset.annotate(
                bookmarked=Count(
                    "pk", filter=Q(articles_bookmarked__device_id=device_id)
                )
            )
            # Annotate the queryset with the count of archived articles for the device ID
            queryset = queryset.annotate(
                archived=Count("pk", filter=Q(articles_archived__device_id=device_id))
            )

        # Return the annotated queryset
        return queryset

    def get_schema_fields(self, view):
        """
        This method overrides the get_schema_fields method of the BaseFilterBackend class.
        It returns an empty list because this filter backend does not add any fields to the schema.
        """

        # Return an empty list
        return []
    

class ArticleTagsSerializer(serializers.ModelSerializer):
    """
    Serializer for the ArticleTags model.

    This serializer is used to convert ArticleTags model instances into JSON
    representation and vice versa. It specifies the fields that should be
    included in the serialized output.

    Attributes:
        model (ArticleTags): The ArticleTags model class.
        fields (tuple): The fields to include in the serialized output.

    """
    selected = serializers.SerializerMethodField()

    class Meta:
        model = Tag
        fields = ("id", "name", "name_en", "color", "selected")
        ref_name = "ArticleTagsSerializerV4"

    @swagger_serializer_method(serializers.BooleanField)
    def get_selected(self, obj):
        app_user = self.context.get("app_user", None)
        if app_user:
            return app_user.tags.filter(id=obj.id).exists()
        return False
    

class AreaIdNameSerializer(serializers.ModelSerializer):
    class Meta:
        model = Area
        fields = ('id', 'name')

class ArticleBaseSerializer(serializers.ModelSerializer):
    """
    This class is a serializer for the Article model.
    It extends the ModelSerializer class provided by Django Rest Framework.
    It defines several fields that are serialized in a custom way.
    """

    # Define the fields that are serialized in a custom way
    date = serializers.SerializerMethodField()
    created_at = serializers.SerializerMethodField()
    moddate = serializers.SerializerMethodField()
    tags = ArticleTagsSerializer(read_only=True, many=True)
    organization = serializers.SerializerMethodField()
    organization_type = serializers.SerializerMethodField()
    organization_name = serializers.SerializerMethodField()
    organization_image = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    archived = serializers.SerializerMethodField()
    bookmarked = serializers.SerializerMethodField()
    link = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()
    translations = serializers.SerializerMethodField()

    @swagger_serializer_method(serializers.CharField)
    def get_type(self, instance):
        """Inhaltstyp für den Client, bei Artikeln immer 'article'."""
        return "article"

    def get_translations(self, instance):
        """Verlinkte, veröffentlichte Sprachversionen des Artikels als [{id, language}]."""
        return [
            {"id": t.id, "language": t.language}
            for t in instance.translations.all()
            if t.published
        ]

    def get_link(self, instance):
        #return instance.link if instance.link else "http://"
        # Beispiel: Über Source-Name oder Organization prüfen
        if instance.source.type == "local":
            # Lokale Artikel werden immer als HTML auf diesem Server angezeigt
            return f"{settings.SERVER_URL.rstrip('/')}/api/v4/public/articles/{instance.id}/"

        else:
            returnlink = instance.link.replace("http://", "https://") if instance.link else "http://"
            return returnlink

    @swagger_serializer_method(serializers.DateTimeField)
    def get_date(self, instance):
        """
        This method returns the date of the article in ISO format.
        It removes the microseconds from the date before converting it to ISO format.
        """
        return instance.date.replace(microsecond=0).isoformat()

    @swagger_serializer_method(serializers.DateTimeField)
    def get_created_at(self, instance):
        if instance.created_at is None:
            return None
        return instance.created_at.replace(microsecond=0).isoformat()

    @swagger_serializer_method(serializers.DateTimeField)
    def get_moddate(self, instance):
        if instance.moddate is None:
            return None
        return instance.moddate.replace(microsecond=0).isoformat()

    def get_organization_image(self, instance):
        org = instance.source.organization
        if org and org.image:
            request = self.context.get("request")
            return request.build_absolute_uri(org.image.url)
        return ""

    @swagger_serializer_method(serializers.BooleanField)
    def get_archived(self, instance):
        """
        This method returns whether the article is archived.
        It gets the 'archived' attribute from the instance and converts it to a boolean.
        """
        archived = bool(getattr(instance, "archived", False))
        return archived

    @swagger_serializer_method(serializers.BooleanField)
    def get_bookmarked(self, instance):
        """Return True if the article is bookmarked for the current app_user."""
        app_user = get_appuser(self.context.get("request").headers.get("X-Device-ID"))
        if app_user:
            return app_user.bookmarked_articles.filter(id=instance.id).exists()
        return False


    def get_image_url(self, instance):
        """
        This method returns the URL of the article's image.
        If the 'detail' context is set and the instance has a 'image_detail' attribute, it returns the absolute URL of 'image_detail'.
        If the instance has a 'image' attribute, it returns the absolute URL of 'image'.
        If the instance has a 'image_url' attribute, it returns 'image_url'.
        Otherwise, it returns the default image URL for the source of the article.
        Returns an empty string if no image is available (never None).
        """

        url = None
        try:
            if self.context.get("detail", None) and instance.image_detail:
                url = self.context["request"].build_absolute_uri(instance.image_detail.url)
            elif instance.image:
                url = self.context["request"].build_absolute_uri(instance.image.url)
            elif instance.image_url:
                url = instance.image_url
            else:
                url = get_source_default_image_url(instance, self.context)
        except Exception:
            url = None

        if not url:
            return ""

        if url.startswith("http://"):
            url = url.replace("http://", "https://", 1)

        return url

    @swagger_serializer_method(serializers.IntegerField)
    def get_organization(self, instance):
        """
        This method returns the ID of the organization that the article's source belongs to.
        """
        return instance.source.organization.id

    def get_organization_type(self, instance):
        """
        This method returns the type of the organization that the article's source belongs to.
        """
        return instance.source.organization.type

    def get_organization_name(self, instance):
        """
        This method returns the name of the organization that the article's source belongs to.
        """
        return instance.source.organization.name


class ArticleListSerializer(ArticleBaseSerializer):
    bookmarked_count = serializers.IntegerField(read_only=True)
    source = serializers.PrimaryKeyRelatedField(read_only=True)
    
    """
    This class is a serializer for the Article model for list views.
    It extends the ArticleBaseSerializer class.
    It includes all the fields defined in ARTICLE_LIST_FIELDS, as well as 'archived' and 'bookmarked'.
    """
    class Meta:
        model = Article
        fields = ARTICLE_LIST_FIELDS + (
            "archived",
            "bookmarked",
            "bookmarked_count",
            "source",
        )


class ArticleBookmarkedArchivedSerializer(ArticleBaseSerializer):
    """
    This class is a serializer for the Article model for bookmarked and archived views.
    It extends the ArticleBaseSerializer class.
    It includes all the fields defined in BASE_LIST_FIELDS, as well as 'archived' and 'bookmarked'.
    """
    class Meta:
        model = Article
        fields = BASE_LIST_FIELDS + (
            "archived",
            "bookmarked",
        )


class PaginatedArticleBookmarkedArchivedSerializer(serializers.Serializer):
    """
    This class is a serializer for paginated responses of bookmarked and archived articles.
    It includes fields for the count of results, the URL of the next page, the URL of the previous page, and the results themselves.
    The results are serialized using the ArticleBookmarkedArchivedSerializer.
    """
    count = serializers.IntegerField()
    next = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    previous = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    results = ArticleBookmarkedArchivedSerializer(many=True)


class ArticleRetrieveSerializer(ArticleBaseSerializer):
    """
    This class is a serializer for the Article model for retrieve views.
    It extends the ArticleBaseSerializer class.
    It includes all the fields defined in ARTICLE_RETRIEVE_FIELDS, as well as 'archived' and 'bookmarked'.
    """
    # get the amount of bookmarks for the article
    #bookmarked_count = serializers.SerializerMethodField()
    areas = serializers.SerializerMethodField()
    source = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Article

        fields = ARTICLE_LIST_FIELDS_NO_REQUEST_COUNT + (
            "archived",
            "bookmarked",
            "areas",
            "source",
            "published",
        )

        ref_name = "ArticleRetrieveSerializerV4"
    
    @swagger_serializer_method(serializers.IntegerField)
    def get_bookmarked_count(self, instance):
        """
        This method returns the amount of bookmarks for the article.
        """
        return instance.articles_bookmarked.count()
    
    @swagger_serializer_method(serializer_or_field=AreaIdNameSerializer(many=True))
    def get_areas(self, instance):
        """
        This method returns all areas of the article.
        """
        #fetch the areas of the article
        areas = instance.area.all()
        #return the areas
        return AreaIdNameSerializer(areas, many=True).data
        

class ArticleRetrieveSerializerUser(ArticleBaseSerializer):
    """
    This class is a serializer for the Article model for retrieve views.
    It extends the ArticleBaseSerializer class.
    It includes all the fields defined in ARTICLE_RETRIEVE_FIELDS, as well as 'archived' and 'bookmarked'.
    """
    # get the amount of bookmarks for the article
    bookmarked_count = serializers.SerializerMethodField()
    areas = serializers.SerializerMethodField()
    source = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Article
        fields = ARTICLE_RETRIEVE_FIELDS + (
            "archived",
            "bookmarked",
            "bookmarked_count",
            "areas",
            "source",
            "published", 
        )

        ref_name = "ArticleRetrieveSerializerV4"
    
    @swagger_serializer_method(serializers.IntegerField)
    def get_bookmarked_count(self, instance):
        """
        This method returns the amount of bookmarks for the article.
        """
        return instance.articles_bookmarked.count()
    
    @swagger_serializer_method(serializer_or_field=AreaIdNameSerializer(many=True))
    def get_areas(self, instance):
        """
        This method returns all areas of the article.
        """
        #fetch the areas of the article
        areas = instance.area.all()
        #return the areas
        return AreaIdNameSerializer(areas, many=True).data
    


class PaginatedArticleRetrieveSerializer(serializers.Serializer):
    """
    This class is a serializer for paginated responses of retrieved articles.
    It includes fields for the count of results, the URL of the next page, the URL of the previous page, and the results themselves.
    The results are serialized using the ArticleRetrieveSerializer.
    """
    count = serializers.IntegerField()
    next = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    previous = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    results = ArticleRetrieveSerializer(many=True)

class PaginatedArticleRetrieveSerializerUser(serializers.Serializer):
    """
    This class is a serializer for paginated responses of retrieved articles.
    It includes fields for the count of results, the URL of the next page, the URL of the previous page, and the results themselves.
    The results are serialized using the ArticleRetrieveSerializer.
    """
    count = serializers.IntegerField()
    next = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    previous = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    results = ArticleRetrieveSerializerUser(many=True)


def get_is_hot_queryset(exclude_article_ids, oldest_date, area_id=None):
    """Get a queryset containing recent articles flagge "is_hot".

    Args:
        exclude_article_ids (list): article ids to be excluded
        oldest_date (date): oldes date for articles

    Returns:
        queryset
    """
    return (
        Article.objects.filter(is_hot=True)
        .exclude(id__in=exclude_article_ids)
        .exclude(source__active=False)
        .exclude(source__organization__active=False)
        .filter(date__gte=oldest_date.strftime("%Y-%m-%d"))
        .filter(**area_filter(area_id))
        .order_by("-date")
    )

       
class ArticleViewSet(BaseViewSet):
    """
    This methods can be used to create, update, delete and list articles
    """

    # Define the queryset, serializer class, model class, filter backends, filterset class, time period to show, ordering fields, ordering, and pagination class
    queryset = Article.objects.filter(published=True).prefetch_related("translations")
    serializer_class = ArticleRetrieveSerializer
    model_class = Article
    filter_backends = BaseViewSet.filter_backends + [
        DeviceIdFilter,
    ]
    filterset_class = ArticleFilter
    time_period_to_show = 200
    ordering_fields = ["date"]
    ordering = ["-date"]
    pagination_class = UserPagination

    def get_limited_queryset(self, device_id, exclude_ids=[], use_basefilter=True):
        """
        Get a query set limited by time and without bookmarked / archived articles.

        Args:
            device_id (str): device id
            exclude_ids (list): list of ids to exclude from queryset
            use_basefilter (bool): use all base filters or if set to False
                only  DeviceIdFilter and  SourceActiveFilter

        Returns:
            queryset, excluded_articles_ids, oldest_date
        """
        # Define the current date and the oldest date for the query
        current_date = datetime.now().date()
        oldest_date = current_date - timedelta(days=self.time_period_to_show)
        app_user = get_appuser(device_id)

        # Define the list of article ids to exclude
        exclude_article_ids = exclude_ids
        #if app_user:
        #    # Add the ids of the user's archived and bookmarked articles to the list of ids to exclude
        #    exclude_article_ids += [
        #        article.id for article in app_user.archived_articles.all()
        #    ]
        #    exclude_article_ids += [
        #        article.id for article in app_user.bookmarked_articles.all()
        #    ]

        # If not using base filter, set the filter backends to DeviceIdFilter and SourceActiveFilter
        if not use_basefilter:
            self.filter_backends = [DeviceIdFilter, SourceActiveFilter]

        # Define the queryset
        self.queryset = Article.objects.filter(published=True).prefetch_related("translations")
        queryset = self.filter_queryset(self.get_queryset())

        # Get or create the AppUser object for the given device id
        app_user, created = AppUser.objects.get_or_create(device_id=device_id)

        # Gebiet des Geräts, sonst das erste aus DEFAULT_AREA_IDS (None = kein Gebietsfilter)
        area_id = app_user.area_id or default_area_id()

        # If the AppUser object was just created, assign all tags and active organizations to the user
        if created:
            # Assign all tags to the user if they don't have any
            user_tags = app_user.tags.values()
            if len(user_tags) == 0:
                all_tags = Tag.objects.all().values()
                for tag in all_tags:
                    app_user.tags.add(tag["id"])
                app_user.save()

            # Assign all active organizations to the user if they don't have any
            user_organisations = app_user.organization_all_tags.values()
            if len(user_organisations) == 0:
                all_organisations = Organization.objects.filter(active=True).values()
                for org in all_organisations:
                    app_user.organization_all_tags.add(org["id"])
                    app_user.organization.add(org["id"])
                app_user.save()

        # Exclude bookmarked and archived articles, filter by area and date
        queryset = (
            queryset.exclude(id__in=exclude_article_ids)
            .filter(date__gte=oldest_date.strftime("%Y-%m-%d"))
        )

        # Filter by areas if provided, otherwise filter by user area
        areas = self.request.query_params.get("areas", None)
        if areas:
            try:
                area_ids = [int(area_id) for area_id in areas.split(",")]
                app_user.areas.clear()
                for area_id in area_ids:
                    area = Area.objects.get(id=area_id)
                    app_user.areas.add(area)

                queryset = queryset.filter(area__id__in=area_ids)
            except ValueError:
                raise ValidationError("Invalid area IDs provided.")
        else:
            queryset = queryset.filter(**area_filter(area_id))
            
        return queryset, exclude_article_ids, oldest_date


    @swagger_auto_schema(
        manual_parameters=[
            string_parameter(
                "search", "Search term - searches in title, abstract, content and tags"
            ),
            choices_parameter(
                "ordering", ("date", "-date"), 'Ordering (default is "-date")'
            ),
            choices_parameter(
                "language", ("de", "en"), "Filter by language, defaults to all languages"
            ),
            openapi.Parameter(
                "areas",
                openapi.IN_QUERY,
                description="List of area IDs - comma separated",
                type=openapi.TYPE_STRING,
            ),
        ]
        + [
            boolean_parameter(
                article_type[0],
                'Include articles with type "{}", defaults to true'.format(
                    article_type[0]
                ),
                default=True,
            )
            for article_type in ORGANIZATION_TYPE_CHOICES
        ]
        + [
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        operation_description="""List published articles.\n
        If any of the parameters "search, tag, organization_all_tags, organization" is supplied
         then no articles flagged as "is_hot" will be inserted as the first article.
        In addition using one of the parameters "tag, organization_all_tags, organization" will supersede stored user settings and filters. 
         """,
        responses={400: "Device ID missing."},
    )
    def list(self, *args, **kwargs):
        # Get the device id from the request headers
        device_id = self.request.headers.get("X-Device-ID", None)
        if device_id is None:
            return bad_request("Device ID is missing")

        # Get the limited queryset
        queryset, exclude_article_ids, oldest_date = self.get_limited_queryset(
            device_id
        )

        # Check if any of the parameters "organization", "organization_all_tags", "tags", "search" are supplied
        parameters = self.request.query_params
        ignore_is_hot = any(
            [
                keyword in parameters.keys() and parameters.get(keyword, "") != ""
                for keyword in [
                    "organization",
                    "organization_all_tags",
                    "tags",
                    "search",
                    "language",
                ]
            ]
        )

        # Get the user's area id, if it doesn't exist, use DEFAULT_AREA_IDS
        app_user = get_appuser(device_id)

        # Gebiet des Geräts, sonst das erste aus DEFAULT_AREA_IDS (wie der Feed)
        area_id = (app_user.area_id if app_user else None) or default_area_id()

        # Assign all tags and active organizations to the user if they don't have any
        user_tags = app_user.tags.values()
        if len(user_tags) == 0:
            all_tags = Tag.objects.all().values()
            for tag in all_tags:
                app_user.tags.add(tag["id"])
        user_organisations = app_user.organization_all_tags.values()
        if len(user_organisations) == 0:
            all_organisations = Organization.objects.filter(active=True).values()
            for org in all_organisations:
                try:
                    app_user.organization_all_tags.add(org["id"])
                    app_user.organization.add(org["id"])
                except Exception as e:
                    logger.error(e)
            app_user.save()

        # Filter by areas if provided
        areas = parameters.get("areas", None)

        # If areas are provided, filter the queryset by area ids
        if areas is None:
            queryset = queryset.filter(**area_filter(area_id))

        if areas:
            try:
                area_ids = [int(area_id) for area_id in areas.split(",")]
                queryset = queryset.filter(area__id__in=area_ids)
                area_id = area_ids[0] 
            except:
                raise ValidationError("Invalid area IDs provided.")
        
        # area_id is first element of the list area_ids
        
        # Get the hottest article if it exists and the "is_hot" flag is not ignored
        hottest = None
        is_hot_queryset = get_is_hot_queryset(exclude_article_ids, oldest_date, area_id)
        if len(is_hot_queryset) > 0 and not ignore_is_hot:
            hottest = is_hot_queryset[0]
            queryset = queryset.exclude(id=hottest.id)
            querysetlist = list(queryset)
            for element in querysetlist:
                element.is_hot = False
            articles = [hottest] + querysetlist
            page = self.paginate_queryset(articles)
        else:
            page = self.paginate_queryset(queryset)

        # Serialize the page and return the paginated response
        if page is not None:
            serializer = self.get_serializer(page, many=True)

            # Increment request_count asynchron für Artikel auf dieser Seite
            article_ids = sorted([article.id for article in page])
            if article_ids:
                update_article_request_counts_async(article_ids)

            return self.get_paginated_response(serializer.data)

        # Serialize the queryset and return the response
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    
    @swagger_auto_schema(
    manual_parameters=[
        header_string_parameter("X-Device-ID", "Device ID", required=False)
    ],
    operation_description="Retrieve a single article. Optional: X-Device-ID header to determine bookmarked state."
    )
    def retrieve(self, request, *args, **kwargs):
        self.queryset = Article.objects.all()
        article = get_object_or_404(Article, pk=kwargs.get('pk'))
        if not article.published and not can_view_unpublished(request, article):
            return Response(status=status.HTTP_404_NOT_FOUND)
        return super().retrieve(request, *args, **kwargs)
    
    # Define the get_serializer_class method, which returns the appropriate serializer class based on the action
    def get_serializer_class(self):
        action = self.action.lower()
        if action == "list":
            self.serializer_class = ArticleListSerializer
        return super().get_serializer_class()
   
    # define a swagger auto schema for a delete method and also the delete method   
    @swagger_auto_schema(
        operation_description="Delete an article",
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        responses={
            204: "Deleted",
            400: "Invalid Device ID",
            404: "Article not found",
        },
    )
    def destroy(self, request, *args, **kwargs):
        """
        Delete an article.

        Args:
            request (Request): The request object.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            Response: The response object.
        """

        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return Response({"detail": "Authorization header missing."}, status=status.HTTP_401_UNAUTHORIZED)

        token = auth_header

        try:
            payload = decode_access_token(token)
            
            user_id = payload.get('user_id')
            if not user_id:
                return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)
            
            user = get_object_or_404(User, id=user_id)
        except jwt.ExpiredSignatureError:
            return Response({"detail": "Token has expired."}, status=status.HTTP_401_UNAUTHORIZED)
        except jwt.InvalidTokenError:
            return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)

        if user.is_authenticated:
   
            # Handle DELETE request logic here
            # Get the id from the last section of the URL in the request metapass
            url_parts = self.request.META['PATH_INFO'].split('/')
            article_id = url_parts[-2]

            # Retrieve the article
            article = get_object_or_404(Article, id=article_id)

            # Inhalte anlegen/ändern/löschen nur mit Redaktionsrolle (content.permissions)
            if not has_content_role(user):
                return Response({"detail": "Account has no content role."}, status=status.HTTP_403_FORBIDDEN)
            source = related_source(user)

            # Check if the user is the source of the article
            if article.source == source:
                # Delete the article
                article.delete()
            else: 
                return Response(status=status.HTTP_401_UNAUTHORIZED)    
        else:
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        # Return a response with status code 204
        return Response(status=status.HTTP_204_NO_CONTENT)

    # define the swagger auto schema for the create method
    @swagger_auto_schema(
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "title": openapi.Schema(type=openapi.TYPE_STRING),
                "abstract": openapi.Schema(type=openapi.TYPE_STRING),
                "content": openapi.Schema(type=openapi.TYPE_STRING),
                "link": openapi.Schema(type=openapi.TYPE_STRING),
                "published": openapi.Schema(type=openapi.TYPE_BOOLEAN),
                "area": openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                ),
                "article_tags": openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                ),  
                "date": openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE),
            },
        ),
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        responses={
            201: openapi.Response(
                description="Event created",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'id': openapi.Schema(type=openapi.TYPE_INTEGER)
                    }
                )
            ),
            400: "Invalid Request",
        },
    )
    def create(self, request, *args, **kwargs):

        # get the user from the token
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return Response({"detail": "Authorization header missing."}, status=status.HTTP_401_UNAUTHORIZED)

        token = auth_header

        try:
            payload = decode_access_token(token)
            user_id = payload.get('user_id')
            if not user_id:
                return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)

            user = get_object_or_404(User, id=user_id)
        except jwt.ExpiredSignatureError:
            return Response({"detail": "Token has expired."}, status=status.HTTP_401_UNAUTHORIZED)
        except jwt.InvalidTokenError:
            return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)

        # only delete the user if the user is authenticated
        if user.is_authenticated:

            # Artikel entstehen unter der Quelle, deren related_user dieses Konto ist
            # Inhalte anlegen/ändern/löschen nur mit Redaktionsrolle (content.permissions)
            if not has_content_role(user):
                return Response({"detail": "Account has no content role."}, status=status.HTTP_403_FORBIDDEN)
            source = related_source(user)
            if source is None:
                return Response({"detail": "No source is linked to this account."}, status=status.HTTP_400_BAD_REQUEST)

            # Gebiete und Themen vor dem Speichern prüfen, damit keine halben Artikel entstehen
            if request.data.get('area'):
                area_ids = request.data['area']
            else:
                area_ids = list(source.area.values_list('id', flat=True))
                if not area_ids:
                    return Response({"detail": "No area specified and organization has no default area."}, status=400)
            tag_ids = request.data.get('article_tags')
            if not tag_ids:
                return Response({"detail": "article_tags is required."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                areas = list(Area.objects.filter(pk__in=area_ids))
                tags = list(Tag.objects.filter(pk__in=tag_ids))
            except (TypeError, ValueError):
                return Response({"detail": "Invalid area or article_tags."}, status=status.HTTP_400_BAD_REQUEST)
            if len(areas) != len(set(area_ids)) or len(tags) != len(set(tag_ids)):
                return Response({"detail": "Unknown area or article_tags ID."}, status=status.HTTP_400_BAD_REQUEST)

            # Handle POST request logic here
            article = Article()
            if 'title' in request.data:
                article.title = request.data['title']
            if 'abstract' in request.data:
                article.abstract = request.data['abstract']
            if 'content' in request.data:
                article.content = request.data['content']
            article.source = source

            if 'link' in request.data:
                if not is_safe_link(request.data['link']):
                    return Response({"detail": "Invalid link."}, status=status.HTTP_400_BAD_REQUEST)
                article.link = request.data['link']
            if 'date' in request.data:
                article.date = request.data['date']


            # Veröffentlichen nur mit Veröffentlichungsrecht, sonst zur Prüfung einreichen
            article.published = bool(request.data.get('published', True)) and can_publish(user)
            article.up_for_review = not article.published

            # Duplicate-Detection: Prüfe ob identischer Artikel in letzten 24h existiert
            check_window = timezone.now() - timedelta(hours=24)

            if article.title and article.content:
                duplicate_exists = Article.objects.filter(
                    title=article.title,
                    content=article.content,
                    source=source,
                    date__gte=check_window
                ).exists()

                if duplicate_exists:
                    return Response(
                        {"detail": "Ein identischer Artikel wurde bereits in den letzten 24 Stunden erstellt."},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            with transaction.atomic():
                article.save()
                article.area.add(*areas)
                article.tags.add(*tags)


        else:
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        # Return a 201 Created response with the article id
        return Response({"id": article.id}, status=status.HTTP_201_CREATED)

    @swagger_auto_schema(
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "title": openapi.Schema(type=openapi.TYPE_STRING),
                "abstract": openapi.Schema(type=openapi.TYPE_STRING),
                "content": openapi.Schema(type=openapi.TYPE_STRING),
                "link": openapi.Schema(type=openapi.TYPE_STRING),
                "published": openapi.Schema(type=openapi.TYPE_BOOLEAN),
                "area": openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                ),
                "article_tags": openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                ),  
                "date": openapi.Schema(type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE),
            },
        ),
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        responses={
            204: "No Content",
            400: "Invalid Request",
        },
    )
    def update(self, request, *args, **kwargs):
        """
        Update an article.

        Args:
            request (Request): The request object.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            Response: The response object.
        """

        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return Response({"detail": "Authorization header missing."}, status=status.HTTP_401_UNAUTHORIZED)

        token = auth_header

        try:
            payload = decode_access_token(token)
            
            user_id = payload.get('user_id')
            if not user_id:
                return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)
            
            user = get_object_or_404(User, id=user_id)
        except jwt.ExpiredSignatureError:
            return Response({"detail": "Token has expired."}, status=status.HTTP_401_UNAUTHORIZED)
        except jwt.InvalidTokenError:
            return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)

        if user.is_authenticated:

            # Inhalte anlegen/ändern/löschen nur mit Redaktionsrolle (content.permissions)
            if not has_content_role(user):
                return Response({"detail": "Account has no content role."}, status=status.HTTP_403_FORBIDDEN)
            source = related_source(user)

            # Get the id from the last section of the URL in the request metapass
            url_parts = self.request.META['PATH_INFO'].split('/')
            article_id = url_parts[-2]

            # Retrieve the article
            article = get_object_or_404(Article, id=article_id)

            # Check if the user is the source of the article
            if article.source == source:

                link = request.data.get('link')
                if link is not None and not is_safe_link(link):
                    return Response({"detail": "Invalid link."}, status=status.HTTP_400_BAD_REQUEST)

                # Ohne Veröffentlichungsrecht gehen Änderungen zur Prüfung
                # (auch bei bereits veröffentlichten Artikeln)
                if not can_publish(user):
                    article.published = False
                    article.up_for_review = True

                # Update the article
                for field, value in request.data.items():
                    if field == 'title' and value:
                        article.title = value
                    elif field == 'abstract' and value:
                        article.abstract = value
                    elif field == 'content' and value:
                        article.content = value
                    elif field == 'link' and value is not None:
                        article.link = value
                    elif field == 'date' and value:
                        article.date = value
                    elif field == 'published' and value is not None:
                        article.published = bool(value) and can_publish(user)
                    elif field == 'area':
                        # Remove all areas from the article
                        article.area.clear()
                        # Add the new areas to the article
                        for area in value:
                            article.area.add(area)

                    elif field == 'article_tags':
                        # Remove all tags from the article
                        article.tags.clear()
                        # Add the new tags to the article
                        for tag in value:
                            article.tags.add(tag)

                    else:
                        logger.error(f"Unknown field: {field}")
                
                    # save the article
                    article.save()
            
            # if article.source == source
            else: 
                return Response(status=status.HTTP_401_UNAUTHORIZED) 
        
        # if user.is_authenticated
        else:
            return Response(status=status.HTTP_401_UNAUTHORIZED)


        # Return a 204 No Content response
        return Response(status=status.HTTP_204_NO_CONTENT)

   
    # Define the similar method, which handles requests for similar articles
    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
            integer_parameter("limit", "Number of results to return per page."),
            integer_parameter(
                "offset", "The initial index from which to return the results."
            ),
        ],
        responses={
            200: PaginatedArticleRetrieveSerializer,
            400: "Device ID missing.",
            404: "No such article.",
        },
    )
    @action(detail=True)
    def similar(self, request, *args, pk=None, **kwargs):
        return super().similar(request, *args, pk=pk, **kwargs)
    
    


class ArticleAppUserBaseViewSet(AppUserBaseViewSet):
    """
    This class is a base ViewSet for the Article model in the context of an AppUser.
    It extends the AppUserBaseViewSet class.
    It includes several properties for handling different types of requests.
    """

    # Define the queryset, serializer class, pagination class, many-to-many field name, time period to show, and filter backends
    queryset = Article.objects.all()  # All instances of the Article model
    serializer_class = ArticleBookmarkedArchivedSerializer  # Serializer class for the Article model
    pagination_class = UserPagination  # Pagination class for the Article model
    m2m_field_name = None  # Name of the many-to-many field in the Article model
    time_period_to_show = 30  # Time period (in days) to show in the queryset
    filter_backends = AppUserBaseViewSet.filter_backends + [
        DeviceIdFilter,  # Additional filter backend for filtering by device id
    ]

class ArticlePagination(LimitOffsetPagination):
    page_query_param = 'offset'
    page_size = 10  # Set the page size to your preference
    page_size_query_param = 'limit'  # Set the query parameter for page size
    max_page_size = 100  # Set the maximum page size
    offset_query_param = 'offset'  # Set the query parameter for offset


class ArticleArchiveViewSet(GenericViewSet):
    queryset = Article.objects.filter(published=True)
    serializer_class = ArticleListSerializer
    model_class = Article
    filter_backends = [DeviceIdFilter, SourceActiveFilter]
    pagination_class = ArticlePagination

    # Name of the many-to-many field for archived articles
    m2m_field_name = "archived_articles"

    @swagger_auto_schema(
        operation_description="List archived articles",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        responses={
            200: PaginatedArticleBookmarkedArchivedSerializer,
            400: "Invalid Device ID",
        },
    )
    def list(self, request, *args, **kwargs):
        """
        Lists the articles archived by the user.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
        Returns:
            A paginated list of archived articles.
        """
        device_id = request.headers.get('X-Device-ID')
        if not device_id:
            return Response({"detail": "Device ID is required."}, status=status.HTTP_400_BAD_REQUEST)

        app_user = get_appuser(device_id)
        if not app_user:
            return Response({"detail": "App user not found."}, status=status.HTTP_400_BAD_REQUEST)

        # Retrieve archived articles
        archived_articles = app_user.archived_articles.all()

        # Make a queryset out of the archived articles
        queryset = Article.objects.filter(id__in=[article.id for article in archived_articles])

        # Paginate the queryset
        page = self.paginate_queryset(queryset)
        if page is not None:
            # If pagination is applied, serialize the paginated data
            serializer = self.get_serializer(page, many=True)
            # Return the paginated response
            return self.get_paginated_response(serializer.data)

        # If no pagination is applied, serialize the full queryset
        serializer = self.get_serializer(queryset, many=True)
        # Return the full response
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Add article to archive",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={},
            required=[],
            description='No data is required in the request body'
        ),
        responses={
            201: "Added to archive",
            400: "Invalid Device ID",
            404: "Article not found",
        },
    )
    def update(self, request, *args, **kwargs):
        """
        Adds an article to the user's archive.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
            **kwargs: Additional keyword arguments.
        
        Returns:
            A response indicating the article was added to archive.
        """
        article_id = kwargs.get('pk')
        app_user = get_appuser(request.META.get('HTTP_X_DEVICE_ID'))
        if not app_user:
            return Response("Invalid Device ID", status=status.HTTP_400_BAD_REQUEST)
        
        try:
            article = Article.objects.get(id=article_id)
        except Article.DoesNotExist:
            return Response("Article not found", status=status.HTTP_404_NOT_FOUND)

        # if article not found return 404
        if not article:
            return Response("Article not found", status=status.HTTP_404_NOT_FOUND)
        app_user.archived_articles.add(article)
        return Response(status=status.HTTP_201_CREATED)
    
    @swagger_auto_schema(
        operation_description="Remove article from archive",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={
            204: "Removed from archive",
            400: "Invalid Device ID",
            404: "Article not found",
        },
    )
    def destroy(self, request, *args, **kwargs):
        """
        Removes an article from the user's archive.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
            **kwargs: Additional keyword arguments.
        
        Returns:
            A response indicating the article was removed from archive.
        """
        article_id = kwargs.get('pk')
        article = get_object_or_404(Article, id=article_id)
        device_id = request.META.get("HTTP_X_DEVICE_ID")
        app_user = get_appuser(device_id)
        
        if not app_user:
            return Response("Invalid Device ID", status=status.HTTP_400_BAD_REQUEST)
        
        if article not in app_user.archived_articles.all():
            return Response("Article not found", status=status.HTTP_404_NOT_FOUND)
        
        app_user.archived_articles.remove(article)
        return Response(status=status.HTTP_204_NO_CONTENT)

# Define an empty serializer
class EmptySerializer(serializers.Serializer):
    pass

class PaginatedArticleListSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    previous = serializers.URLField(allow_blank=True, allow_null=True, required=False)
    results = ArticleListSerializer(many=True)

@method_decorator(csrf_exempt, name='dispatch')
class ArticleBookmarksViewSet(GenericViewSet):
    queryset = Article.objects.filter(published=True).prefetch_related("translations")
    serializer_class = ArticleRetrieveSerializer
    model_class = Article
    filter_backends = [DeviceIdFilter, SourceActiveFilter]
    pagination_class = ArticlePagination
    m2m_field_name = "bookmarked_articles"
    pagination_class = UserPagination

    def get_serializer_class(self):
       
        if self.action == 'list':
            return ArticleRetrieveSerializer
        elif self.action == 'update':
            return None

        if getattr(self, 'swagger_fake_view', False):
            # If the request is made by Swagger for schema generation, return the default serializer class
            return super().get_serializer_class()
        

        return super().get_serializer_class()


    @swagger_auto_schema(
        operation_description="List bookmarked articles",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        
        responses={
            200: PaginatedArticleListSerializer,
            400: "Invalid Device ID",
        },
    )
    def list(self, request, *args, **kwargs):
        """
        Lists the articles bookmarked by the user.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
        Returns:
            A paginated list of bookmarked articles.
        """
        device_id = request.headers.get('X-Device-ID')
        if not device_id:
            return Response({"detail": "Device ID is required."}, status=status.HTTP_400_BAD_REQUEST)

        app_user = get_appuser(device_id)
        if not app_user:
            return Response({"detail": "App user not found."}, status=status.HTTP_400_BAD_REQUEST)

        # Retrieve bookmarked articles
        bookmarked_articles = app_user.bookmarked_articles.all()

        # Make a queryset out of the bookmarked articles
        queryset = Article.objects.filter(id__in=[article.id for article in bookmarked_articles]).order_by('-date')

        # Paginate the queryset
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        # Fallback falls Pagination ausgeschaltet ist
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Add article to bookmarks",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        request_body=None,
        responses={
            201: "Added to bookmarks",
            400: "Invalid Device ID",
            404: "Article not found",
        },
    )
    def update(self, request, *args, **kwargs):
        """
        Adds an article to the user's bookmarks.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
            **kwargs: Additional keyword arguments.
        
        Returns:
            A response indicating the article was added to bookmarks.
        """
        # Override get_serializer to ensure no data is expected
        def get_serializer(*args, **kwargs):
            return None

        article_id = kwargs.get('pk')
        app_user = get_appuser(request.META.get('HTTP_X_DEVICE_ID'))
        if not app_user:
            return Response("Invalid Device ID", status=status.HTTP_400_BAD_REQUEST)
        
        try:
            article = Article.objects.get(id=article_id)
        except Article.DoesNotExist:
            return Response("Article not found", status=status.HTTP_404_NOT_FOUND)
        # if article not found return 404
        if not article:
            return Response("Article not found", status=status.HTTP_404_NOT_FOUND)
        app_user.bookmarked_articles.add(article)

        logger.error(f"Article {article_id} added to bookmarks for user {app_user.device_id}")
        
        return Response(status=status.HTTP_201_CREATED)
    
    @swagger_auto_schema(
        operation_description="Remove article from bookmarks",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        responses={
            204: "Removed from bookmarks",
            400: "Invalid Device ID",
            404: "Article not found",
        },
    )
    def destroy(self, request, *args, **kwargs):
        """
        Removes an article from the user's bookmarks.
        
        Args:
            request: The HTTP request object.
            *args: Additional arguments.
            **kwargs: Additional keyword arguments.
        
        Returns:
            A response indicating the article was removed from bookmarks.
        """
        article_id = kwargs.get('pk')
        article = get_object_or_404(Article, id=article_id)
        device_id = request.META.get("HTTP_X_DEVICE_ID")
        app_user = get_appuser(device_id)
        
        if not app_user:
            return Response("Invalid Device ID", status=status.HTTP_400_BAD_REQUEST)
        
        if article not in app_user.bookmarked_articles.all():
            return Response("Article not found", status=status.HTTP_404_NOT_FOUND)
        
        app_user.bookmarked_articles.remove(article)
        return Response(status=status.HTTP_204_NO_CONTENT)


    
class ArticleFlagViewSet(viewsets.ViewSet):
    queryset = Article.objects.all()

    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'article_id': openapi.Schema(type=openapi.TYPE_INTEGER),
            }
        ),
        responses={
            204: "Flagged article",
            400: "Device ID missing.",
            404: "No such article.",
        },
    )
    def flag_article(self, request, *args, **kwargs):

        article_id =  request.data['article_id']

        if article_id is None:
            return Response(data={"message": "Article ID is missing"}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Send confirmation email
            sender_email = settings.MAIL_SENDING['DEFAULT_FROM_EMAIL']
            receiver_email = settings.FEEDBACK_MAIL_TARGET or settings.MAIL_SENDING.get("ERROR_NOTIFICATION_EMAIL")
            if not receiver_email:
                return Response(data={"message": "Kein Empfänger für Meldungen konfiguriert"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            subject = 'Problematic Article Reported'
            
            message = f'<html><body>Article ID: {article_id}<br>This article has been flagged as problematic.</br>'
            try:
                # add the article title and description to the message
                article = Article.objects.get(id=article_id)
                message += f'Title: {article.title}<br>Abstract: {article.abstract}</br>'
                # add the source name of the article to the message
                message += f'Source: {article.source.name}</body></html>'
            except: 
                pass            
            
            send_html_mail(subject, message, receiver_email, sender_email)

        except Exception as e:
            return Response(data={"message": f"Error sending confirmation email: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        
        return Response(status=status.HTTP_204_NO_CONTENT)

class ArticleTagReceiveViewSet(viewsets.ViewSet):
    queryset = Tag.objects.all()

    # route that returns all possible tags for an article
    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={
            200: ArticleTagsSerializer(many=True),
            400: "Device ID missing.",
        },
    )
    def get_all_tags(self, request, *args, **kwargs):

        try:
            # Ermitteln des AppUser anhand der device_id
            device_id = request.headers.get("X-Device-ID")
            app_user = get_appuser(device_id)

            # Tags abrufen (nur Artikel-Tags)
            tags = Tag.objects.filter(type='article')
            tags = tags.order_by('name')
            tags = sorted(tags, key=lambda tag: tag.name if tag.name != "andere Sportarten" else "Fußball" + tag.name)

            # Serialisierung der Tags mit dem zusätzlichen Feld "selected"
            serializer = ArticleTagsSerializer(tags, many=True, context={'app_user': app_user})

        except Exception as e:
            return Response(data={"message": f"Error retrieving tags: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        return Response(serializer.data)
    

class CombinedViewSetArticle(BaseViewSet):

    # Define the queryset, serializer class, model class, filter backends, filterset class, time period to show, ordering fields, ordering, and pagination class
    queryset = Article.objects.filter(published=True).select_related('area')
    serializer_class = ArticleRetrieveSerializerUser
    model_class = Article
    filter_backends = BaseViewSet.filter_backends + [
        DeviceIdFilter,
    ]
    filterset_class = ArticleFilter
    time_period_to_show = 14
    ordering_fields = ["date"]
    ordering = ["-date"]
    pagination_class = UserPagination
    
    @swagger_auto_schema(
        operation_description="List articles for a specific user.",
        manual_parameters=[
            header_string_parameter("Authorization", "JWT token", required=True),
        ],
        responses={
            200: PaginatedArticleRetrieveSerializerUser,
            400: "Device ID missing.",
            401: "Unauthorized",
        },
    )
    @action(detail=False, methods=['get'], url_path='user-articles')
    def list_user_articles(self, request, *args, **kwargs):
        """
        List articles for a specific user.
        """
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return Response({"detail": "Authorization header missing."}, status=status.HTTP_401_UNAUTHORIZED)

        token = auth_header

        try:
            payload = decode_access_token(token)
            user_id = payload.get('user_id')
            if not user_id:
                return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)
            
            user = get_object_or_404(User, id=user_id)
        except jwt.ExpiredSignatureError:
            return Response({"detail": "Token has expired."}, status=status.HTTP_401_UNAUTHORIZED)
        except jwt.InvalidTokenError:
            return Response({"detail": "Invalid token."}, status=status.HTTP_401_UNAUTHORIZED)

        if user.is_authenticated:
            # Optimierte Queryset - area ist ein M2M Feld, daher prefetch_related verwenden
            queryset = Article.objects.filter(
                source__related_user=user  # Filter direkt anwenden
            ).select_related(
                'source'  # N+1 Problem für Source lösen
            ).prefetch_related(
                'area',  # M2M Feld für Areas
                'articles_bookmarked',  # Bookmarks vorladen für bessere Performance
                'tags'  # Tags sind auch M2M, falls im Serializer verwendet
            ).annotate(
                bookmarked_count=Count('articles_bookmarked')
            ).order_by('-date')

            # Paginate the queryset if pagination is set up
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)

            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data)
        else:
            return Response(status=status.HTTP_401_UNAUTHORIZED)

class ArticleListRetrieveSerializer(ArticleRetrieveSerializer):
    """Serializer für list_articles mit gekürztem Content."""
    content = serializers.SerializerMethodField()

    class Meta(ArticleRetrieveSerializer.Meta):
        pass

    
    def get_content(self, instance):
        if instance.content:
            return instance.content[:150] + "..." if len(instance.content) > 150 else instance.content
        return None
       
class ArticleListViewSet(BaseViewSet):
    """
    This methods can be used to create, update, delete and list articles
    """

    # Define the queryset, serializer class, model class, filter backends, filterset class, time period to show, ordering fields, ordering, and pagination class
    queryset = Article.objects.filter(published=True).prefetch_related("translations")
    serializer_class = ArticleRetrieveSerializer
    model_class = Article
    filter_backends = BaseViewSet.filter_backends + [
        DeviceIdFilter,
    ]
    time_period_to_show = 14
    ordering_fields = ["date"]
    ordering = ["-date"]
    pagination_class = UserPagination

    def get_limited_queryset(self, device_id, exclude_ids=[], use_basefilter=True):
        """
        Get a query set limited by time and without bookmarked / archived articles.

        Args:
            device_id (str): device id
            exclude_ids (list): list of ids to exclude from queryset
            use_basefilter (bool): use all base filters or if set to False
                only  DeviceIdFilter and  SourceActiveFilter

        Returns:
            queryset, excluded_articles_ids, oldest_date
        """
        # Define the current date and the oldest date for the query
        current_date = datetime.now().date()
        oldest_date = current_date - timedelta(days=self.time_period_to_show)
        app_user = get_appuser(device_id)

        # Define the list of article ids to exclude
        exclude_article_ids = exclude_ids

        # If not using base filter, set the filter backends to DeviceIdFilter and SourceActiveFilter
        if not use_basefilter:
            self.filter_backends = [DeviceIdFilter, SourceActiveFilter]

        # Define the queryset
        self.queryset = Article.objects.filter(published=True).prefetch_related("translations")
        queryset = self.filter_queryset(self.get_queryset())

        # Get or create the AppUser object for the given device id
        app_user, created = AppUser.objects.get_or_create(device_id=device_id)

        # Gebiet des Geräts, sonst das erste aus DEFAULT_AREA_IDS (None = kein Gebietsfilter)
        area_id = app_user.area_id or default_area_id()

        # If the AppUser object was just created, assign all tags and active organizations to the user
        if created:
            # Assign all tags to the user if they don't have any
            user_tags = app_user.tags.values()
            if len(user_tags) == 0:
                all_tags = Tag.objects.all().values()
                for tag in all_tags:
                    app_user.tags.add(tag["id"])
                app_user.save()

            # Assign all active organizations to the user if they don't have any
            user_organisations = app_user.organization_all_tags.values()
            if len(user_organisations) == 0:
                all_organisations = Organization.objects.filter(active=True).values()
                for org in all_organisations:
                    app_user.organization_all_tags.add(org["id"])
                    app_user.organization.add(org["id"])
                app_user.save()

        # Exclude bookmarked and archived articles, filter by area and date
        queryset = (
            queryset.exclude(id__in=exclude_article_ids)
            .filter(date__gte=oldest_date.strftime("%Y-%m-%d"))
        )

        # Filter by areas if provided, otherwise filter by user area
        areas = self.request.query_params.get("areas", None)
        if areas:
            try:
                area_ids = [int(area_id) for area_id in areas.split(",")]
                app_user.areas.clear()
                for area_id in area_ids:
                    area = Area.objects.get(id=area_id)
                    app_user.areas.add(area)

                queryset = queryset.filter(area__id__in=area_ids)
            except ValueError:
                raise ValidationError("Invalid area IDs provided.")
        else:
            queryset = queryset.filter(**area_filter(area_id))
            
        return queryset, exclude_article_ids, oldest_date


    @swagger_auto_schema(
        manual_parameters=[
            string_parameter("search", "Search term - searches in title, abstract, content and tags"),
            openapi.Parameter(
                'ignore_user_selection',
                openapi.IN_QUERY,
                description="Ignore user tag and organization selection",
                type=openapi.TYPE_BOOLEAN,
                required=False,
                default=False
            ),
            *[ boolean_parameter(
                article_type[0],
                'Include articles with type "{}", defaults to true'.format(
                    article_type[0]
                ),
                default=True,
            )
            for article_type in ORGANIZATION_TYPE_CHOICES ],
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        operation_description="List published articles based on user's areas and tags. Optional: ignore_user_selection=True um die Tag-Auswahl des Users zu ignorieren.",
    )
    def list_articles(self, request, *args, **kwargs):
       
        self.serializer_class = ArticleListRetrieveSerializer
        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return bad_request("Device ID is missing")

        app_user, created = AppUser.objects.get_or_create(device_id=device_id)

        # If the AppUser object was just created, assign all tags and active organizations to the user
        if created:
            # Assign all tags to the user if they don't have any
            user_tags = app_user.tags.values()
            if len(user_tags) == 0:
                all_tags = Tag.objects.all().values()
                for tag in all_tags:
                    app_user.tags.add(tag["id"])
                app_user.save()

            # Assign all active organizations to the user if they don't have any
            user_organisations = app_user.organization_all_tags.values()
            if len(user_organisations) == 0:
                all_organisations = Organization.objects.filter(active=True).values()
                for org in all_organisations:
                    app_user.organization_all_tags.add(org["id"])
                    app_user.organization.add(org["id"])
                app_user.save()

        # Query-Parameter in Boolean wandeln
        ignore_user_selection = request.query_params.get("ignore_user_selection", "false")
        ignore_user_selection = str(ignore_user_selection).lower() in ("1", "true", "yes")

        # Areas des Users holen (wie gehabt)
        user_area_ids = list(app_user.areas.values_list("id", flat=True))
        if not user_area_ids:
            # Fallback: single area
            if app_user.area_id:
                user_area_ids = [app_user.area_id]
            else:
                return bad_request("No area set for AppUser")
        
        # NUR Artikel aus abonnierten Organisationen und passenden Tags
        user_organization_ids = list(app_user.organization_all_tags.values_list("id", flat=True))
        user_organization_ids_other = list(app_user.organization.values_list("id", flat=True))
        user_organization_ids = list(set(user_organization_ids + user_organization_ids_other))

        # Remove deselected organizations
        deselected_org_ids = list(app_user.deselected_organization.values_list("id", flat=True))
        deselected_org_all_tags_ids = list(app_user.deselected_organization_all_tags.values_list("id", flat=True))
        all_deselected_ids = set(deselected_org_ids + deselected_org_all_tags_ids)

        if all_deselected_ids:
            user_organization_ids = [org_id for org_id in user_organization_ids if org_id not in all_deselected_ids]

        # get all sources that belong to the organizations
        user_sources = list(Source.objects.filter(organization__id__in=user_organization_ids).values_list("id", flat=True))

        if not user_organization_ids:
            return bad_request("No organizations set for AppUser")
        
        if ignore_user_selection:
            # Alle Artikel in den Areas des Users, KEIN Tag-Filter
            queryset = Article.objects.filter(
                published=True,
                area__id__in=user_area_ids,
                source__id__in=user_sources,
            ).distinct("date", "id") 
        else:
                        
            user_tag_ids = list(app_user.tags.values_list("id", flat=True))
            if not user_tag_ids:
                return bad_request("No tags set for AppUser")
            
            queryset = Article.objects.filter(
                published=True,
                area__id__in=user_area_ids,
                tags__id__in=user_tag_ids,
                source__id__in=user_sources,
            ).distinct("date", "id") 
        
        org_type_filter = []
        for org_type, _ in ORGANIZATION_TYPE_CHOICES:
            param = request.query_params.get(org_type)
            # Standard: alle true, also alle Typen anzeigen, außer explizit false
            if param is not None and str(param).lower() in ("0", "false", "no"):
                continue
            org_type_filter.append(org_type)

        queryset = queryset.filter(source__organization__type__in=org_type_filter)
        
        # Artikel ohne Titel entfernen
        queryset = queryset.exclude(Q(title__isnull=True) | Q(title__exact=""))

        # Optionale Suche
        search = request.query_params.get("search")
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search)
                | Q(abstract__icontains=search)
                | Q(content__icontains=search)
                | Q(tags__name__icontains=search)
            ).distinct("date", "id") 

        queryset = queryset.order_by("-date", "id")

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            
            # Asynchrones Update für request_count
            article_ids = sorted([article.id for article in page])
            if article_ids:
                update_article_request_counts_async(article_ids)

            data =  self.get_paginated_response(serializer.data)

            #for query in connection.queries:
            #    duration_ms = float(query["time"]) * 1000
            #    if duration_ms > 50:
            #        logger.info (f"SLOW QUERY ({duration_ms:.1f} ms): {query['sql']}")


            return data

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                "organization_id",
                openapi.IN_QUERY,
                description="ID der Organisation (Pflicht)",
                type=openapi.TYPE_INTEGER,
                required=True,
            ),
            header_string_parameter("X-Device-ID", "Device ID", required=False),
        ],
        operation_description="List all articles published by a specific organization (by organization id).",
        responses={200: PaginatedArticleRetrieveSerializer},
    )
    def list_articles_by_organization(self, request,*args, **kwargs):
        """
        List all published articles for a given organization.
        """
        org_id = request.query_params.get("organization_id")
        if not (org_id and org_id.isdigit()):
            return bad_request("organization_id missing or invalid")
        org_id = int(org_id)

        try:
            organization = Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist:
            return Response({"detail": "Organization not found."}, status=404)

        # Queryset: nur veröffentlichte Artikel, deren Source auf die Org zeigt
        queryset = (
            Article.objects.filter(published=True, source__organization=organization)
            .order_by("-date")
            .distinct()
        )

        # Optional: Suche nach Titel/Abstract/Tags
        search = request.query_params.get("search")
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) |
                Q(abstract__icontains=search) |
                Q(content__icontains=search) |
                Q(tags__name__icontains=search)
            ).distinct()

        # Pagination
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data, status=200)
