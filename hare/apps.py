from django.apps import AppConfig
from health_check.plugins import plugin_dir


class HareConfig(AppConfig):
    name = "hare"

    def ready(self):
        from .health import HareHealthBackend

        plugin_dir.register(HareHealthBackend)
