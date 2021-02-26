

from facade.messages.assignation import AssignationMessage
from facade.messages.provision_request import ProvisionRequestMessage
from facade.messages.activatepod import ActivatePodMessage
from channels.generic.websocket import AsyncWebsocketConsumer
from facade.messages.types import ACTIVATE_POD, ASSIGNATION_REQUEST, PROVISION_REQUEST,ASSIGNATION
from facade.messages import AssignationRequestMessage
import json
import logging

logger = logging.getLogger(__name__)

class BaseConsumer(AsyncWebsocketConsumer):
    expander = {
        ASSIGNATION_REQUEST: AssignationRequestMessage,
        ASSIGNATION: AssignationMessage,
        PROVISION_REQUEST: ProvisionRequestMessage,
        ACTIVATE_POD: ActivatePodMessage,
    }

    mapper = {
        AssignationRequestMessage: lambda cls: cls.on_assignation_request,
        ActivatePodMessage: lambda cls: cls.on_activate_pod,
        ProvisionRequestMessage: lambda cls: cls.on_provision_request,
        AssignationMessage: lambda cls: cls.on_assignation,
    }

    async def catch(self, text_data, exception=None):
        raise NotImplementedError(f"Received untyped request {text_data}: {exception}")


    async def receive(self, text_data):
        try:
            json_dict = json.loads(text_data)
            type = json_dict["meta"]["type"]
            modelType = self.expander[type]
            model = modelType(**json_dict)
            function = self.mapper[modelType](self)
            await function(model)
        except Exception as e:
            logger.error(e)
            self.catch(text_data)

