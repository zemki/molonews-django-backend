from logging import getLogger
from django.db.models import Q
from rest_framework import status
from rest_framework import viewsets, serializers
from rest_framework.response import Response
from drf_yasg.utils import swagger_auto_schema, swagger_serializer_method
from drf_yasg import openapi



from .util import (
    choices_parameter,
    header_string_parameter,
)
from content.choices import ORGANIZATION_TYPE_CHOICES
from content.models import AppUser, Tag, Organization, Category
from .category import CategoryTagsSerializer

logger = getLogger("molonews")


def bad_request(detail, return_status=status.HTTP_400_BAD_REQUEST):
    logger.error("Bad request: {}".format(detail))
    return Response({"detail": detail}, status=return_status)


class AppUserSerializer(serializers.ModelSerializer):

    class Meta:
        model = AppUser
        fields = (
            "device_id",
            "firebase_token",
            "tags",
            "organizations",
            "organization_all_tags",
            "location_latitude",
            "location_longitude",
        )


class TagListSerializer(serializers.ModelSerializer):

    class Meta:
        model = AppUser
        fields = ("tags",)


class EventTagListSerializer(serializers.ModelSerializer):

    class Meta:
        model = AppUser
        fields = (
            "tags",
            "filter_events_by_source",
        )


class EventCategorySerializer(serializers.ModelSerializer):

    class Meta:
        model = Category
        fields = (
            "id",
            "name",
            "title",
            "description",
            "rank",
        )


class EventTagListGetSerializer(serializers.ModelSerializer):

    category = EventCategorySerializer(read_only=True)
    tags = CategoryTagsSerializer(read_only=True, many=True)

    class Meta:
        model = AppUser
        fields = (
            "tags",
            "category",
            "filter_events_by_source",
        )


class AppUserBaseViewSet(viewsets.GenericViewSet):

    queryset = AppUser.objects.all()
    serializer_class = None
    http_method_names = ["get", "post"]
    app_user = None

    def get_user(self, create=True):
        device_id = self.request.headers.get("X-Device-ID", None)
        created = False
        if device_id is None:
            return None, bad_request("Device ID is missing")
        if create:
            app_user, created = AppUser.objects.get_or_create(device_id=device_id)
        else:
            try:
                app_user = AppUser.objects.get(device_id=device_id)
            except AppUser.DoesNotExist:
                return None, bad_request("Device ID does not exist")
            
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

        return app_user, None


class AppUserKnownViewSet(AppUserBaseViewSet):

    @swagger_auto_schema(
        operation_description="Is Device known?",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        responses={200: "Device ID is known", 400: "Device ID does not exist"},
    )
    def list(self, request, *args, **kwargs):
        """

        Args:
            request (request): request object
            *args (args): additional args
            **kwargs (kwargs): additional kwargs

        Returns:
            Response
        """
        app_user, error_response = self.get_user(create=False)
        if error_response:
            return error_response
        return Response(status.HTTP_200_OK)


