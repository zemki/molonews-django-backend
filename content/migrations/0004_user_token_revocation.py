"""Token-Widerruf, Ablauf von Reset-Links und Organisationsrecht für Eigentümer.

* ``tokens_valid_after`` / ``token_exempt_jti``: Passwortänderungen widerrufen
  ältere API-Tokens (siehe ``api.views.util.check_token_payload``).
* ``password_reset_requested_at``: Reset-Links verfallen nach
  ``PASSWORD_RESET_TIMEOUT``. Bestehende Reset-Tokens haben keinen Zeitpunkt und
  sind damit ungültig; Nutzer fordern einfach einen neuen Link an.
* ``can_modify_organization_info``: Eigentümer einer Organisation
  (``Organization.related_user``, z. B. selbst registrierte Organisationen)
  erhalten das Recht, ihre Organisation zu bearbeiten. Es lässt sich im Admin
  pro Konto entziehen.
"""

from django.db import migrations, models


def grant_organization_owners(apps, schema_editor):
    User = apps.get_model("content", "User")
    Organization = apps.get_model("content", "Organization")
    owner_ids = (
        Organization.objects.exclude(related_user__isnull=True)
        .values_list("related_user_id", flat=True)
        .distinct()
    )
    User.objects.filter(pk__in=list(owner_ids)).update(can_modify_organization_info=True)


def clear_unhashed_reset_tokens(apps, schema_editor):
    # Ältere Tokens stehen im Klartext und ohne Zeitpunkt in der Datenbank
    User = apps.get_model("content", "User")
    User.objects.exclude(password_reset_token__isnull=True).update(password_reset_token=None)


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0003_source_import_script_approval"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="password_reset_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="token_exempt_jti",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="user",
            name="tokens_valid_after",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(grant_organization_owners, migrations.RunPython.noop),
        migrations.RunPython(clear_unhashed_reset_tokens, migrations.RunPython.noop),
    ]
