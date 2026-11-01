from django.db.models import Q
from rest_framework import viewsets, serializers, filters
from rest_framework.renderers import BaseRenderer
from drf_yasg.utils import swagger_auto_schema, swagger_serializer_method
from drf_yasg import openapi
import django_filters as df
from rest_framework.response import Response
from django.http import FileResponse, Http404
import os
import mimetypes


class BinaryFileRenderer(BaseRenderer):
    media_type = 'application/octet-stream'
    format = 'binary'
    charset = None
    render_style = 'binary'

    def render(self, data, media_type=None, renderer_context=None):
        return data
from .util import bad_request, choices_parameter, header_string_parameter
from content.models import Organization, AppUser
from content.choices import ORGANIZATION_TYPE_CHOICES, V1_ORGANIZATION_TYPE_CHOICES
from django.conf import settings
import logging

logger = logging.getLogger("molonews")

class OrganizationFilter(df.FilterSet):

    type = df.CharFilter(method='filter_type')

    class Meta:
        model = Organization
        fields = ['type']

    def filter_type(self, queryset, name, value):
        if value == "all":
            return queryset  
        if value in dict(ORGANIZATION_TYPE_CHOICES):
            return queryset.filter(type=value)
        return queryset.none()  
    

def get_appuser(device_id):
    """Try to get an AppUser by device id

    Args:
        device_id (str): device id string

    Returns:
        AppUser or None
    """
    try:
        return AppUser.objects.get(device_id=device_id)
    except AppUser.DoesNotExist:
        return None

class OrganizationActiveFilter(filters.BaseFilterBackend):

    def filter_queryset(self, request, queryset, view):
        """Filter queryset to only include active organizations.

        Args:
            request (Request): current request
            queryset (QuerySet): unfiltered Queryset
            view (View): current view

        Returns:
            filtered QuerySet
        """
        return queryset.filter(active=True)

class OrganizationSerializerPartial(serializers.ModelSerializer):

    is_selected = serializers.SerializerMethodField()
    homepage = serializers.CharField()
    image = serializers.CharField()

    @swagger_serializer_method(serializers.BooleanField)
    def get_is_selected(self, instance):

        org_data = self.context.get('org_data', None)
        device_id = self.context.get('device_id', None)
        
        if not org_data:
            return None
        
        try:
            appuser = AppUser.objects.get(device_id=device_id)
            if instance not in appuser.organization.all():
                appuser.organization.add(instance)
                appuser.save()
        except AppUser.DoesNotExist:
            pass

        # Vereinfachte is_selected Logik:
        # True : Organisation ist in selected_all_tags ODER in selected
        # None: Organisation ist weder in selected_all_tags noch in selected
       
        # Prüfe ob Organisation in selected_all_tags oder selected ist
        selected_all_tags = instance.id in org_data['selected_all_tags']
        
        if selected_all_tags:
            return True
    
        return False
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)      
        representation['image'] = f"{settings.SERVER_URL}/media/{instance.image}"

        if not instance.homepage:
            representation['homepage'] = "https://"  # Oder ein Hinweis-Link
        # only for the list but not for the retrieve method
        #if 'list' in self.context['view'].action:
        representation['description'] = ""
        representation['address'] = ""
        #else:
        #    pass
                

        return representation
    
    class Meta:
        model = Organization
        fields = (
            'id',
            'title',
            'name',
            'description',
            'address',
            'type',
            'image',
            'homepage',
            'is_selected',
        )

    
