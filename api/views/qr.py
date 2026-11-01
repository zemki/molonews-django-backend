from html import escape

from django.conf import settings
from django.http import HttpResponse
from rest_framework.views import APIView


class RedirectView(APIView):
    """
    Leitet QR-Code-Scans je nach Gerät in den passenden App-Store weiter
    (APP_STORE_URL / PLAY_STORE_URL), sonst auf die Website (WEBSITE_URL).
    Es wird nichts protokolliert.
    """

    def get(self, request, *args, **kwargs):
        user_agent = request.META.get("HTTP_USER_AGENT", "")

        if ("iPhone" in user_agent or "iPad" in user_agent) and settings.APP_STORE_URL:
            redirect_url = settings.APP_STORE_URL
        elif "Android" in user_agent and settings.PLAY_STORE_URL:
            redirect_url = settings.PLAY_STORE_URL
        else:
            redirect_url = settings.WEBSITE_URL

        url = escape(redirect_url, quote=True)
        html = f"""<html>
<head><meta http-equiv="refresh" content="0; url={url}"></head>
<body><a href="{url}">Hier klicken, falls die Weiterleitung nicht startet.</a></body>
</html>"""
        return HttpResponse(html)
