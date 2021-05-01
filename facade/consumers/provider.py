# chat/consumers.py
from delt.messages.postman.unprovide.unprovide_done import UnprovideDoneMessage
from delt.messages.postman.unprovide.unprovide_critical import UnprovideCriticalMessage
from delt.messages.postman.unprovide.unprovide_progress import UnprovideProgressMessage
from delt.messages.postman.provide import ProvideDoneMessage, ProvideCriticalMessage, ProvideProgressMessage
from herre.bouncer.utils import bounced_ws
from ..models import Provider, Provision
from asgiref.sync import sync_to_async
from .base import BaseConsumer
import logging
import aiormq


logger = logging.getLogger(__name__)

@sync_to_async
def activateProviderForClientID(app, user):
    if user is None or user.is_anonymous:
        provider = Provider.objects.get(app=app, user=None)
        provider.active = True
        provider.save()

        return provider

    provider = Provider.objects.get(app=app, user=user)
    provider.active = True
    provider.save()

    return provider


@sync_to_async
def deactivateProvider(provider: Provider):
    provisions = Provision.objects.filter(template__provider_id=provider.id)
    for provision in provisions:
        print(provision)
        # TODO: Maybe send a signal here to the provisions


    provider.active = False
    provider.save()
    return provider
  

def initial_cleanup():
    for provider in Provider.objects.all():
        provider.active = False
        provider.save()

initial_cleanup()



class ProviderConsumer(BaseConsumer): #TODO: Seperate that bitch
    mapper = {
        ProvideDoneMessage: lambda cls: cls.on_provide_done,
        ProvideProgressMessage: lambda cls: cls.on_provide_progress,
        ProvideCriticalMessage: lambda cls: cls.on_provide_critical,

        UnprovideProgressMessage: lambda cls: cls.on_unprovide_progress,
        UnprovideCriticalMessage: lambda cls: cls.on_unprovide_critical,
    }

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.accept()
        #TODO: Check if in provider mode
        self.provider = await activateProviderForClientID(self.scope["bounced"].app, self.scope["bounced"].user)
        logger.warning(f"Connecting {self.provider.name}") 
        logger.info("This provide is now active and will be able to provide Pods")

        await self.connect_to_rabbit()

    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()
        # Declaring queue
        self.on_provide_queue = await self.channel.queue_declare(f"provision_in_{self.provider.unique}", auto_delete=True)
        self.on_unprovide_queue = await self.channel.queue_declare(f"unprovision_in_{self.provider.unique}", auto_delete=True)

        logger.warning("ddd")
        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.on_provide_queue.queue, self.on_provide)
        await self.channel.basic_consume(self.on_unprovide_queue.queue, self.on_provide)


    async def on_provide(self, message: aiormq.types.DeliveredMessage):
        #TODO: Maybe do some serialized first here
        logger.error(message.body.decode())
        await self.send(text_data=message.body.decode()) 
        # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def disconnect(self, close_code):
        try:
            logger.warning(f"Disconnecting {self.provider.name} with close_code {close_code}") 
            # We are deleting all associated Provisions for this Provider 
            await deactivateProvider(self.provider)
            await self.connection.close()
        except:
            logger.error("Something weird happened in disconnection!")

    async def on_unprovide_done(self, unprovide_done: UnprovideDoneMessage):
        if unprovide_done.meta.extensions.callback is not None:
            await self.forward(unprovide_done, unprovide_done.meta.extensions.callback)
        else:
            logger.warn("Was system Call (for example unprovide)")
        
    async def on_provide_done(self, provide_done: ProvideDoneMessage):
        if provide_done.meta.extensions.callback is not None:
            await self.forward(provide_done, provide_done.meta.extensions.callback)
        else:
            logger.warn("Was system Call (for example unprovide)")

    async def on_provide_critical(self, provide_error: ProvideCriticalMessage):
        await self.forward(provide_error, provide_error.meta.extensions.callback)

    async def on_unprovide_critical(self, provide_error: UnprovideCriticalMessage):
        await self.forward(provide_error, provide_error.meta.extensions.callback)

    async def on_provide_progress(self, provide_error: ProvideProgressMessage):
        await self.forward(provide_error, provide_error.meta.extensions.progress)

    async def on_unprovide_progress(self, message: UnprovideProgressMessage):
        if message.meta.extensions.progress is not None:
            await self.forward(message, message.meta.extensions.progress)
        else:
            logger.warn("Was system Call (for example unprovide)")


        

