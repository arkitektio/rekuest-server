# chat/consumers.py
import json
from aiormq import channel
from delt.messages.postman.provide.provide_done import ProvideDoneMessage
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
    pod = Pod.objects.select_related("created_by").get(id=pod_id)
    pod.status = PodStatus.ACTIVE
    pod.save()
    return pod, pod.template.channel, pod.template.node.channel

@sync_to_async
def deactivatePods(podids):
    pods = []
    for pod in Pod.objects.filter(pk__in=podids):
        pod.status = PodStatus.DOWN.value
        pod.save()
        pods.append(pod)

    return True

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
            await deactivatePods([id for id, queue in self.pod_queues.items()])
            await self.connection.close()
        except Exception as e:
            logger.error(f"Something weird happened in disconnection! {e}")


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


        await self.forward(ProvideDoneMessage(
            data= {
                "pod": pod.id
            },
            meta= {
                "reference": pod.created_by.reference,
                "extensions": {
                    "callback": pod.created_by.callback,
                    "progress": pod.created_by.progress
                }
            }), pod.created_by.callback)

        self.pod_queues[pod_id] = (pod_queue, template_queue, node_queue)
        

    async def on_assign_critical(self, assign_critical: AssignCriticalMessage):
        await self.forward(assign_critical, assign_critical.meta.extensions.callback)

    async def on_assign_yields(self, assign_yield: AssignYieldsMessage):
        await self.forward(assign_yield, assign_yield.meta.extensions.callback)

    async def on_assign_return(self, assign_return: AssignReturnMessage):
        await self.forward(assign_return, assign_return.meta.extensions.callback)

    async def on_assign_progress(self, assign_return: AssignProgressMessage):
        await self.forward(assign_return, assign_return.meta.extensions.progress)





        

