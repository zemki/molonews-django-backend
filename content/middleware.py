from django.utils.deprecation import MiddlewareMixin
from django.http import HttpResponse
from corsheaders.middleware import CorsMiddleware as BaseCorsMiddleware

class CustomCorsMiddleware(BaseCorsMiddleware):
    def check_preflight(self, request):
        response = HttpResponse()
        response["Content-Length"] = "0"
        return response

class AccessControlAllowOriginMiddleware(MiddlewareMixin):
    def process_response(self, request, response):
        origin = request.META.get('HTTP_ORIGIN')
        if origin:
            # Dynamically set the allowed origin based on the request's origin
            response["Access-Control-Allow-Origin"] = origin
        # Note: No fallback to "*" because Access-Control-Allow-Credentials: true
        # is incompatible with wildcard origins
        
        # Add other CORS headers
        response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, PATCH, DELETE"
        response["Access-Control-Allow-Headers"] = "Origin, Content-Type, X-Auth-Token, X-Requested-With, Authorization, x-csrftoken, x-device-id"
        response["Access-Control-Allow-Credentials"] = "true"

        # Handle preflight (OPTIONS) requests
        if request.method == "OPTIONS":
            response.status_code = 200
            response["Content-Length"] = "0"
            return response
        
        return response
