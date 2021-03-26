# chat/consumers.py
import asyncio
import json
from aiormq import channel
from delt.messages.postman.reserve import ReserveDoneMessage, ReserveCriticalMessage
from delt.messages.postman.provide import ProvideDoneMessage, ProvideCriticalMessage
from delt.messages.postman.assign import AssignYieldsMessage, AssignReturnMessage, AssignProgressMessage, AssignCriticalMessage
from delt.messages.host import ActivatePodMessage
from herre.bouncer.utils import bounced_ws
from ..models import Pod
from ..enums import AssignationStatus, PodStatus
from asgiref.sync import sync_to_async
from .base import BaseConsumer
import logging
import aiormq


logger = logging.getLogger(__name__)

@sync_to_async
def activatePod(pod_id):
    pod = Pod.objects.select_related("provision").prefetch_related("reservations").get(id=pod_id)
    pod.status = PodStatus.ACTIVE
    pod.save()
    return pod, pod.template.channel, pod.template.node.channel

@sync_to_async
def deactivatePods(podids):
    pods = []
    for pod in Pod.objects.filter(pk__in=podids):
        pod.delete()

    return True

@sync_to_async
def deactivatePod(podid):
    pod = Pod.objects.select_related("provision").prefetch_related("reservations").get(id=podid)
    pod.delete()
    return pod



NO_PODS_CODE = 2
NOT_AUTHENTICATED_CODE = 3


class HostConsumer(BaseConsumer): #TODO: Seperate that bitch
    mapper = {
        ActivatePodMessage: lambda cls: cls.on_activate_pod,
        AssignYieldsMessage: lambda cls: cls.on_assign_yields,
        AssignProgressMessage: lambda cls: cls.on_assign_progress,
        AssignCriticalMessage: lambda cls: cls.on_assign_critical,
        AssignReturnMessage: lambda cls: cls.on_assign_return,
    }

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.accept()
        logger.warning(f'Connecting Host {self.scope["bounced"].user}') 

        self.pod_queues = {}
        self.channel_name = await self.connect_to_rabbit()

    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        
    async def on_assign_to_pod(self, pod_id, message: aiormq.types.DeliveredMessage):
        logger.warn(message.body.decode())

        text_data= message.body.decode()
        jsond = json.loads(text_data)
        jsond["data"]["pod"] = pod_id

        await self.send(text_data=json.dumps(jsond)) # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def disconnect(self, close_code):
        try:
            logger.warning(f"Disconnecting Host with close_code {close_code}") 
            #TODO: Depending on close code send message to all running Assignations

            # We are assuming that every shutdown was ungently and check if we need to deactivate the pods
            await asyncio.gather(*[self.destroy_pod(id) for id, queue in self.pod_queues.items()])


            await self.connection.close()
        except Exception as e:
            logger.error(f"Something weird happened in disconnection! {e}")


    async def destroy_pod(self, pod_id, message="Pod Just Failed"):
        pod = await deactivatePod(pod_id)

        assert pod.provision is not None, "This should never happen"

        for reservation in pod.reservations.all():
            logger.info("Telling Reserving Clients that we went bye bye")
            await self.forward(ReserveCriticalMessage(
                data= {
                    "message": message
                },
                meta= {
                    "reference": reservation.reference,
                    "extensions": {
                        "callback": reservation.callback,
                        "progress": reservation.progress
                    }
                }), reservation.callback)

        
        if pod.provision.callback is not None:
            logger.info("Telling Our Providing Client that we went bye bye")
            await self.forward(ProvideCriticalMessage(
                data= {
                    "message": pod.id
                },
                meta= {
                    "reference": pod.provision.reference,
                    "extensions": {
                        "callback": pod.provision.callback,
                        "progress": pod.provision.progress
                    }
                }), pod.provision.callback)


        del self.pod_queues[pod_id]




    async def on_deactivate_pod(self, message: str):
        logger.warn("Deactiving Pod")



    async def on_activate_pod(self, message: ActivatePodMessage):
        logger.warn(f"Activating Pod {message.data.pod}")
        pod_id = message.data.pod

        pod, template_q, node_q = await activatePod(pod_id)
        pod_queue = await self.channel.queue_declare(pod.channel)
        template_queue = await self.channel.queue_declare(template_q)
        node_queue = await self.channel.queue_declare(node_q)
        # Each pod through podman listens to the same pod

        logger.warn(f"Listening to {pod.channel}")
        logger.warn(f"Listening to {template_q}")
        logger.warn(f"Listening to {node_q}")

        await self.channel.basic_consume(pod_queue.queue, lambda x: self.on_assign_to_pod(pod_id, x))
        #await self.channel.basic_consume(template_queue.queue, lambda x: self.on_assign_to_pod(pod_id, x))
        #await self.channel.basic_consume(node_queue.queue, lambda x: self.on_assign_to_pod(pod_id, x))


        self.pod_queues[pod_id] = (pod_queue, template_queue, node_queue)


        assert pod.provision is not None, "This should never happen"
        for reservation in pod.reservations.all():
            logger.info(f"Sending ReserveDone {reservation.reference}")
            await self.forward(ReserveDoneMessage(
                data= {
                    "channel": pod.channel
                },
                meta= {
                    "reference": reservation.reference,
                    "extensions": {
                        "callback": reservation.callback,
                        "progress": reservation.progress
                    }
                }), reservation.callback)

        
        if pod.provision.callback is not None:
            logger.info(f"Sending ProvideDone {pod.provision.reference}")
            await self.forward(ProvideDoneMessage(
                data= {
                    "pod": pod.id
                },
                meta= {
                    "reference": pod.provision.reference,
                    "extensions": {
                        "callback": pod.provision.callback,
                        "progress": pod.provision.progress
                    }
                }), pod.provision.callback)

        

    async def on_assign_critical(self, assign_critical: AssignCriticalMessage):
        await self.forward(assign_critical, assign_critical.meta.extensions.callback)

    async def on_assign_yields(self, assign_yield: AssignYieldsMessage):
        await self.forward(assign_yield, assign_yield.meta.extensions.callback)

    async def on_assign_return(self, assign_return: AssignReturnMessage):
        await self.forward(assign_return, assign_return.meta.extensions.callback)

    async def on_assign_progress(self, assign_return: AssignProgressMessage):
        await self.forward(assign_return, assign_return.meta.extensions.progress)





        

