# chat/consumers.py
from facade.messages.activatepod import ActivatePodMessage
from herre.bouncer.utils import bounced_ws
from ..messages import AssignationMessage
from ..models import Pod, Provider
from ..enums import AssignationStatus, PodStatus
from asgiref.sync import sync_to_async
from .base import BaseConsumer
import logging
import aiormq


logger = logging.getLogger(__name__)

@sync_to_async
def activateProviderForClientID(client_id):
    print(client_id)
    provider = Provider.objects.get(app=client_id)
    provider.active = True
    return provider

@sync_to_async
def activatePod(pod_id, channel):
    pod = Pod.objects.get(id=pod_id)
    pod.status = PodStatus.ACTIVE
    pod.channel = channel
    pod.save()
    return pod.id, pod.template.channel

@sync_to_async
def deactivatePods(podids):
    pods = []
    for pod in Pod.objects.filter(pk__in=podids):
        pod.status = PodStatus.DOWN.value
        pod.channel = None
        pod.save()
        pods.append(pod)

    return True

NO_PODS_CODE = 2
NOT_AUTHENTICATED_CODE = 3



class ProviderConsumer(BaseConsumer): #TODO: Seperate that bitch

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.accept()
        #TODO: Check if in provider mode
        self.provider = await activateProviderForClientID(self.scope["bounced"].client_id)
        logger.warning(f"Connecting {self.provider.name}") 

        self.pod_queues = {}


        self.channel_name = await self.connect_to_rabbit()

    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()
        # Declaring queue
        self.on_provision_queue = await self.channel.queue_declare(auto_delete=True)

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.on_provision_queue.queue, self.on_provision)
        return self.on_provision_queue.queue
        
    async def on_assign_to_pod(self, message: aiormq.types.DeliveredMessage):
        logger.error(message.body.decode())
        await self.send(text_data=message.body.decode()) # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def on_provision(self, message: aiormq.types.DeliveredMessage):
        logger.error(message.body.decode())
        await self.send(text_data=message.body.decode()) # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def disconnect(self, close_code):
        try:
            logger.warning(f"Disconnecting {self.provider.name} with close_code {close_code}") 
            #TODO: Depending on close code send message to all running Assignations
            await deactivatePods([id for id, queue in self.pod_queues.items()])
            await self.connection.close()
        except:
            logger.error("Something weird happened in disconnection!")


    async def on_activate_pod(self, message: ActivatePodMessage):
        logger.warn("Activating Pod")

        podid, queuename = await activatePod(message.data.pod, self.channel_name)
        declared_queue = await self.channel.queue_declare(queuename)
        # Each pod through podman listens to the same pod

        await self.channel.basic_consume(declared_queue.queue, self.on_assign_to_pod)
        self.pod_queues[podid] = declared_queue
        

    async def on_assignation(self, assignation: AssignationMessage):
        
        if assignation.data.status == AssignationStatus.DONE:
            await self.channel.basic_publish(
                assignation.to_message(), routing_key="assignation_done",
                properties=aiormq.spec.Basic.Properties(
                    correlation_id=assignation.data.reference
            )
        )

        if assignation.data.status == AssignationStatus.ERROR:
            await self.channel.basic_publish(
                assignation.to_message(), routing_key="assignation_error",
                properties=aiormq.spec.Basic.Properties(
                    correlation_id=assignation.data.reference
            )
        )

        if assignation.data.status == AssignationStatus.YIELD:
            await self.channel.basic_publish(
                assignation.to_message(), routing_key="assignation_yield",
                properties=aiormq.spec.Basic.Properties(
                    correlation_id=assignation.data.reference
            )
        )
        
        elif assignation.data.status == AssignationStatus.PROGRESS:
            if assignation.meta.extensions.progress is not None:
                # We are publishing the progress not right to the listener but passt it first to assignation progress (in order to set the datamodels??)
                await self.channel.basic_publish(
                    assignation.to_message(), routing_key="assignation_progress",
                    properties=aiormq.spec.Basic.Properties(
                        correlation_id=assignation.data.reference
                )
        )




        

