# chat/consumers.py
import asyncio
import json
from delt.messages import *
from herre.bouncer.utils import bounced_ws
from ..models import Pod, Provision
from ..enums import PodStatus
from asgiref.sync import sync_to_async
from .base import BaseConsumer
import logging
import aiormq
from delt.messages.utils import expandFromRabbitMessage


logger = logging.getLogger(__name__)


@sync_to_async
def getOrCreatePodFromProvision(provision):

    provision = Provision.objects.get(reference=provision)
    
    #TODO: Check if we are able to susbcribe to the same Topic throught the Provision
    # Provision holds the information how many pods are okay to have

    pod = Pod.objects.create(**{
                "template": provision.template,
                "status": PodStatus.ACTIVE,
                "provision": provision
            }
        )

    reservation = provision.reservation
    pod.reservations.add(reservation)
    pod.save()

    pod = Pod.objects.select_related("provision").prefetch_related("reservations").get(id=pod.id)
    return pod

@sync_to_async
def deltePodFromProvision(provision):

    pod = Pod.objects.select_related("provision__reservation").prefetch_related("reservations").get(provision__reference=provision)
    pod.delete()

    return pod




@sync_to_async
def deactivatePod(podid):
    pod = Pod.objects.select_related("provision").prefetch_related("reservations").get(id=podid)
    pod.delete()
    return pod


def initial_cleanup():
    for pod in Pod.objects.all():
        pod.delete()

initial_cleanup()


NO_PODS_CODE = 2
NOT_AUTHENTICATED_CODE = 3


