from django import forms
from django.contrib import admin, messages
from django.http import Http404, HttpResponseForbidden, HttpResponseNotAllowed
from django.urls import path, reverse
from django.shortcuts import redirect
from logging import getLogger
from django.utils.html import format_html
from django.utils.safestring import mark_safe

import re
import subprocess
import sys

from django.conf import settings

from content.import_tools import script_generator
from content.import_tools.url_safety import check_url
from ..models import Tag
from .mixins import CantModifyRelatedMixin

logger = getLogger("molonews")


class SourceForm(CantModifyRelatedMixin, forms.ModelForm):

    dont_modify_related = ('organization', 'default_category')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance

        if instance and instance.default_category_id:
            queryset = Tag.objects.filter(category=instance.default_category).all()
        else:
            queryset = Tag.objects.none()

        self.fields['default_tags'] = forms.ModelMultipleChoiceField(
            queryset=queryset,
            required=False
        )

    def clean_link(self):
        link = self.cleaned_data.get('link')
        # Nur neue oder geänderte Links prüfen; der Server ruft sie beim Import ab
        if link and 'link' in self.changed_data:
            error = check_url(link)
            if error:
                raise forms.ValidationError(error)
        return link

    def clean(self):
        cleaned_data = super().clean()
        auto_detect = cleaned_data.get('auto_detect_area_from_content')

        # If auto_detect is enabled, clear the area field
        if auto_detect:
            cleaned_data['area'] = []

        return cleaned_data


class SourceAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'organization', 'active', 'priority', 'import_date', 'type', 'tags_display', 'display_errors', 'created_at')
    list_filter = ('name', 'organization', 'type', 'priority', 'created_at')
    list_editable = ('active', 'priority')
    readonly_fields = (
        'import_date', 'display_errors', "result_link", "import_script",
        "script_review", "script_actions",
    )
    # Die Freigabe wird nur über den Button "Script freigeben" gesetzt
    exclude = ('import_script_approved_hash',)
    form = SourceForm

    def get_readonly_fields(self, request, obj=None):
        fields = (
            'import_date', 'display_errors', 'result_link', 'import_script',
            'script_review', 'script_actions',
        )
        if request.user.is_superuser:
            fields += ('script_approve_button',)
        return fields

    class Media:
        js = ('admin/js/source_admin.js',)

    def tags_display(self, obj):
        return ", ".join([tag.name for tag in obj.default_tags.all()])

    tags_display.short_description = 'Tags'

    def display_errors(self, instance):
        if instance.id and instance.import_errors:
            errors = instance.import_errors
            if len(errors) > 80:
                return format_html('<span title="{}">{}&hellip;</span>', errors, errors[:80])
            return errors
        return ''
    display_errors.short_description = "Fehler"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        user = request.user

        # Wenn User Superuser ist, alle Sources anzeigen
        if user.is_superuser:
            return qs

        # Filtert Sources nach den Bereichen des Benutzers
        user_areas = user.area.values()
        all_user_areas = []
        for area in user_areas:
            all_user_areas.append(area["id"])

        # Sources anzeigen wenn:
        # 1. Areas passen zum User
        # 2. Keine Areas vorhanden (area_count=0)
        # 3. Auto-Detect aktiviert (auto_detect_area_from_content=True)
        # 4. User ist als related_user eingetragen
        from django.db.models import Q, Count
        qs = qs.annotate(area_count=Count('area')).filter(
            Q(area__in=all_user_areas) |
            Q(area_count=0) |
            Q(auto_detect_area_from_content=True) |
            Q(related_user=user)
        ).distinct()
        return qs

    def save_model(self, request, obj, form, change):
        default_image = None
        default_image_detail = None
        if obj.id is None and (obj.default_image is not None or obj.default_image_detail is not None):
            default_image = obj.default_image
            default_image_detail = obj.default_image_detail
            obj.default_image = None
            obj.default_image_detail = None

        super().save_model(request, obj, form, change)

        if default_image or default_image_detail:
            obj.default_image = default_image
            obj.default_image_detail = default_image_detail
            super().save_model(request, obj, form, change)

    # === Hier beginnt die Button-Logik ===
    #
    # Die Aktionen ändern Daten und sind deshalb nur per POST erreichbar. Die
    # Buttons stehen im Änderungsformular und senden es per ``formaction`` an die
    # jeweilige URL; das CSRF-Token des Formulars wird dabei mitgeschickt.

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:object_id>/clear-import-script/',
                self.admin_site.admin_view(self.clear_import_script_view),
                name='source_clear_import_script',
            ),
            path(
                '<int:object_id>/generate-script/',
                self.admin_site.admin_view(self.generate_script_view),
                name='source_generate_script',
            ),
            path(
                '<int:object_id>/approve-script/',
                self.admin_site.admin_view(self.approve_script_view),
                name='source_approve_script',
            ),
        ]
        return custom_urls + urls

    # Quellentyp -> (Script-Art, Import-Command)
    SCRIPT_SOURCE_TYPES = {
        'ai-article': (script_generator.KIND_ARTICLES, 'import_html_articles'),
        'ai-event': (script_generator.KIND_EVENTS, 'import_html_events'),
        'html-event': (script_generator.KIND_EVENTS, 'import_html_events'),
    }

    def _admin_url(self, name, obj):
        return reverse(f'{self.admin_site.name}:{name}', args=[obj.pk])

    def _change_url(self, obj):
        return self._admin_url('content_source_change', obj)

    def _get_object_for_action(self, request, object_id, superuser_only=False):
        """Gemeinsame Prüfung der Script-Aktionen: POST, Objekt, Berechtigung.

        Gibt (obj, None) oder (None, Antwort) zurück.
        """
        if request.method != 'POST':
            return None, HttpResponseNotAllowed(['POST'])
        obj = self.get_object(request, object_id)
        if obj is None:
            raise Http404("Quelle nicht gefunden")
        if superuser_only and not request.user.is_superuser:
            return None, HttpResponseForbidden("Nur Superuser dürfen Import-Scripts freigeben.")
        if not self.has_change_permission(request, obj):
            return None, HttpResponseForbidden("Keine Berechtigung für diese Quelle.")
        return obj, None

    def _remove_script_files(self, obj):
        """Löscht das gespeicherte Import-Script und die Vorschau einer Quelle."""
        if obj.link and obj.type in self.SCRIPT_SOURCE_TYPES:
            kind = self.SCRIPT_SOURCE_TYPES[obj.type][0]
            preview = script_generator.preview_path(kind, script_generator.preview_name(obj))
            if preview.exists():
                preview.unlink()
                logger.info(f"Vorschau-Datei gelöscht: {preview}")
        if obj.import_script:
            script_generator.delete_script(obj.import_script)

    def _reset_script(self, obj, status=''):
        self._remove_script_files(obj)
        obj.import_script = ''
        obj.import_script_approved_hash = ''
        obj.result = ''
        obj.import_errors = status
        obj.save()

    def clear_import_script_view(self, request, object_id):
        obj, response = self._get_object_for_action(request, object_id)
        if response:
            return response
        self._reset_script(obj)
        self.message_user(request, "Import-Script und Vorschau wurden gelöscht. Beim nächsten Import wird neu generiert.", messages.SUCCESS)
        return redirect(self._change_url(obj))

    def generate_script_view(self, request, object_id):
        """Generiert ein neues Import-Script für eine Source (ohne es auszuführen)."""
        obj, response = self._get_object_for_action(request, object_id)
        if response:
            return response

        if obj.type not in self.SCRIPT_SOURCE_TYPES:
            self.message_user(request, f"Script-Generierung ist nur für KI-Import-Typen verfügbar (aktuell: {obj.type}).", messages.WARNING)
            return redirect(self._change_url(obj))

        if not obj.link:
            self.message_user(request, "Kein Link konfiguriert. Bitte zuerst eine URL eintragen.", messages.ERROR)
            return redirect(self._change_url(obj))

        # Altes Script, Vorschau und Freigabe löschen
        self._reset_script(obj, status='Script-Generierung gestartet...')

        # Import-Command im Hintergrund starten
        command = self.SCRIPT_SOURCE_TYPES[obj.type][1]
        try:
            settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_file = open(settings.LOG_DIR / f'{command}.log', 'a')
            subprocess.Popen(
                [sys.executable, str(settings.BASE_DIR / 'manage.py'), command],
                cwd=str(settings.BASE_DIR),
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            self.message_user(
                request,
                "Script-Generierung wurde im Hintergrund gestartet. Seite in einigen Minuten neu laden, "
                "den Code prüfen und das Script freigeben.",
                messages.SUCCESS
            )
        except Exception as e:
            obj.import_errors = f"Fehler beim Starten: {str(e)}"
            obj.save(update_fields=['import_errors'])
            self.message_user(request, f"Fehler beim Starten der Script-Generierung: {e}", messages.ERROR)

        return redirect(self._change_url(obj))

    def approve_script_view(self, request, object_id):
        """Gibt das angezeigte Import-Script frei (nur Superuser)."""
        obj, response = self._get_object_for_action(request, object_id, superuser_only=True)
        if response:
            return response

        if not obj.import_script:
            self.message_user(request, "Es gibt kein Import-Script zum Freigeben.", messages.ERROR)
            return redirect(self._change_url(obj))

        try:
            current_hash = script_generator.script_hash(script_generator.read_script(obj.import_script))
        except (FileNotFoundError, OSError, UnicodeDecodeError) as e:
            self.message_user(request, f"Script-Datei nicht lesbar: {e}", messages.ERROR)
            return redirect(self._change_url(obj))

        # Freigegeben wird genau die Fassung, die im Admin angezeigt wurde
        if request.POST.get('_script_hash') != current_hash:
            self.message_user(
                request,
                "Das Script hat sich seit dem Laden der Seite geändert. Bitte den Code erneut prüfen.",
                messages.ERROR,
            )
            return redirect(self._change_url(obj))

        try:
            script_generator.approve_script(obj)
        except script_generator.ScriptRejected as e:
            self.message_user(request, f"Script nicht freigegeben: {e}", messages.ERROR)
            return redirect(self._change_url(obj))

        if obj.import_errors in (script_generator.PENDING_APPROVAL_NOTE, script_generator.CHANGED_AFTER_APPROVAL_NOTE):
            obj.import_errors = ''
        obj.save(update_fields=['import_script_approved_hash', 'import_errors'])
        logger.info(f"Import-Script {obj.import_script} von {request.user} freigegeben (SHA-256 {current_hash})")
        self.message_user(request, "Script freigegeben. Es wird beim nächsten Importlauf ausgeführt.", messages.SUCCESS)
        return redirect(self._change_url(obj))

    def script_review(self, obj):
        """Code des Import-Scripts zur Prüfung, mit Freigabestatus."""
        if not obj or not obj.pk or not obj.import_script:
            return "-"
        try:
            code = script_generator.read_script(obj.import_script)
        except (FileNotFoundError, OSError, UnicodeDecodeError) as e:
            return f"Script-Datei nicht lesbar: {e}"

        current_hash = script_generator.script_hash(code)
        if obj.import_script_approved_hash == current_hash:
            status, color = "Freigegeben – wird beim Import ausgeführt", "#2e7d32"
        elif obj.import_script_approved_hash:
            status, color = "Nach der Freigabe verändert – wird nicht ausgeführt", "#c62828"
        else:
            status, color = "Nicht freigegeben – wird nicht ausgeführt", "#c62828"

        safety_error = script_generator.check_script_safety(code)
        safety = (
            format_html('<p style="color:#c62828;">Sicherheitsprüfung: {}</p>', safety_error)
            if safety_error else
            mark_safe('<p>Sicherheitsprüfung: keine verbotenen Imports oder Aufrufe gefunden. '
                      'Der Code muss trotzdem gelesen werden.</p>')
        )
        return format_html(
            '<p style="color:{};font-weight:bold;">{}</p>{}'
            '<p>SHA-256: <code>{}</code></p>'
            '<pre style="max-height:600px;overflow:auto;white-space:pre;font-size:12px;'
            'background:#f6f6f6;padding:10px;border:1px solid #ddd;">{}</pre>',
            color, status, safety, current_hash, code,
        )
    script_review.short_description = "Import-Script prüfen"

    def script_actions(self, obj):
        if not obj or not obj.pk or obj.type not in self.SCRIPT_SOURCE_TYPES:
            return ''
        return format_html(
            '<button type="submit" class="button" formmethod="post" formnovalidate formaction="{}" '
            'style="background:#417690;color:#fff;padding:5px 15px;">Script neu generieren</button>'
            '<button type="submit" class="button" formmethod="post" formnovalidate formaction="{}" '
            'style="margin-left:10px;">Import-Script löschen</button>',
            self._admin_url('source_generate_script', obj),
            self._admin_url('source_clear_import_script', obj),
        )
    script_actions.short_description = "Import-Script"

    def script_approve_button(self, obj):
        """Freigabe-Button (nur für Superuser sichtbar, siehe get_readonly_fields)."""
        if not obj or not obj.pk or not obj.import_script:
            return '-'
        try:
            code = script_generator.read_script(obj.import_script)
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return '-'
        current_hash = script_generator.script_hash(code)
        if obj.import_script_approved_hash == current_hash:
            return 'Bereits freigegeben'
        if script_generator.check_script_safety(code):
            return 'Freigabe nicht möglich: Sicherheitsprüfung fehlgeschlagen'
        return format_html(
            '<button type="submit" class="button" formmethod="post" formnovalidate formaction="{}" '
            'name="_script_hash" value="{}" style="background:#2e7d32;color:#fff;padding:5px 15px;">'
            'Script freigeben</button>'
            '<p class="help">Gibt genau den oben angezeigten Code frei. Vorher vollständig lesen.</p>',
            self._admin_url('source_approve_script', obj),
            current_hash,
        )
    script_approve_button.short_description = "Freigabe"

    def result_link(self, obj):
        """
        Zeigt das Result-Feld als klickbaren Link im Admin.
        """
        if obj.result and re.match(r'^https?://', str(obj.result)):
            return format_html(
                '<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>',
                url=obj.result
            )
        elif obj.result:
            return obj.result
        return "-"
    result_link.short_description = "Probe-Import"

# Registriere das Model (passen falls nötig)
# from ..models import Source
# admin.site.register(Source, SourceAdmin)
