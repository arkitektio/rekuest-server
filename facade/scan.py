from typing import Tuple, List
from .models import DataPoint, DataModel, Service, ServiceProvider
import requests
from delt.service import types
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
    data_query = types.DataQuery(**result.json())
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



def scan_service(host, port) -> Tuple[DataPoint, List[DataModel]]:
    """Scans a microservice for available datamodels and their identifiers according tot he well-known challenge

    See online Documentation for details how to setup your own Microservice

    Args:
        host ([type]): Inward facing hostadress for the well-known challange
        port ([type]): The port we should be choosing

    Returns:
        Tuple[DataPoint, List[DataModel]]: Datapoint and the models created
    """
    url = f"http://{host}:{port}/.well-known/arkitekt"
    logger.info(f"Scannning {host} at {url}")

    result = requests.get(url)
    serialized_service = types.Service(**result.json())

    service, created = Service.objects.update_or_create(name=serialized_service.name, defaults={
        "inward": serialized_service.inward, 
        "outward": serialized_service.outward,
        "port": serialized_service.port,
        "types": serialized_service.types,
        "version": "0.1",
        "name": serialized_service.name
    })

    if serialized_service.dependencies:
        for name in service.dependencies:
            depservice = Service.objects.get_or_create(name=name, defaults={"inward": "FAKE", "outward": "FAKE", "port": 0})
            depservice.parent.add(service)

    if types.ServiceType.DATA in serialized_service.types:
        # This Service provides Datamodels that we want to register with the Framework
        url = f"http://{service.inward}:{service.port}/.well-known/arnheim_query"
        logger.info(f"Scannning {host} at {url}")

    if types.ServiceType.PROVIDER in serialized_service.types:

        url = f"http://{service.inward}:{service.port}/.well-known/provider"
        provider, created = ServiceProvider.objects.update_or_create(service=service, defaults = {
            "name": service.name
        })
        logger.info(f"Is Provider: Scannning {service.name} at {url}")
        result = requests.post(url, {"unique": provider.unique})







    return service