class HostConsumer(BaseConsumer): #TODO: Seperate that bitch
    mapper = {
        BouncedProvideMessage: lambda cls: cls.on_bounced_provide,
        BouncedUnprovideMessage: lambda cls: cls.on_bounced_unprovide,
        UnprovideMessage: lambda cls: cls.on_unprovide_done,
        AssignYieldsMessage: lambda cls: cls.on_assign_yields,
        AssignProgressMessage: lambda cls: cls.on_assign_progress,
        AssignCriticalMessage: lambda cls: cls.on_assign_critical,
        AssignReturnMessage: lambda cls: cls.on_assign_return,
        UnassignCriticalMessage: lambda cls: cls.on_unassign_critical,
        UnassignDoneMessage: lambda cls: cls.on_unassign_done,
        UnassignProgressMessage: lambda cls: cls.on_unassign_progress,
    }

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.accept()
        logger.warning(f'Connecting Host {self.scope["bounced"].app_name}') 

        self.provision_queues = {}
        self.hosted_pods = {}
        self.channel_name = await self.connect_to_rabbit()

    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        
    async def on_assign_related(self, provision_reference, message: aiormq.types.DeliveredMessage):
        nana = expandFromRabbitMessage(message)
        
        if isinstance(nana, BouncedAssignMessage):
            forwarded_message = BouncedForwardedAssignMessage(data={**nana.data.dict(), "provision": provision_reference}, meta={**nana.meta.dict(), "type": "bounced_forwarded_assign"})
            await self.send_message(forwarded_message) # No need to go through pydantic???
            
        elif isinstance(nana, BouncedUnassignMessage):
            await self.send_message(nana) 

        else:
            logger.error("This message is not what we expeceted here")
            
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def disconnect(self, close_code):
        try:
            logger.warning(f"Disconnecting Host with close_code {close_code}") 
            #TODO: Depending on close code send message to all running Assignations
            # We are assuming that every shutdown was ungently and check if we need to deactivate the pods
            await asyncio.gather(*[self.destroy_pod(id) for id, queue in self.hosted_pods.items()])
            await self.connection.close()
        except Exception as e:
            logger.error(f"Something weird happened in disconnection! {e}")


    async def on_bounced_unprovide(self, message: BouncedUnprovideMessage):

        pod = await deltePodFromProvision(message.data.provision)

        assert pod.provision is not None, "This should never happen"

        await self.forward(UnprovideDoneMessage(
                data= {
                    "provision": pod.provision.reference
                },
                meta= {
                    "reference": message.meta.reference,
                    "extensions": {
                        "callback": None,
                        "progress": None
                    }
        }), message.meta.extensions.callback)

        await self.forward(UnreserveDoneMessage(
                data= {
                    "reservation": pod.provision.reservation.reference
                },
                meta= {
                    "reference": message.meta.reference,
                    "extensions": {
                        "callback": None,
                        "progress": None
                    }
        }), message.meta.extensions.callback)

        for reservation in pod.reservations.all():
            logger.info("Telling Reserving Clients that we went bye bye")
            await self.forward(UnreserveDoneMessage(
                data= {
                    "reservation": reservation.reference
                },
                meta= {
                    "reference": reservation.reference,
                    "extensions": {
                        "callback": reservation.callback,
                        "progress": reservation.progress
                    }
                }), reservation.callback)

        
        if pod.provision.callback is not None:
            logger.info("Telling Our Providing Client that we went bye bye ")
            await self.forward(UnprovideDoneMessage(
                data= {
                    "provision": pod.provision.reference
                },
                meta= {
                    "reference": pod.provision.reference,
                    "extensions": {
                        "callback": pod.provision.callback,
                        "progress": pod.provision.progress
                    }
                }), pod.provision.callback)


        del self.provision_queues[message.data.provision]

    async def on_bounced_provide(self, message: BouncedProvideMessage):
        logger.warn(f"Activating Provision {message.meta.reference}")

        provision_reference = message.meta.reference

        pod = await getOrCreatePodFromProvision(provision_reference)

        assign_queue = await self.channel.queue_declare(str(pod.channel))
        cancel_me_queue = await self.channel.queue_declare("cancel_me_"+provision_reference)


        # Each pod through podman listens to the same pod
        logger.warn(f"Listening to Topic {assign_queue.queue} and Awaiting Cancellation on {cancel_me_queue.queue}")

        await self.channel.basic_consume(assign_queue.queue, lambda x: self.on_assign_related(provision_reference, x))
        await self.channel.basic_consume(cancel_me_queue.queue, lambda x: self.on_cancel_pod(provision_reference, x))


        self.provision_queues[message.meta.reference] = (assign_queue, cancel_me_queue)
        self.hosted_pods[pod.id] = pod.id

        assert pod.provision is not None, "This should never happen"

        for reservation in pod.reservations.all():
            logger.info(f"Sending ReserveDone {reservation.reference}")
            await self.forward(ReserveDoneMessage(
                data= {
                    "topic": pod.channel
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

            provide_done = ProvideDoneMessage(
                data= {
                    "pod": pod.id
                },
                meta= {
                    "reference": pod.provision.reference,
                    "extensions": {
                        "callback": pod.provision.callback,
                        "progress": pod.provision.progress
                    }
                })

            await self.send_message(provide_done)
            await self.forward(provide_done, pod.provision.callback)


    async def destroy_pod(self, pod_id, message="Pod Just Failed"):
        ''' This Method gets called if we lost a pod due to failure '''
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


        del self.provision_queues[pod.provision.reference]


    async def on_assign_critical(self, assign_critical: AssignCriticalMessage):
        await self.forward(assign_critical, assign_critical.meta.extensions.callback)

    async def on_assign_yields(self, assign_yield: AssignYieldsMessage):
        await self.forward(assign_yield, assign_yield.meta.extensions.callback)

    async def on_assign_return(self, assign_return: AssignReturnMessage):
        await self.forward(assign_return, assign_return.meta.extensions.callback)

    async def on_assign_progress(self, assign_return: AssignProgressMessage):
        await self.forward(assign_return, assign_return.meta.extensions.progress)

    async def on_unassign_done(self, assign_return: UnassignDoneMessage):
        print("oisdnoisndofinsdo9finh")
        await self.forward(assign_return, assign_return.meta.extensions.callback)

    async def on_unassign_progress(self, assign_return: UnassignProgressMessage):
        await self.forward(assign_return, assign_return.meta.extensions.progress)
    
    async def on_unassign_critical(self, assign_return: UnassignCriticalMessage):
        print("oisdnoisndofinsdfsdfsegserhgsefgsefsefo9finh")
        await self.forward(assign_return, assign_return.meta.extensions.callback)





        