class OrganizationSerializer(serializers.ModelSerializer):

    is_selected = serializers.SerializerMethodField()
    homepage = serializers.CharField()
    image = serializers.CharField()

    @swagger_serializer_method(serializers.BooleanField)
    def get_is_selected(self, instance):
        # is_selected:
        # True : (selected_all_tags == True)
        # False: (is_selected = True)
        # None: (selected_all_tags == False AND is_selected == False)

        is_selected = None
        org_data = self.context.get('org_data', None)
        device_id = self.context.get('device_id', None)
        if not org_data:
            return None
        selected = instance.id in org_data['selected']
        deselected = instance.id in org_data['deselected']
        if selected is not None and selected and not deselected:
            is_selected = False
        selected_all_tags = instance.id in org_data['selected_all_tags']
        deselected_all_tags = instance.id in org_data['deselected_all_tags']
        if selected_all_tags is not None and selected_all_tags and not deselected_all_tags:
            is_selected = True

        # default to tag selection
        if not selected_all_tags and not selected and not deselected and not deselected_all_tags:
            is_selected = False
            try:
                appuser = AppUser.objects.get(device_id=device_id)
                appuser.organization.add(instance)
                appuser.save()
            except AppUser.DoesNotExist:
                pass

        return is_selected
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)      
        representation['image'] = f"{settings.SERVER_URL}/media/{instance.image}"

        if not instance.homepage:
            representation['homepage'] = "https://"  # Oder ein Hinweis-Link
        # only for the list but not for the retrieve method
        #if 'list' in self.context['view'].action:
        #    representation['description'] = ""
        #    representation['address'] = ""
        #else:
        #    pass
                
        return representation

    class Meta:
        model = Organization
        fields = (
            'id',
            'title',
            'name',
            'description',
            'address',
            'type',
            'image',
            'homepage',
            'is_selected',
        )


class OrganizationViewSet(viewsets.ReadOnlyModelViewSet):

    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    serializer_class_partial = OrganizationSerializerPartial
    filter_backends = [
        df.rest_framework.DjangoFilterBackend,
        OrganizationActiveFilter,
    ]
    filterset_class = OrganizationFilter

    def _get_org_data(self, device_id):

        try:
            app_user = AppUser.objects.get(device_id=device_id)
            org_data = {
                "selected": [_org.id for _org in app_user.organization.all()],
                "deselected": [_org.id for _org in app_user.deselected_organization.all()],
                "selected_all_tags": [_org.id for _org in app_user.organization_all_tags.all()],
                "deselected_all_tags": [_org.id for _org in app_user.deselected_organization_all_tags.all()],
            }
        except AppUser.DoesNotExist:
            org_data = {
                "selected": [],
                "deselected": [],
                "selected_all_tags": [],
                "deselected_all_tags": [],
            }
        return org_data

    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter('X-Device-ID', 'Device ID', required=True),
            choices_parameter('type', list(ORGANIZATION_TYPE_CHOICES) + [("all", "All types")])
            
        ]
    )
    def list(self, *args, **kwargs):
        device_id = self.request.headers.get('X-Device-ID', None)
        if device_id is None:
            return bad_request("Device ID is missing")

        org_data = self._get_org_data(device_id)

        queryset = self.filter_queryset(self.get_queryset()).order_by('name')

        app_user = get_appuser(device_id)
        #get all user_areas
        user_areas = app_user.areas.all()
        user_areas_ids = [user_area.id for user_area in user_areas]

        # filter: Organisationen im User-Gebiet ODER mit auto_detect_area_from_content-Quellen
        queryset = queryset.filter(
            Q(area__in=user_areas_ids) |
            Q(source__auto_detect_area_from_content=True)
        ).distinct()

        #area_id = user_area.__dict__['id']
    
        #queryset = queryset.filter(area = area_id)

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = OrganizationSerializerPartial(page, many=True, context={
                'orgs_data': org_data,
            })
            return self.get_paginated_response(serializer.data)

        serializer = OrganizationSerializerPartial(queryset, many=True, context={
            'org_data': org_data, 'device_id': device_id,
        })
        return Response(serializer.data)
    
    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter('X-Device-ID', 'Device ID', required=False)
        ]
    )
    def retrieve(self, request, *args, **kwargs):
        # get the organization data by provided id in url string

        # get device id
        device_id = request.headers.get('X-Device-ID', None)
        if device_id is None:
            device_id = "None"
        org_id = kwargs.get('pk', None)
        if org_id is None:
            return bad_request("Organization ID is missing")
        
        instance = self.get_object()
        org_data = self._get_org_data(device_id)

        serializer = self.get_serializer(instance, context={
            'org_data': org_data, 'device_id': device_id,
        })
        return Response(serializer.data)