class AppUserTagViewSet(AppUserBaseViewSet):

    serializer_class = TagListSerializer
    tag_filter = [1, 2, 3, 4]
    queryset = Tag.objects.all()

    @swagger_auto_schema(
        operation_description="Set selected tags",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={200: "User Settings saved", 400: "Device ID is missing"},
        request_body=TagListSerializer,
    )
    def create(self, request, *args, **kwargs):
        """

        Args:
            request (request): request object
            *args (args): additional args
            **kwargs (kwargs): additional kwargs

        Returns:
            Response
        """
        app_user, error_response = self.get_user()
        if error_response:
            logger.error(error_response)
            return error_response

        # get user tags to see if some already exist or if the list is empty
        user_tags = app_user.tags.values()

        # get all tags from db
        all_tags = Tag.objects.all().values()
        # add one tag after another to the user
        for tag in all_tags:
            app_user.tags.add(tag["id"])
        app_user.save()

        # get list of all organisations that the user requested
        user_organisations = app_user.organization.values()
        # if there is none the initial values need to be set
        if len(user_organisations) == 0:
            logger.error("user has no organisations")
            # get all organisations that are active from db
            all_organisations = Organization.objects.filter(active=True).values()
            # add all ids the the users list of organisations
            for org in all_organisations:
                app_user.organization_all_tags.add(org["id"])
            app_user.save()

        data = self.request.data
        # get already selected tags not matching tag filter
        user_tags = [
            tag for tag in app_user.tags.exclude(category_id__in=self.tag_filter)
        ]
        _tags = data.get("tags", None)
        user_tags += [_tag for _tag in Tag.objects.filter(id__in=_tags)]
        app_user.tags.set(user_tags)
        if self.serializer_class == EventTagListSerializer:
            filter_events_by_source = data.get("filter_events_by_source", False)
            app_user.filter_events_by_source = filter_events_by_source
        app_user.save()

        return Response(status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Get selected tags",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={200: TagListSerializer, 400: "Device ID is missing"},
    )
    def list(self, request, *args, **kwargs):
        _filter_events_by_source = True
        app_user, error_response = self.get_user()
        if app_user:
            _filter_events_by_source = app_user.filter_events_by_source

        queryset = self.get_queryset().filter(category_id__in=self.tag_filter)
        serializer = None
        if self.serializer_class == TagListSerializer:
            serializer = TagListSerializer({"tags": queryset})
        elif self.serializer_class in [
            EventTagListSerializer,
            EventTagListGetSerializer,
        ]:
            # serializer = self.serializer_class({'tags': queryset, 'filter_events_by_source': _filter_events_by_source})
            serializer = self.serializer_class(
                {
                    "tags": queryset,
                    "category": Category.objects.get(id=2),
                    "filter_events_by_source": _filter_events_by_source,
                },
                context={"device_id": request.headers.get("X-Device-ID", None)},
            )
        if serializer:
            return Response(serializer.data)


class AppUserArticleTagViewSet(AppUserTagViewSet):
    tag_filter = [1, 3, 4]


