from django.apps import AppConfig


class FacadeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "facade"

    def ready(self):
        # Implicitly connect signal handlers decorated with @receiver.
        #
        # The embedding system checks (model width == EMBEDDINGS.DIMENSIONS == the vector
        # column's width) register on import; ``migrate`` runs the database-tagged one at
        # every boot, so a mismatch stops the service before it serves a wrong search.
        import embeddings.checks  # noqa: F401
