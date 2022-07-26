from guardian.shortcuts import assign_perm
import graphene
from django.contrib.contenttypes.models import ContentType
from django.apps import apps
import logging


logger = logging.getLogger(__name__)

guarded_models = {}

for app in apps.get_app_configs():
    if app.label not in ["facade", "hare"]:
        continue
    for model in app.get_models():
        guarded_models[
            f"{app.label}_{model.__name__}".replace(" ", "_").upper()
        ] = model

print(guarded_models)

AvailableModelsEnum = type(
    "AvailableModels",
    (graphene.Enum,),
    {m: m for m in guarded_models.keys()},
)
