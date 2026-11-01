from django.apps import AppConfig


class JetConfig(AppConfig):
    """Konfiguration für das Fremdpaket ``jet`` (Admin-Oberfläche).

    ``jet`` bringt keine eigene AppConfig mit und würde sonst das projektweite
    ``DEFAULT_AUTO_FIELD`` (BigAutoField) erben. Seine Migrationen legen die
    IDs aber als ``AutoField`` an; ohne diese Festlegung meldet
    ``makemigrations --check`` eine fehlende Migration im Paket.
    """

    name = "jet"
    default_auto_field = "django.db.models.AutoField"