class AppUserEventTagViewSet(AppUserTagViewSet):

    serializer_class = EventTagListSerializer
    tag_filter = [2]

    @swagger_auto_schema(
        operation_description="Set selected event tags",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={200: "User Settings saved", 400: "Device ID is missing"},
        request_body=EventTagListSerializer,
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @swagger_auto_schema(
        operation_description="Get selected tags",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={200: EventTagListGetSerializer, 400: "Device ID is missing"},
    )
    def list(self, request, *args, **kwargs):
        self.serializer_class = EventTagListGetSerializer
        return super().list(request, *args, **kwargs)
    
    
def save_organizations(
    app_user, organization=None, organization_all_tags=None, organization_type=None
):
    """
    Setzt die ausgewählten Organisationen (und All-Tags) für den User,
    aber jeweils nur für Organisationen im (den) Gebiet(en) des Users.
    Organisationen außerhalb der User-Areas bleiben unverändert.
    """
    # Hole alle relevanten Area-IDs des Users (einzelne oder mehrere)
    if hasattr(app_user, "areas") and app_user.areas.exists():
        user_area_ids = list(app_user.areas.values_list("id", flat=True))
    elif hasattr(app_user, "area_id") and app_user.area_id:
        user_area_ids = [app_user.area_id]
    else:
        user_area_ids = []

    # Shortcut: falls keine Area bekannt, nichts ändern
    if not user_area_ids:
        return

    # Filter-Querysets nur auf relevante Gebiete einschränken
    orgs_in_area = Organization.objects.filter(area__in=user_area_ids)
    
    if organization is None and organization_all_tags is None and organization_type is None:
        # Standard: alles im Gebiet auswählen und für all_tags aktivieren
        # Bestehende Organisationen außerhalb der Areas beibehalten
        current_orgs_outside_area = app_user.organization.exclude(area__in=user_area_ids)
        current_all_tags_outside_area = app_user.organization_all_tags.exclude(area__in=user_area_ids)
        
        # Neue Auswahl: Organisationen im Gebiet + bestehende außerhalb
        new_organizations = list(orgs_in_area) + list(current_orgs_outside_area)
        new_all_tags = list(orgs_in_area) + list(current_all_tags_outside_area)
        
        app_user.organization.set(new_organizations)
        app_user.organization_all_tags.set(new_all_tags)
        
        # Deselected nur für Organisationen im Gebiet setzen (leer, da alle ausgewählt)
        app_user.deselected_organization.set(app_user.deselected_organization.exclude(area__in=user_area_ids))
        app_user.deselected_organization_all_tags.set(app_user.deselected_organization_all_tags.exclude(area__in=user_area_ids))
    else:
        if organization_type and organization_type != "all":
            # Bestehende Organisationen des gleichen Typs aber außerhalb der Areas beibehalten
            existing_orgs_same_type_outside = list(
                app_user.organization.filter(type=organization_type)
                .exclude(area__in=user_area_ids)
                .values_list("id", flat=True)
            )
            existing_all_tags_same_type_outside = list(
                app_user.organization_all_tags.filter(type=organization_type)
                .exclude(area__in=user_area_ids)
                .values_list("id", flat=True)
            )
            
            # Bestehende Organisationen anderen Typs komplett beibehalten
            existing_orgs_other_types = list(
                app_user.organization.exclude(type=organization_type).values_list("id", flat=True)
            )
            existing_all_tags_other_types = list(
                app_user.organization_all_tags.exclude(type=organization_type).values_list("id", flat=True)
            )

            # Nur IDs im Gebiet aus den übergebenen Listen verwenden
            organization_in_area = [org_id for org_id in (organization or []) 
                                  if org_id in orgs_in_area.values_list('id', flat=True)]
            organization_all_tags_in_area = [org_id for org_id in (organization_all_tags or []) 
                                           if org_id in orgs_in_area.values_list('id', flat=True)]

            # Finale Listen zusammenstellen
            final_organizations = (existing_orgs_other_types + 
                                 existing_orgs_same_type_outside + 
                                 organization_in_area)
            final_all_tags = (existing_all_tags_other_types + 
                            existing_all_tags_same_type_outside + 
                            organization_all_tags_in_area)

            # Sets aktualisieren
            app_user.organization.set(Organization.objects.filter(id__in=final_organizations))
            app_user.organization_all_tags.set(Organization.objects.filter(id__in=final_all_tags))

            # Deselected nur für Organisationen im Gebiet und vom richtigen Typ setzen
            orgs_in_area_and_type = orgs_in_area.filter(type=organization_type)
            
            deselected_orgs_in_area = orgs_in_area_and_type.exclude(id__in=organization_in_area)
            deselected_all_tags_in_area = orgs_in_area_and_type.exclude(id__in=organization_all_tags_in_area)
            
            # Bestehende deselected außerhalb der Areas beibehalten
            existing_deselected_outside = app_user.deselected_organization.exclude(area__in=user_area_ids)
            existing_deselected_all_tags_outside = app_user.deselected_organization_all_tags.exclude(area__in=user_area_ids)
            
            final_deselected = list(existing_deselected_outside) + list(deselected_orgs_in_area)
            final_deselected_all_tags = list(existing_deselected_all_tags_outside) + list(deselected_all_tags_in_area)
            
            app_user.deselected_organization.set(final_deselected)
            app_user.deselected_organization_all_tags.set(final_deselected_all_tags)
        else:
            # organization_type == "all" oder None
            # Bestehende Organisationen außerhalb der Areas beibehalten
            existing_orgs_outside = list(app_user.organization.exclude(area__in=user_area_ids))
            existing_all_tags_outside = list(app_user.organization_all_tags.exclude(area__in=user_area_ids))
            
            # Nur IDs im Gebiet aus den übergebenen Listen verwenden
            organization_in_area = [org_id for org_id in (organization or []) 
                                  if org_id in orgs_in_area.values_list('id', flat=True)]
            organization_all_tags_in_area = [org_id for org_id in (organization_all_tags or []) 
                                           if org_id in orgs_in_area.values_list('id', flat=True)]
            
            selected_orgs_in_area = orgs_in_area.filter(id__in=organization_in_area)
            selected_all_tags_in_area = orgs_in_area.filter(id__in=organization_all_tags_in_area)
            
            # Finale Listen
            final_organizations = list(existing_orgs_outside) + list(selected_orgs_in_area)
            final_all_tags = list(existing_all_tags_outside) + list(selected_all_tags_in_area)
            
            app_user.organization.set(final_organizations)
            app_user.organization_all_tags.set(final_all_tags)
            
            # Deselected nur für Organisationen im Gebiet setzen
            deselected_orgs_in_area = orgs_in_area.exclude(id__in=organization_in_area)
            deselected_all_tags_in_area = orgs_in_area.exclude(id__in=organization_all_tags_in_area)
            
            # Bestehende deselected außerhalb der Areas beibehalten
            existing_deselected_outside = app_user.deselected_organization.exclude(area__in=user_area_ids)
            existing_deselected_all_tags_outside = app_user.deselected_organization_all_tags.exclude(area__in=user_area_ids)
            
            final_deselected = list(existing_deselected_outside) + list(deselected_orgs_in_area)
            final_deselected_all_tags = list(existing_deselected_all_tags_outside) + list(deselected_all_tags_in_area)
            
            app_user.deselected_organization.set(final_deselected)
            app_user.deselected_organization_all_tags.set(final_deselected_all_tags)

    app_user.save()



class UserOrganizationsSerializer(serializers.ModelSerializer):

    class Meta:
        model = AppUser
        fields = (
            "organization",
        )


class AppUserOrganizationsViewSet(AppUserBaseViewSet):

    @swagger_auto_schema(
        operation_description="Set selected organizations",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
            choices_parameter(
                "organization_type",
                list(ORGANIZATION_TYPE_CHOICES) + [("all", "All organization types")],
                "The type of an organization",
            ),
        ],
        responses={204: "User Settings saved", 400: "Device ID is missing"},
        request_body=UserOrganizationsSerializer,
    )
    def create(self, request, *args, **kwargs):
        """

        Args:
            request (request): request object
            *args (args): additional args
            **kwargs (kwargs): additional kwargs

        Returns:
            Response
        """
        app_user, error_response = self.get_user()
        if error_response:
            return error_response

        data = self.request.data

        _organization_type = request.query_params.get("organization_type")
        _organization = []
        _organization_all_tags = data.get("organization", [])


        save_organizations(
            app_user, _organization, _organization_all_tags, _organization_type
        )

        return Response(status=status.HTTP_204_NO_CONTENT)


class UserLocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppUser
        fields = (
            "location_latitude",
            "location_longitude",
            "area",
        )


class AppUserLocationViewSet(AppUserBaseViewSet):

    @swagger_auto_schema(
        operation_description="Get user location",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        responses={200: UserLocationSerializer(), 400: "Device ID does not exist"},
    )
    def list(self, request, *args, **kwargs):
        """Get user location from db if exists.

        Args:
            request (request): request object
            *args (args): additional args
            **kwargs (kwargs): additional kwargs

        Returns:
            Response
        """
        app_user, error_response = self.get_user(create=True)
        if error_response:
            return error_response

            # get user tags to see if some already exist or if the list is empty
        user_tags = app_user.tags.values()
        # if the user has no tags assigned then assign all tags
        if len(user_tags) == 0:
            # get all tags from db
            all_tags = Tag.objects.all().values()
            # add one tag after another to the user
            for tag in all_tags:
                app_user.tags.add(tag["id"])

        # get list of all organisations that the user requested
        user_organisations = app_user.organization.values()
        # if there is none the initial values need to be set
        if len(user_organisations) == 0:
            logger.error("user has no organisations")
            # get all organisations that are active from db
            all_organisations = Organization.objects.filter(active=True).values()
            # add all ids the the users list of organisations
            for org in all_organisations:
                app_user.organization_all_tags.add(org["id"])

        app_user.save()

        serializer = UserLocationSerializer(app_user)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Set user location",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={200: "User Settings saved", 400: "Device ID is missing"},
        request_body=UserLocationSerializer,
    )
    def create(self, request, *args, **kwargs):
        """Save user location to db.

        Args:
            request (request): request object
            *args (args): additional args
            **kwargs (kwargs): additional kwargs

        Returns:
            Response
        """
        app_user, error_response = self.get_user()
        if error_response:
            logger.error(error_response)
            return error_response

        data = self.request.data

        location_latitude = data.get("location_latitude", None)
        location_longitude = data.get("location_longitude", None)

        if isinstance(location_latitude, str):
            location_latitude = float(location_latitude)
        if isinstance(location_longitude, str):
            location_longitude = float(location_longitude)

        app_user.location_latitude = location_latitude
        app_user.location_longitude = location_longitude
        app_user.save()

        area_id = data.get("area", None)
        logger.error("creating app user")
        logger.error(area_id)
        if isinstance(area_id, int) and area_id > 0:
            app_user.area_id = area_id
            app_user.area.id = area_id

        # write out appuser id
        # logger.error (app_user.__dict__["id"])

        # get user tags to see if some already exist or if the list is empty
        user_tags = app_user.tags.values()
        # if the user has no tags assigned then assign all tags
        logger.error("user tags")
        logger.error(len(user_tags))
        if len(user_tags) == 0:
            # get all tags from db
            all_tags = Tag.objects.all().values()
            # add one tag after another to the user
            for tag in all_tags:
                app_user.tags.add(tag["id"])

        # get list of all organisations that the user requested
        user_organisations = app_user.organization.values()
        # if there is none the initial values need to be set
        logger.error("user organisations")
        logger.error(len(user_organisations))

        if len(user_organisations) == 0:
            all_organisations = Organization.objects.filter(active=True).values()
            
            # add all ids the the users list of organisations
            for org in all_organisations:
                try:
                    app_user.organization_all_tags.add(org["id"])
                    logger.error("adding organisation")
                    logger.error(org["id"])
                except Exception as e:
                    logger.error(e)
            
            #save all organisations to the user with all
            logger.error("user organisations")
            logger.error(app_user.organization.values())

            app_user.save()
            app_user.organization_all_tags.set(
                Organization.objects.filter(id__in=all_organisations)
            )
            app_user.save()



        return Response(status=status.HTTP_200_OK)


