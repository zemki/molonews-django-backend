"""
Tag-Vorschläge für Artikel und Events per lokalem Sprachmodell (siehe molo/llm.py).

Das Modell bekommt Titel und Beschreibung sowie die verfügbaren Tags und
antwortet per JSON-Schema mit 1-3 Tag-Namen aus genau dieser Liste.
"""

import re
from logging import getLogger

from drf_yasg import openapi
from drf_yasg.openapi import Parameter
from drf_yasg.utils import swagger_auto_schema
from rest_framework import serializers, status, viewsets
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from content.models import Tag
from molo import llm

logger = getLogger('molonews')

MIN_WORDS = 10
MAX_TAGS = 3

ARTICLE_SYSTEM_PROMPT = """Du bist ein Experte für die Kategorisierung von Nachrichtenartikeln.
Analysiere den Artikel und wähle die 1 bis 3 passendsten Tags aus der vorgegebenen Liste."""

EVENT_SYSTEM_PROMPT = """Du bist ein Experte für die Kategorisierung von Veranstaltungen und Events.
Analysiere die Veranstaltung und wähle die 1 bis 3 passendsten Themen aus der vorgegebenen Liste."""


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ('id', 'name', 'category')


class TagSerializerId(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ('id',)


def _clean_tag_name(name):
    """Entfernt Emojis und Sonderzeichen, damit das Modell die Namen besser versteht."""
    return re.sub(r'[^\w\s&-]', '', name).strip()


def suggest_tag_ids(system_prompt, title, description, available_tags):
    """
    Fragt das Sprachmodell nach passenden Tags.

    Args:
        available_tags: Iterable aus dicts mit 'id' und 'name'

    Returns:
        list[int]: höchstens MAX_TAGS Tag-IDs
    """
    ids_by_name = {}
    for tag in available_tags:
        clean = _clean_tag_name(tag['name'])
        if clean and clean not in ids_by_name:
            ids_by_name[clean] = tag['id']
    if not ids_by_name:
        return []

    names = list(ids_by_name)
    schema = {
        'type': 'object',
        'properties': {
            'tags': {
                'type': 'array',
                'items': {'type': 'string', 'enum': names},
                'minItems': 1,
                'maxItems': MAX_TAGS,
            },
        },
        'required': ['tags'],
        'additionalProperties': False,
    }
    user_prompt = (
        f"Titel: {title}\n"
        f"Beschreibung: {description}\n\n"
        f"Verfügbare Tags: {', '.join(names)}"
    )

    try:
        result = llm.generate_json(system_prompt, user_prompt, schema=schema, timeout=60)
    except llm.LLMError as exc:
        logger.error(f"Tag-Vorschlag fehlgeschlagen: {exc}")
        return []

    tag_ids = []
    for name in result.get('tags', []):
        tag_id = ids_by_name.get(name)
        if tag_id and tag_id not in tag_ids:
            tag_ids.append(tag_id)
    return tag_ids[:MAX_TAGS]


TAG_REQUEST_PARAMETERS = [
    Parameter(name="title", in_=openapi.IN_FORM, type=openapi.TYPE_STRING,
              description="Titel"),
    Parameter(name="abstract", in_=openapi.IN_FORM, type=openapi.TYPE_STRING,
              description="Kurzfassung bzw. Beschreibung"),
]

TAG_RESPONSES = {
    '200': openapi.Response(description='Liste der vorgeschlagenen Tags',
                            schema=TagSerializerId(many=True)),
    '204': openapi.Response(description='Titel oder Beschreibung fehlt'),
}


class _TagSuggestionViewSet(viewsets.ViewSet):
    parser_classes = (MultiPartParser, FormParser)
    system_prompt = ''

    def get_available_tags(self):
        raise NotImplementedError

    def create(self, request):
        title = request.POST.get('title')
        abstract = request.POST.get('abstract')

        if not title or not abstract:
            return Response(status=status.HTTP_204_NO_CONTENT)

        if len(f"{title} {abstract}".split()) < MIN_WORDS:
            return Response([], status=status.HTTP_200_OK)

        available_tags = list(self.get_available_tags())
        if not available_tags:
            logger.warning(f"{self.__class__.__name__}: keine Tags in der Datenbank")
            return Response([], status=status.HTTP_200_OK)

        tag_ids = suggest_tag_ids(self.system_prompt, title, abstract, available_tags)
        return Response([{"id": tag_id} for tag_id in tag_ids], status=status.HTTP_200_OK)


class ArticleTagViewSet(_TagSuggestionViewSet):
    """Schlägt Tags für einen Artikel vor (lokales Sprachmodell)."""

    queryset = Tag.objects.filter(type='article')
    system_prompt = ARTICLE_SYSTEM_PROMPT

    def get_available_tags(self):
        return Tag.objects.filter(type='article').values('id', 'name')

    @swagger_auto_schema(consumes=['multipart/form-data'],
                         manual_parameters=TAG_REQUEST_PARAMETERS, responses=TAG_RESPONSES)
    def create(self, request):
        return super().create(request)


class EventTagViewSet(_TagSuggestionViewSet):
    """Schlägt Themen für ein Event vor (lokales Sprachmodell)."""

    queryset = Tag.objects.filter(type='event')
    system_prompt = EVENT_SYSTEM_PROMPT

    def get_available_tags(self):
        return Tag.objects.filter(type='event', assigned_by_rule=False).values('id', 'name')

    @swagger_auto_schema(consumes=['multipart/form-data'],
                         manual_parameters=TAG_REQUEST_PARAMETERS, responses=TAG_RESPONSES)
    def create(self, request):
        return super().create(request)
