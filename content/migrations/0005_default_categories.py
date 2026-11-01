"""Standard-Kategorien und AppUser.area ohne festen Standardwert.

Der Code erkennt Kategorien über ihre ID (wie die Gruppen in 0002):

    1  Artikel-Themen (Standardkategorie neuer Quellen)
    2  Event-Themen

Artikel-Themen dürfen zusätzlich in den Kategorien 3 und 4 liegen. Bestehende
Kategorien mit diesen IDs bleiben unverändert.

AppUser.area hatte als Standardwert fest das Gebiet 3. Auf einer neuen
Installation gibt es das nicht, und das Anlegen jedes Geräteprofils scheiterte.
"""

import django.db.models.deletion
from django.core.management.color import no_style
from django.db import migrations, models

DEFAULT_CATEGORIES = [
    (1, "articles", "Artikel", 1),
    (2, "events", "Veranstaltungen", 2),
]


def create_categories(apps, schema_editor):
    Category = apps.get_model("content", "Category")
    for pk, name, title, rank in DEFAULT_CATEGORIES:
        Category.objects.get_or_create(
            pk=pk, defaults={"name": name, "title": title, "description": "", "rank": rank}
        )
    # Feste IDs: PostgreSQL-Sequenz nachziehen (siehe 0002)
    for sql in schema_editor.connection.ops.sequence_reset_sql(no_style(), [Category]):
        schema_editor.execute(sql)


class Migration(migrations.Migration):

    dependencies = [
        ("content", "0004_user_token_revocation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="appuser",
            name="area",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="content.area",
                verbose_name="area",
            ),
        ),
        migrations.RunPython(create_categories, migrations.RunPython.noop),
    ]
