


from typing import Tuple, List
from .models import DataPoint, DataModel
import requests
from delt.datamodel import pydantic
import logging

logger = logging.getLogger(__name__)

def scan_service_for_models(host, port) -> Tuple[DataPoint, List[DataModel]]:
    """Scans a microservice for available datamodels and their identifiers according tot he well-known challenge

    See online Documentation for details how to setup your own Microservice

    Args:
        host ([type]): Inward facing hostadress for the well-known challange
        port ([type]): The port we should be choosing

    Returns:
        Tuple[DataPoint, List[DataModel]]: Datapoint and the models created
    """
    url = f"http://{host}:{port}/.well-known/arnheim_query"
    logger.info(f"Scannning {host} at {url}")

    result = requests.get(url)
    data_query = pydantic.DataQuery(**result.json())
    point = data_query.point

    datapoint, created = DataPoint.objects.update_or_create(inward=point.inward, port=point.port, defaults = {
        "type":point.type,
        "outward": point.outward,
        "version":data_query.version
    })

    models = []
    for model in data_query.models:
        model, created = DataModel.objects.update_or_create(point= datapoint, identifier=model.identifier, defaults = {
            "extenders": model.extenders
        })
        models.append(model)

    return datapoint, models