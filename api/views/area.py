from rest_framework import viewsets, serializers
from drf_yasg.utils import swagger_auto_schema
from django.conf import settings
from math import acos, sin, cos, radians
from rest_framework.response import Response
from rest_framework import status
from .util import bad_request, header_string_parameter, string_parameter
from content.models import Area, AppUser, User, Article, Source, Tag, Organization
from logging import getLogger
from django.utils.html import escape
from drf_yasg import openapi
from django.utils import timezone
from rest_framework.decorators import action

logger = getLogger("molonews")


def calculate_distance(lon1, lat1, lon2, lat2):
    """
    Calculate the distance between two points on the Earth's surface using the Haversine formula.
    https://stackoverflow.com/questions/69644740/filter-users-within-a-given-distance-in-django-rest-framework

    Parameters:
    lon1 (float): The longitude of the first point in degrees.
    lat1 (float): The latitude of the first point in degrees.
    lon2 (float): The longitude of the second point in degrees.
    lat2 (float): The latitude of the second point in degrees.

    Returns:
    float: The distance between the two points in kilometers.
    """

    distance_val = 0
    try:
        lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
        distance_val =  6371 * (
            acos(sin(lat1) * sin(lat2) + cos(lat1) * cos(lat2) * cos(lon1 - lon2))
        )
    except Exception:
        distance_val = 10000

    return distance_val

class AreaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Area
        fields = (
            'id',
            'name',
            'longitude',
            'latitude',
        )

class ListAreaSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    results = AreaSerializer(many=True)