PUSH_SETTINGS_FIELDS = [
    "firebase_token",
    "push_news",
    "push_collective",
    "push_official",
]


class UserPushSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppUser
        fields = PUSH_SETTINGS_FIELDS


class AppUserPushViewSet(AppUserBaseViewSet):

    @swagger_auto_schema(
        operation_description="Get user push settings",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True)
        ],
        responses={200: UserPushSerializer(), 400: "Device ID does not exist"},
    )
    def list(self, request, *args, **kwargs):
        """Return push settings of user if exists in db.

        Args:
            request (request): request object
            *args (args): additional args
            **kwargs (kwargs): additional kwargs

        Returns:
            Response
        """
        app_user, error_response = self.get_user(create=False)
        if error_response:
            return error_response

        serializer = UserPushSerializer(app_user)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Set user push settings",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={200: "User Settings saved", 400: "Device ID is missing"},
        request_body=UserPushSerializer,
    )
    def create(self, request, *args, **kwargs):
        """Save user push settings to db.

        Args:
            request (request): request object
            *args (args): additional args
            **kwargs (kwargs): additional kwargs

        Returns:
            Response
        """
        app_user, error_response = self.get_user()
        if error_response:
            return error_response

        data = self.request.data

        for field in PUSH_SETTINGS_FIELDS:
            setattr(app_user, field, data.get(field, None))
        app_user.save()

        firebase_token = data.get("firebase_token", None)
        if firebase_token:
            appusers_with_firebase_token = AppUser.objects.filter(
                firebase_token=firebase_token
            )
            if len(appusers_with_firebase_token) > 1:
                logger.debug("Duplicates for firebase id {}".format(firebase_token))
                # there are duplicates !
                # delete the dupes
                duplicates = appusers_with_firebase_token.exclude(app_user)
                logger.debug(
                    "Deleting devices: {}".format(
                        [device.device_id for device in duplicates]
                    )
                )
                duplicates.delete()

        return Response(status.HTTP_200_OK)