class OrganizationSerializer_V1(OrganizationSerializer):

    # TODO remove temporary workaround, see Ticket 515
    address = serializers.SerializerMethodField()

    @swagger_serializer_method(serializers.CharField)
    def get_address(self, instance):
        if instance.address:
            return instance.address
        if instance.address is None:
            return "http://"


    # TODO remove temporary workaround, see Ticket 515

    type = serializers.SerializerMethodField()

    def get_type(self, instance):
        if instance.type == 'official':
            return 'collective'
        return instance.type


class OrganizationViewSet_V1(OrganizationViewSet):

    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer_V1
    filter_backends = None

    def filter_queryset(self, queryset):
        org_type = self.request.query_params.get('type', None)
        if org_type == 'collectives':
            queryset = queryset.exclude(type='news')
        if org_type == 'news':
            queryset = queryset.filter(type='news')
        # TODO remove workaround
        if org_type == 'official':
            queryset = queryset.filter(type='official')
        return queryset

    @swagger_auto_schema(
        manual_parameters=[
            header_string_parameter('X-Device-ID', 'Device ID', required=True),
            choices_parameter('type', V1_ORGANIZATION_TYPE_CHOICES),
        ]
    )
    def list(self, *args, **kwargs):
        device_id = self.request.headers.get('X-Device-ID', None)
        if device_id is None:
            return bad_request("Device ID is missing")

        org_data = self._get_org_data(device_id)

        queryset = self.filter_queryset(self.get_queryset()).order_by('name')

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = OrganizationSerializer_V1(page, many=True, context={
                'orgs_data': org_data,
            })
            return self.get_paginated_response(serializer.data)

        serializer = OrganizationSerializer_V1(queryset, many=True, context={
            'org_data': org_data,
        })
        return Response(serializer.data)


class OrganizationImageViewSet(viewsets.ViewSet):
    renderer_classes = [BinaryFileRenderer]
    """
    ViewSet to serve organization images directly for use in HTML img tags.
    Example: GET /organization/195/image returns the image file directly.
    """

    @swagger_auto_schema(
        operation_description="Retrieve organization image file directly",
        produces=['application/octet-stream', 'image/png', 'image/jpeg'],
        responses={
            200: openapi.Response(
                description="Organization image file",
                schema=openapi.Schema(type=openapi.TYPE_FILE)
            ),
            404: "Organization not found or no image available"
        }
    )
    def retrieve_image(self, request, pk=None):
        """
        Serve the organization image directly as a file response.

        Args:
            request: HTTP request
            pk: Organization primary key

        Returns:
            FileResponse with the image file
        """
        try:
            organization = Organization.objects.get(pk=pk)
        except Organization.DoesNotExist:
            raise Http404("Organization not found")

        if not organization.image:
            raise Http404("No image available for this organization")

        # Construct the file path
        # The image field contains just the filename (e.g., "abc123.jpg")
        image_filename = str(organization.image)
        file_path = os.path.join(settings.MEDIA_ROOT, image_filename)

        # Check if file exists
        if not os.path.exists(file_path):
            logger.error(f"Image file not found at path: {file_path}")
            raise Http404("Image file not found")

        # Determine content type
        content_type, _ = mimetypes.guess_type(file_path)
        if content_type is None:
            content_type = 'application/octet-stream'

        # Open and return the file
        try:
            file_handle = open(file_path, 'rb')
            response = FileResponse(file_handle, content_type=content_type)
            response['Content-Disposition'] = f'inline; filename="{os.path.basename(image_filename)}"'
            return response
        except Exception as e:
            logger.error(f"Error serving image file: {e}")
            raise Http404("Error serving image file")
