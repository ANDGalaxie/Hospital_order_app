from django.apps import AppConfig


class HospitalEngagementsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hospital_engagements"

    def ready(self):
        from . import signals  # noqa: F401