class OrganizationChoiceSerializer(serializers.ModelSerializer):
    type = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    push = serializers.SerializerMethodField()
    all_tags = serializers.SerializerMethodField()
    by_tag = serializers.SerializerMethodField()
    deselected = serializers.SerializerMethodField()

    class Meta:
        model = AppUser
        fields = (
            "type",
            "name",
            "push",
            "all_tags",
            "by_tag",
            "deselected",
        )

    @swagger_serializer_method(serializers.BooleanField)
    def get_push(self, instance):
        return getattr(instance, "push_{}".format(self.context["type"][0]))

    @swagger_serializer_method(serializers.CharField)
    def get_type(self, instance):
        return self.context["type"][0]

    @swagger_serializer_method(serializers.CharField)
    def get_name(self, instance):
        return self.context["type"][1]

    @swagger_serializer_method(serializers.IntegerField)
    def get_all_tags(self, instance):
        # Get the AppUser instance from the instance object
        app_user = instance
        # Get all user_areas
        user_areas = app_user.areas.all()
        user_areas_ids = [user_area.id for user_area in user_areas]

        # Filter: im User-Gebiet ODER mit auto_detect_area_from_content-Quellen
        return len(
            instance.organization_all_tags.filter(type=self.context["type"][0])
            .filter(
                Q(area__in=user_areas_ids) |
                Q(source__auto_detect_area_from_content=True)
            )
            .distinct()
            .values()
        )

    @swagger_serializer_method(serializers.IntegerField)
    def get_by_tag(self, instance):
        # Get the AppUser instance from the dictionary
        app_user = instance
        # Get all user_areas
        user_areas = app_user.areas.all()
        user_areas_ids = [user_area.id for user_area in user_areas]
        # Filter: im User-Gebiet ODER mit auto_detect_area_from_content-Quellen
        all_organisations_in_user_area = (
            instance.organization.filter(type=self.context["type"][0])
            .filter(
                Q(area__in=user_areas_ids) |
                Q(source__auto_detect_area_from_content=True)
            )
            .distinct()
            .values()
        )

        return len(all_organisations_in_user_area)

    @swagger_serializer_method(serializers.IntegerField)
    def get_deselected(self, instance):
        # convert the instance which is an instance of the app user into dictionary
        app_user = instance
        # get the area id so it can be used for filtering the results
        #get all user_areas
        user_areas = app_user.areas.all()
        user_areas_ids = [user_area.id for user_area in user_areas]

        selected = instance.organization.filter(type=self.context["type"][0]).filter(
            active=True
        )
        selected_all_tags = instance.organization_all_tags.filter(
            type=self.context["type"][0]
        ).filter(active=True)
        _selected = set(list(set(selected)) + list(set(selected_all_tags)))
        available = (
            Organization.objects.filter(type=self.context["type"][0])
            .filter(active=True)
            .filter(
                Q(area__in=user_areas_ids) |
                Q(source__auto_detect_area_from_content=True)
            )
            .distinct()
        )
        _deselected = [org for org in available if org not in _selected and org.active]
        return len(_deselected)


class SummarySerializer(serializers.ModelSerializer):

    overview = serializers.SerializerMethodField()

    class Meta:
        model = AppUser
        fields = ("overview",)

    @swagger_serializer_method(OrganizationChoiceSerializer(many=True))
    def get_overview(self, instance):
        overview = []
        for choice_type in ORGANIZATION_TYPE_CHOICES:
            _choice_serializer = OrganizationChoiceSerializer(
                instance,
                context={
                    "type": choice_type,
                },
            )
            overview.append(_choice_serializer.data)

        return overview


class SummaryViewSet(AppUserBaseViewSet):
    http_method_names = ["get"]

    @swagger_auto_schema(
        operation_description="Get a summary of selected organizations and tags",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={200: SummarySerializer, 400: "Device ID is missing"},
    )
    def list(self, request, *args, **kwargs):
        """Overview of number of selected organizations and tags.

        Args:
            request (request): request object
            *args (args): additional args
            **kwargs (kwargs): additional kwargs

        Returns:
            Response
        """
        app_user, error_response = self.get_user(create=False)
        if error_response:
            if error_response.data["detail"] == "Device ID is missing":
                return error_response
            app_user, error_response = self.get_user()
            #save_organizations(app_user)

        serializer = SummarySerializer(app_user)
        return Response(serializer.data)