class AreaViewSet(viewsets.ViewSet):

    queryset = Area.objects.all()
    # only list Areas that are visible = True
    queryset = queryset.filter(visible=True)
    serializer_class = AreaSerializer


    @swagger_auto_schema(
        operation_description='Get locations where molo is available',
        manual_parameters=[string_parameter('area', ''), string_parameter('longitude', ''), string_parameter('latitude', ''), header_string_parameter("X-Device-ID", "Device ID", required=False), ],
        
        responses={200: ListAreaSerializer(), 400: 'Device ID does not exist'},
    )
    def list(self, request, *args, **kwargs):
        
        arealist = list(self.queryset)
        data = self.request.GET.dict()

        try:
            longitude = float(data['longitude'])
            latitude = float(data['latitude'])
        except:
            longitude = 0
            latitude = 0

        try:
            area_name = data['area']
        except:
            area_name = 0

        # if lattitude and longitude are zero then take the values from the user
        if longitude == 0 or latitude == 0:
            try:
                user = AppUser.objects.get(device_id=self.request.META.get('HTTP_DEVICEID'))
                longitude = user.location_longitude
                latitude = user.location_latitude
            except:  
                # if no user is found then take the default values
                latitude = 53.0793
                longitude = 8.8017  

        calculated_distance = False

        if longitude and latitude:
            # add all distances into List
            for item in arealist:
                item.distance = calculate_distance(lon1=longitude, lat1=latitude, lon2=item.longitude, lat2=item.latitude)    

            # order the list by distance
            def sortFn(value):
                return value.distance
            arealist.sort(key=sortFn)
            # store that a calculation of the distance has happened
            # the result of this is preferred over the search field
            calculated_distance = True
            
        # if only the area name is provided

        if calculated_distance == False and isinstance(area_name, str) == True: 
            # check if its in the list
            # if its in the list only return the requested item
            # otherwise leave the list untouched and return it in complete
            arealist_temp = arealist.copy()
            arealist_temp.clear()

            for item in arealist:
                if area_name.lower() in item.name.lower():
                    arealist_temp.append(item)
            
            arealist = arealist_temp

        # if an area_name was provided, only return those areas that contain the area_name in lowercase
        if isinstance(area_name, str) == True:
            arealist_temp = arealist.copy()
            arealist_temp.clear()

            for item in arealist:
                if area_name.lower() in item.name.lower():
                    arealist_temp.append(item)
            
            arealist = arealist_temp

        # sort the arealist alphabetically
        def sortFn(value):
            return value.name
        arealist.sort(key=sortFn)

        serializer = AreaSerializer(arealist, many=True)
   
        return Response({
            'count': len(arealist),
            'results': serializer.data
        })  

    @swagger_auto_schema(
        operation_description='Create a new location where molo is available',
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'area': openapi.Schema(type=openapi.TYPE_STRING, description='Name of the area'),
                'longitude': openapi.Schema(type=openapi.TYPE_NUMBER, description='Longitude of the area'),
                'latitude': openapi.Schema(type=openapi.TYPE_NUMBER, description='Latitude of the area'),
                'zip_code': openapi.Schema(type=openapi.TYPE_STRING, description='Zip code of the area'),
            },
            required=['area', 'longitude', 'latitude', 'zip_code'],
        ),
        responses={
            201: 'Created',
            400: 'Bad Request',
            401: 'Unauthorized',
            409: 'Conflict - Area already exists',
        },
    )

    def create(self, request, *args, **kwargs):
        data = request.data

        # Replace commas with dots if longitude/latitude are provided as strings
        if isinstance(data.get('longitude'), str):
            data['longitude'] = data['longitude'].replace(',', '.')
        if isinstance(data.get('latitude'), str):
            data['latitude'] = data['latitude'].replace(',', '.')

        # Safely convert longitude/latitude to floats
        try:
            longitude = float(data['longitude'])
            latitude = float(data['latitude'])
        except (TypeError, ValueError) as e:
            logger.error("An error occurred while converting longitude/latitude to float: %s", e)
            return bad_request("Longitude or latitude format is invalid.")

        # Extract the remaining required fields
        try:
            area_name = data['area']
            zip_code = data['zip_code']
        except KeyError:
            # If any of these keys are missing in the incoming JSON
            logger.error("An error occurred: missing one of the required keys (area, zip_code).")
            return bad_request('Bad Request')

        if not isinstance(area_name, str) or not isinstance(zip_code, str):
            return bad_request('Bad Request')
        area_name = area_name.strip()
        zip_code = zip_code.strip()
        if len(area_name) > 100 or len(zip_code) > 10 or not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            return bad_request('Bad Request')
        
        #check if area already exists and if it exists then make it visible = true
        try:
            area = Area.objects.get(name=area_name)
            area.visible = True
            area.save()
            return Response({"detail": "Area already exists and is now visible."},
                            status=status.HTTP_200_OK)
        except Area.DoesNotExist:
            pass

        # Validate area_name, zip_code, and make sure float conversion didn't yield None
        # (which shouldn't happen, but good to be safe).
        if area_name is None or zip_code is None or longitude is None or latitude is None:
            logger.error("Missing required fields. One of area, longitude, latitude, or zip_code is None.")
            return bad_request('Bad Request')

        # Disallow empty strings for area_name or zip_code if that’s your requirement
        if not area_name or not zip_code:
            logger.error("An error occurred: area or zip_code is an empty string.")
            return bad_request('Bad Request')

        # At this point, zero is allowed as a valid coordinate if that’s intended.
        # We only bail if they’re missing, which we’ve already checked.

        # Check if the area already exists
        try:
            Area.objects.get(name=area_name)
            return Response({"detail": "Area already exists."}, status=status.HTTP_409_CONFLICT)
        except Area.DoesNotExist:
            pass
        except Exception as e:
            logger.error("An unexpected error occurred while checking if area exists: %s", e)
            return Response({"detail": "An error occurred while creating the area."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # If we’re here, the area does not exist; create it
        try:
            area = Area.objects.create(
                name=area_name,
                longitude=longitude,
                latitude=latitude,
                zip=zip_code
            )

            # Assign the new area to all admin (superuser) accounts
            admin_users = User.objects.filter(is_superuser=True)
            for admin_user in admin_users:
                admin_user.area.add(area)

            # Quelle des Willkommensbeitrags (optional, AREA_WELCOME_SOURCE_ID)
            source = None
            if settings.AREA_WELCOME_SOURCE_ID:
                source = Source.objects.filter(pk=settings.AREA_WELCOME_SOURCE_ID).first()

            # Build your welcome message abstract
            try:
                abstract = (
                    "<p>Du möchtest Infos aus <strong>"
                    + escape(area.name)
                    + "</strong> lesen? Dann hast du jetzt die Möglichkeit, "
                    "eine*r der Ersten zu sein und selber Infos zu veröffentlichen.</p>"
                    "<p>Teile der Community mit, was hier los ist. Poste und veröffentliche "
                    "ganz einfach Infos und Veranstaltungen direkt hier in der App und weise "
                    "andere Menschen auf diese Möglichkeit hin. So wird die molo-Community in "
                    + "<strong>" + escape(area.name) + "</strong> immer größer.</p>"
                    + (
                        f"<p>Bei Fragen oder Feedback, sende uns eine Nachricht an {settings.CONTACT_EMAIL}.</p>"
                        if settings.CONTACT_EMAIL else ""
                    )
                    + "<p>Viel Spaß beim Posten wünscht dir das molo-Team!</p>"
                )
            except Exception as e:
                logger.error("An error occurred while building the abstract: %s", e)
                abstract = ""

            # Willkommensbeitrag nur mit eingestellter Quelle (Article.source ist Pflicht)
            try:
                if source is None:
                    raise LookupError("AREA_WELCOME_SOURCE_ID not set or source missing")
                new_article = Article.objects.create(
                    title="Willkommen bei molo!",
                    abstract=abstract,
                    link="",
                    date=timezone.now(),
                    moddate=timezone.now(),
                    source=source,
                    published=True,
                    up_for_review=False,
                    image=None,
                    image_source=None,
                    foreign_id=None,
                    image_url=None,
                )
                new_article.area.add(area)
                # Tag des Willkommensbeitrags (optional, AREA_WELCOME_TAG_ID)
                welcome_tag = (
                    Tag.objects.filter(pk=settings.AREA_WELCOME_TAG_ID).first()
                    if settings.AREA_WELCOME_TAG_ID else None
                )
                if welcome_tag:
                    new_article.tags.add(welcome_tag)
                new_article.save()
            except LookupError as e:
                logger.info("No welcome article for new area: %s", e)
            except Exception as e:
                logger.error("An error occurred while creating the welcome article: %s", e)

            return Response({"detail": "Area created successfully."},
                            status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error("An error occurred while creating the area or assigning users: %s", e)
            return Response({"detail": "An error occurred while creating the area."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


    # get the closes area by longitude and latitude only as internal function
    def get_closest_area(self, longitude, latitude):
        arealist = list(self.queryset)
        for item in arealist:
            item.distance = calculate_distance(lon1=longitude, lat1=latitude, lon2=item.longitude, lat2=item.latitude)    

        # order the list by distance
        def sortFn(value):
            return value.distance
        arealist.sort(key=sortFn)
        return arealist[0]
    
    # destroy an area by id
    @swagger_auto_schema(
        operation_description='Delete a location where molo is available',
        responses={200: 'Deleted', 400: 'Bad Request'},
    )
    def destroy(self, request, *args, **kwargs):
        
        return bad_request('Not Implemented') 

    
    # get the closest area by longitude and latitude as api endpoint
    @swagger_auto_schema(
        operation_description='Get the closest location where molo is available',
        manual_parameters=[string_parameter('longitude', ''), string_parameter('latitude', '') ],
        responses={200: AreaSerializer(), 400: 'Bad Request'},
    )   
    def closest(self, request, *args, **kwargs):
        data = request.GET.dict()

        try:
            longitude = float(data['longitude'])
            latitude = float(data['latitude'])
        except:
            return bad_request('Bad Request')

        area = self.get_closest_area(longitude, latitude)
        serializer = AreaSerializer(area)
        return Response(serializer.data)
    
    @swagger_auto_schema(
        operation_description='Get locations within a specified distance from a given point',
        manual_parameters=[
            string_parameter('longitude', 'Longitude of the starting point'),
            string_parameter('latitude', 'Latitude of the starting point'),
            string_parameter('distance', 'Distance in kilometers')
        ],
        responses={200: ListAreaSerializer(), 400: 'Bad Request'},
    )
    @action(detail=False, methods=['get'], url_path='by_distance')
    def by_distance(self, request, *args, **kwargs):
        data = request.GET.dict()

        try:
            data['longitude'] = data['longitude'].replace(',', '.')
            data['latitude'] = data['latitude'].replace(',', '.')
            longitude = float(data['longitude'])
            latitude = float(data['latitude'])
            distance = float(data['distance'])
        except (KeyError, ValueError):
            return bad_request('Invalid or missing parameters')
        
        # print out area.latitude and area.longitude
        #for area in self.queryset:
        #     logger.error("Area: " + str(area.name) + " " + str(area.latitude) + " " + str(area.longitude))

        # Filter areas within the specified distance
        areas_within_distance = [
            area for area in self.queryset
            if calculate_distance(longitude,latitude,  area.longitude, area.latitude) <= distance
        ]

        # Areas nach Distanz sortieren
        for area in areas_within_distance:
            area.distance = calculate_distance(longitude, latitude, area.longitude, area.latitude)

        # Liste nach Distanz sortieren (aufsteigend - nächste zuerst)
        areas_within_distance.sort(key=lambda area: area.distance)

        serializer = AreaSerializer(areas_within_distance, many=True)
        return Response({
            'count': len(areas_within_distance),
            'results': serializer.data
        })


def get_areas_within_distance(longitude, latitude, distance_km=50):
    from content.models import Area  # ggf. anpassen
    areas = Area.objects.filter(visible=True, latitude__isnull=False, longitude__isnull=False)
    result = []
    for area in areas:
        if area.latitude is not None and area.longitude is not None:
            d = calculate_distance(longitude, latitude, float(area.longitude), float(area.latitude))
            if d <= distance_km:
                result.append(area)
    return result


class AppUserAreaRadiusViewSet(viewsets.ViewSet):
    """
    ViewSet, um Areas nach Koordinate/Umkreis zu listen oder dem AppUser zuzuweisen.
    """

    @swagger_auto_schema(
        operation_description="Gibt alle Areas zurück, die dem AppUser (über Device ID) zugeordnet sind.",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
        ],
        responses={200: AreaSerializer(many=True), 400: "Bad Request"},
    )
    @action(detail=False, methods=["get"], url_path="list_areas_by_user")
    def list_areas_by_user(self, request, *args, **kwargs):
        device_id = request.headers.get("X-Device-ID")
        if not device_id:
            return bad_request("Device ID fehlt im Header.")

        try:
            app_user = AppUser.objects.get(device_id=device_id)
        except AppUser.DoesNotExist:
            return bad_request("Unbekannter Device ID.")

        user_areas = app_user.areas.all()
        serializer = AreaSerializer(user_areas, many=True)
        return Response({
            "count": len(user_areas),
            "results": serializer.data
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Setzt für einen Nutzer die Areas, die im Umkreis der angegebenen Koordinaten liegen.",
        manual_parameters=[
            header_string_parameter("X-Device-ID", "Device ID", required=True),
            string_parameter("longitude", "User longitude"),
            string_parameter("latitude", "User latitude"),
            string_parameter("distance", "Radius in km (optional, default: 50)"),
        ],
        responses={200: "Areas gesetzt", 400: "Bad Request"},
    )

    def set_areas_by_location(self, request, *args, **kwargs):
        device_id = request.headers.get("X-Device-ID")

        # nur dann organisation setzten wenn device id nicht vorhanden ist
        # oganisationen alle auf true setzten
        # alle tags anwählen 
        # wenn user noch nicht bekannt ist

        if not device_id:
            return bad_request("Device ID fehlt im Header.")
        
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
                logger.error("Added all tags to new user with device_id: " + str(device_id))

            # Assign all active organizations to the user if they don't have any
            user_organisations = app_user.organization_all_tags.values()
            if len(user_organisations) == 0:
                all_organisations = Organization.objects.filter(active=True).values()
                for org in all_organisations:
                    app_user.organization_all_tags.add(org["id"])
                    app_user.organization.add(org["id"])
                app_user.save()
                logger.error("Added all active organizations to new user with device_id: " + str(device_id))


        data = request.data if request.data else request.query_params
        try:
            longitude = float(data.get("longitude"))
            latitude = float(data.get("latitude"))
            distance = float(data.get("distance", 50))
        except Exception:
            return bad_request("longitude, latitude oder distance fehlt oder ungültig")

        areas_in_radius = get_areas_within_distance(longitude, latitude, distance)

        # Areas nach Distanz sortieren
        for area in areas_in_radius:
            area.distance = calculate_distance(longitude, latitude, area.longitude, area.latitude)

        # Liste nach Distanz sortieren (aufsteigend - nächste zuerst)
        areas_in_radius.sort(key=lambda area: area.distance)

        # Kein Treffer im Radius → nächstgelegene Area als Fallback
        nearest_fallback = False
        if not areas_in_radius:
            all_areas = list(Area.objects.filter(visible=True, latitude__isnull=False, longitude__isnull=False))
            if all_areas:
                closest = min(all_areas, key=lambda a: calculate_distance(longitude, latitude, float(a.longitude), float(a.latitude)))
                areas_in_radius = [closest]
                nearest_fallback = True

        app_user.areas.set(areas_in_radius)
        app_user.location_latitude = latitude
        app_user.location_longitude = longitude
        app_user.save()

        return Response({
            "area_ids": [area.id for area in areas_in_radius],
            "nearest_fallback": nearest_fallback,
        }, status=status.HTTP_200_OK)