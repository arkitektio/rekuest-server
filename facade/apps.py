from django.apps import AppConfig


class FacadeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "facade"

    def ready(self):
        # Implicitly connect signal handlers decorated with @receiver.
        #
        # The embedding system check (the vector column is as wide as the release's model)
        # registers on import; ``migrate`` runs it, so a mismatch stops the migration job
        # before the service serves a wrong search.
        import embeddings.checks  # noqa: F401