class AppUserOrganizationsViewSetSingle(AppUserBaseViewSet):
    """
    Entfernt eine Organization aus dem User.organization ManyToManyField, ohne die Organisation selbst zu löschen.
    """

    @swagger_auto_schema(
        operation_description="Removes an organization from the user's organization list",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "organization_id": openapi.Schema(type=openapi.TYPE_INTEGER)
            },
            required=["organization_id"],
        ),
        responses={204: "removed organization", 400: "Missing or invalid parameters"},
    )
    def remove_organization(self, request):
        logger.debug("=== REMOVE ORGANIZATION START ===")
        
        app_user, error_response = self.get_user()
        if error_response:
            logger.error(f"Failed to get user: {error_response}")
            return error_response
        
        logger.debug(f"App user found: {app_user.id}, device_id: {app_user.device_id}")

        org_id = request.data.get("organization_id")
        logger.debug(f"Organization ID from request: {org_id} (type: {type(org_id)})")
        
        if not org_id:
            logger.error("No organization_id provided in request")
            return bad_request("organization_id muss angegeben werden.")

        try:
            org = Organization.objects.get(id=org_id)
            logger.debug(f"Organization found: {org.id}, name: {getattr(org, 'name', 'N/A')}")
        except Organization.DoesNotExist:
            logger.error(f"Organization with id {org_id} does not exist")
            return bad_request("Organization nicht gefunden.")
        except Exception as e:
            logger.error(f"Unexpected error when fetching organization: {e}")
            return bad_request("Fehler beim Laden der Organisation.")

        # Log current state before removal
        logger.debug(f"Current organization_all_tags count: {app_user.organization_all_tags.count()}")
        logger.debug(f"Current organization count: {app_user.organization.count()}")
        logger.debug(f"Is org in organization_all_tags? {app_user.organization_all_tags.filter(id=org.id).exists()}")
        logger.debug(f"Is org in organization? {app_user.organization.filter(id=org.id).exists()}")

        # Remove from organization_all_tags
        try:
            app_user.organization_all_tags.remove(org)
            logger.debug(f"Successfully removed org {org.id} from organization_all_tags")
        except Exception as e:
            logger.error(f"Failed to remove from organization_all_tags: {e}")
            logger.error(f"Exception type: {type(e)}")

        # Remove from organization
        try:
            app_user.organization.remove(org)
            logger.debug(f"Successfully removed org {org.id} from organization")
        except Exception as e:
            logger.error(f"Failed to remove from organization: {e}")
            logger.error(f"Exception type: {type(e)}")

        # Add to deselected lists (Fix #2)
        try:
            app_user.deselected_organization.add(org)
            logger.debug(f"Added org {org.id} to deselected_organization")
        except Exception as e:
            logger.error(f"Failed to add to deselected_organization: {e}")

        try:
            app_user.deselected_organization_all_tags.add(org)
            logger.debug(f"Added org {org.id} to deselected_organization_all_tags")
        except Exception as e:
            logger.error(f"Failed to add to deselected_organization_all_tags: {e}")

        # Log state after removal
        logger.debug(f"After removal - organization_all_tags count: {app_user.organization_all_tags.count()}")
        logger.debug(f"After removal - organization count: {app_user.organization.count()}")
        logger.debug(f"After removal - Is org in organization_all_tags? {app_user.organization_all_tags.filter(id=org.id).exists()}")
        logger.debug(f"After removal - Is org in organization? {app_user.organization.filter(id=org.id).exists()}")
        logger.debug(f"After removal - Is org in deselected_organization? {app_user.deselected_organization.filter(id=org.id).exists()}")
        logger.debug(f"After removal - Is org in deselected_organization_all_tags? {app_user.deselected_organization_all_tags.filter(id=org.id).exists()}")

        try:
            app_user.save()
            logger.debug("App user saved successfully")
        except Exception as e:
            logger.error(f"Failed to save app user: {e}")
            return bad_request("Fehler beim Speichern.")

        logger.debug("=== REMOVE ORGANIZATION END ===")
        return Response(status=status.HTTP_204_NO_CONTENT)


    @swagger_auto_schema(
        operation_description="Adds an organization to the user's organization list",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "organization_id": openapi.Schema(type=openapi.TYPE_INTEGER)
            },
            required=["organization_id"],
        ),
        responses={204: "added organization", 400: "Missing or invalid parameters"},
    )
    def add_organization(self, request):
        logger.debug("=== ADD ORGANIZATION START ===")
        
        app_user, error_response = self.get_user()
        if error_response:
            logger.error(f"Failed to get user: {error_response}")
            return error_response
        
        logger.debug(f"App user found: {app_user.id}, device_id: {app_user.device_id}")

        org_id = request.data.get("organization_id")
        logger.debug(f"Organization ID from request: {org_id} (type: {type(org_id)})")
        
        if not org_id:
            logger.error("No organization_id provided in request")
            return bad_request("organization_id muss angegeben werden.")

        try:
            org = Organization.objects.get(id=org_id)
            logger.debug(f"Organization found: {org.id}, name: {getattr(org, 'name', 'N/A')}")
            logger.debug(f"Organization type: {getattr(org, 'type', 'N/A')}")
            logger.debug(f"Organization active: {getattr(org, 'active', 'N/A')}")
            logger.debug(f"Organization area: {getattr(org, 'area', 'N/A')}")
        except Organization.DoesNotExist:
            logger.error(f"Organization with id {org_id} does not exist")
            return bad_request("Organization nicht gefunden.")
        except Exception as e:
            logger.error(f"Unexpected error when fetching organization: {e}")
            return bad_request("Fehler beim Laden der Organisation.")

        # Log current state before addition
        logger.debug(f"Current organization_all_tags count: {app_user.organization_all_tags.count()}")
        logger.debug(f"Current organization count: {app_user.organization.count()}")
        logger.debug(f"Is org already in organization_all_tags? {app_user.organization_all_tags.filter(id=org.id).exists()}")
        logger.debug(f"Is org already in organization? {app_user.organization.filter(id=org.id).exists()}")

        # Add to organization_all_tags
        try:
            app_user.organization_all_tags.add(org)
            logger.debug(f"Successfully added org {org.id} to organization_all_tags")
        except Exception as e:
            logger.error(f"Failed to add to organization_all_tags: {e}")
            logger.error(f"Exception type: {type(e)}")
            logger.error(f"Exception args: {e.args}")

        # Add to organization
        try:
            app_user.organization.add(org)
            logger.debug(f"Successfully added org {org.id} to organization")
        except Exception as e:
            logger.error(f"Failed to add to organization: {e}")
            logger.error(f"Exception type: {type(e)}")
            logger.error(f"Exception args: {e.args}")

        # Remove from deselected lists (Fix #1)
        try:
            app_user.deselected_organization.remove(org)
            logger.debug(f"Removed org {org.id} from deselected_organization")
        except Exception as e:
            logger.debug(f"Org {org.id} was not in deselected_organization: {e}")

        try:
            app_user.deselected_organization_all_tags.remove(org)
            logger.debug(f"Removed org {org.id} from deselected_organization_all_tags")
        except Exception as e:
            logger.debug(f"Org {org.id} was not in deselected_organization_all_tags: {e}")

        # Log state after addition
        logger.debug(f"After addition - organization_all_tags count: {app_user.organization_all_tags.count()}")
        logger.debug(f"After addition - organization count: {app_user.organization.count()}")
        logger.debug(f"After addition - Is org in organization_all_tags? {app_user.organization_all_tags.filter(id=org.id).exists()}")
        logger.debug(f"After addition - Is org in organization? {app_user.organization.filter(id=org.id).exists()}")
        logger.debug(f"After addition - Is org in deselected_organization? {app_user.deselected_organization.filter(id=org.id).exists()}")
        logger.debug(f"After addition - Is org in deselected_organization_all_tags? {app_user.deselected_organization_all_tags.filter(id=org.id).exists()}")

        # Log all current organizations for debugging
        try:
            all_org_ids = list(app_user.organization.values_list('id', flat=True))
            all_org_all_tags_ids = list(app_user.organization_all_tags.values_list('id', flat=True))
            logger.debug(f"All organization IDs: {all_org_ids}")
            logger.debug(f"All organization_all_tags IDs: {all_org_all_tags_ids}")
        except Exception as e:
            logger.error(f"Failed to log current organizations: {e}")

        try:
            app_user.save()
            logger.debug("App user saved successfully")
        except Exception as e:
            logger.error(f"Failed to save app user: {e}")
            return bad_request("Fehler beim Speichern.")

        logger.debug("=== ADD ORGANIZATION END ===")
        return Response(status=status.HTTP_204_NO_CONTENT)