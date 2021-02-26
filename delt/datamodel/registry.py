from delt.datamodel.views import DataPointViewBuilder, DataModelViewBuilder, DataQueryViewBuilder
from typing import List
from delt.datamodel.parser import DataModel, parse_data_models
from delt.settings import get_active_settings
from django.conf.urls import url
from django.urls import include, path, re_path



class DataModelRegistry:

    def __init__(self) -> None:
        self.settings = get_active_settings()
        self.models: List[DataModel] = []


    def registerInstalledModels(self, exclude=[]):
        self.models = parse_data_models()

    def registerDataModel(self, model: DataModel):
        self.models.append(model)
    
    def buildView(self):
        return DataModelViewBuilder(self)


    def buildPaths(self):
        return (
            url('.well-known/arnheim_models', DataModelViewBuilder(self).as_view()),
            url('.well-known/arnheim_point', DataPointViewBuilder(self).as_view()),
            url('.well-known/arnheim_query', DataQueryViewBuilder(self).as_view()),
        )





DATA_MODEL_REGISTRY = None

def get_datamodel_registry():
    global DATA_MODEL_REGISTRY
    if DATA_MODEL_REGISTRY is None:
        DATA_MODEL_REGISTRY = DataModelRegistry()
    return DATA_MODEL_REGISTRY