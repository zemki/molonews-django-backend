from django.apps import AppConfig


class ContentConfig(AppConfig):
    name = 'content'

    def ready(self):
        from content.signals import register_signals
        register_signals()
