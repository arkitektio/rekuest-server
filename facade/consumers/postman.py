# chat/consumers.py
from facade.messages.postman import cancel
from facade.messages.postman.cancel import CancelAssignMessage
from facade.messages.postman.assign import AssignMessage
from facade.messages.assignation import AssignationMessage
from facade.models import Assignation
from asgiref.sync import sync_to_async
from facade.consumers.base import BaseConsumer
from herre.bouncer.utils import bounced_ws
from ..messages import MessageModel, AssignationRequestMessage, ProvisionRequestMessage
from ..messages.types import ASSIGNATION_REQUEST, PROVISION_REQUEST
from channels.generic.websocket import AsyncWebsocketConsumer
import logging
import aiormq
import logging

logger = logging.getLogger(__name__)


@sync_to_async
def create_assignation_from_request(request: AssignationRequestMessage, user, callback, progress) -> AssignationMessage:

    try:
        return Assignation.objects.get(reference=request.data.reference)
    except Assignation.DoesNotExist:
        assignation = Assignation.objects.create(**{
            "inputs": request.data.inputs,
            "node_id": request.data.node,
            "pod_id":request.data.pod,
            "template_id": request.data.template,
            "creator": user,
            "callback": callback,
            "progress": progress,
            "reference": request.data.reference
        }
        )

    return AssignationMessage.fromAssignation(assignation)

@sync_to_async
def create_assignation_from_assign(assign: AssignMessage, user, callback, progress) -> AssignationMessage:

    try:
        return Assignation.objects.get(reference=assign.meta.reference)
    except Assignation.DoesNotExist:
        assignation = Assignation.objects.create(**{
            "inputs": assign.data.inputs,
            "node_id": assign.data.node,
            "pod_id":assign.data.pod,
            "template_id": assign.data.template,
            "creator": user,
            "callback": callback,
            "progress": progress,
            "reference": assign.meta.reference
        }
        )

    return AssignationMessage.fromAssignation(assignation)


class PostmanConsumer(BaseConsumer):

    @bounced_ws(only_jwt=True)
    async def connect(self):
        logger.error(f"Connecting Postman {self.scope['user']}")
        await self.accept()
        self.callback_name, self.progress_name = await self.connect_to_rabbit()


        self.user = self.scope["user"]
        

    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()
        # Declaring queue
        self.callback_queue = await self.channel.queue_declare(auto_delete=True)
        self.progress_queue = await self.channel.queue_declare(auto_delete=True)

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.callback_queue.queue, self.on_callback)
        await self.channel.basic_consume(self.progress_queue.queue, self.on_progress)
        return self.callback_queue.queue, self.progress_queue.queue
        

    async def on_callback(self, message):
        logger.error(message)
        await self.send(text_data=message.body.decode()) # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def on_progress(self, message):
        logger.info(message)
        await self.send(text_data=message.body.decode()) # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)

    async def disconnect(self, close_code):
        logger.info(f"Disconnecting Postman {close_code}")
        await self.connection.close()


    async def on_provision_request(self,provision_request: ProvisionRequestMessage):
        provision_request.meta.extensions.callback = self.callback_name
        provision_request.meta.extensions.progress = self.progress_name
        logger.info(f"Received provision request for Node {provision_request.data.node}")


        await self.channel.basic_publish(
            provision_request.to_message(), routing_key="provision_request",
            properties=aiormq.spec.Basic.Properties(
                correlation_id=provision_request.meta.reference,
                reply_to=self.callback_name
        )
        )


    async def on_assignation_request(self, assignation_request: AssignationRequestMessage):
        assignation = await create_assignation_from_request(assignation_request, self.user, self.callback_name, self.progress_name)

        await self.channel.basic_publish(
            assignation.to_message(), routing_key="assignation_in",
            properties=aiormq.spec.Basic.Properties(
                correlation_id=assignation.data.reference,
                reply_to=self.callback_name
        )
        )


    async def on_assign(self, assign: AssignMessage):
        assignation: AssignationMessage = await create_assignation_from_assign(assign, self.user, self.callback_name, self.progress_name)
        print(assignation)
        await self.channel.basic_publish(
            assignation.to_message(), routing_key="assignation_in",
            properties=aiormq.spec.Basic.Properties(
                correlation_id=assignation.meta.reference,
                reply_to=self.callback_name
        )
        )

    async def on_cancel_assign(self, cancel_assign: CancelAssignMessage):
        print(cancel_assign)




            

