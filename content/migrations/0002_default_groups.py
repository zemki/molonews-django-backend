"""
Legt die beiden Benutzergruppen an, die der Code über ihre ID erkennt
(siehe User.is_editor / User.is_contributor):

    1  Editors       Redaktion: Inhalte der zugeordneten Quellen prüfen und bearbeiten,
                     dazu Rechte auf Quellen, Gebiete und Themen
    2  Contributors  Organisationen/Beitragende: eigene Artikel und Events

Die Rechte lassen sich anschließend im Admin unter "Gruppen" anpassen.
"""

from django.contrib.auth.management import create_permissions
from django.db import migrations

EDITOR_MODELS = [
    "article", "articledraft", "eventv4", "event_occurrence", "eventlinks",
    "organization", "source", "area", "category", "tagcategories", "tag",
    "subtag", "genre", "filmlanguage", "appuser",
]
CONTRIBUTOR_PERMISSIONS = [
    "add_article", "change_article", "delete_article", "view_article",
    "add_articledraft", "change_articledraft", "delete_articledraft", "view_articledraft",
    "add_eventv4", "change_eventv4", "delete_eventv4", "view_eventv4",
    "add_event_occurrence", "change_event_occurrence", "delete_event_occurrence", "view_event_occurrence",
    "add_eventlinks", "change_eventlinks", "delete_eventlinks", "view_eventlinks",
    "change_organization", "view_organization",
    "view_area", "view_tag", "view_subtag", "view_category",
]


def create_groups(apps, schema_editor):
    # Rechte entstehen sonst erst nach dem Migrieren (post_migrate)
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    content_perms = Permission.objects.filter(content_type__app_label="content")

    editors, _ = Group.objects.get_or_create(pk=1, defaults={"name": "Editors"})
    editors.permissions.add(*content_perms.filter(content_type__model__in=EDITOR_MODELS))

    contributors, _ = Group.objects.get_or_create(pk=2, defaults={"name": "Contributors"})
    contributors.permissions.add(*content_perms.filter(codename__in=CONTRIBUTOR_PERMISSIONS))

    # Feste IDs setzen den ID-Zähler (PostgreSQL-Sequenz) nicht weiter; ohne
    # Nachziehen schlüge die nächste neu angelegte Gruppe mit doppelter ID fehl
    from django.core.management.color import no_style

    for sql in schema_editor.connection.ops.sequence_reset_sql(no_style(), [Group]):
        schema_editor.execute(sql)


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(create_groups, migrations.RunPython.noop),
    ]
