from django.apps import AppConfig


class RulesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.rules"
    label = "rules"

    def ready(self):
        from . import signals  # noqa: F401
