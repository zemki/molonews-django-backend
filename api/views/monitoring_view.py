import json
import os
from django.shortcuts import render
from django.conf import settings
from django.contrib.auth.decorators import user_passes_test

# Nur für Superuser: enthält interne Adressen und Fehlermeldungen der Quellen
@user_passes_test(lambda u: u.is_active and u.is_superuser, login_url="newadmin:login")
def monitoring_status_page(request):
    """
    Lädt die monitoring.json und zeigt die Ergebnisse visuell an.
    """
    json_path = os.path.join(settings.LOG_DIR, "monitoring.json")

    data = {}
    if os.path.exists(json_path):
        with open(json_path, "r") as f:
            data = json.load(f)

    return render(
        request,
        "monitoring/status_page.html",
        {
            "data": data,
        },
    )