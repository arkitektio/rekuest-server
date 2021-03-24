# chat/consumers.py
from delt.messages.postman.provide import ProvideDoneMessage, ProvideCriticalMessage, ProvideProgressMessage
from herre.bouncer.utils import bounced_ws
from ..models import AppProvider, Provision
from asgiref.sync import sync_to_async
from .base import BaseConsumer
import logging
import aiormq


logger = logging.getLogger(__name__)

@sync_to_async
def activateProviderForClientID(client_id):
    print(client_id)
    provider = AppProvider.objects.get(client_id=client_id)
    provider.active = True

    return provider

@sync_to_async
def get_provisions(provider: AppProvider):
    provisions = Provision.objects.filter(template__provider_id=provider.id)
    return {provision.reference: provision for provision in provisions}

@sync_to_async
def deactivateProviderForClientID(client_id):
    provider = AppProvider.objects.get(client_id=client_id)
    provider.active = False
    return provider


NO_PODS_CODE = 2
NOT_AUTHENTICATED_CODE = 3



class ProviderConsumer(BaseConsumer): #TODO: Seperate that bitch
    mapper = {
        ProvideDoneMessage: lambda cls: cls.on_provide_done,
        ProvideProgressMessage: lambda cls: cls.on_provide_progress,
        ProvideCriticalMessage: lambda cls: cls.on_provide_critical,
    }

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.accept()
        #TODO: Check if in provider mode
        self.provider = await activateProviderForClientID(self.scope["bounced"].client_id)
        logger.warning(f"Connecting {self.provider.name}") 
        self.provisions = await get_provisions(self.provider)
        print(self.provisions.keys())


        await self.connect_to_rabbit()

    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        # Declaring queue
        self.on_provide_queue = await self.channel.queue_declare(f"provision_in_{self.provider.unique}", auto_delete=True)
        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.on_provide_queue.queue, self.on_provide)



    async def on_provide(self, message: aiormq.types.DeliveredMessage):
        #TODO: Maybe do some serialized first here
        logger.error(message.body.decode())
        await self.send(text_data=message.body.decode()) 
        # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)


    async def disconnect(self, close_code):
        try:
            logger.warning(f"Disconnecting {self.provider.name} with close_code {close_code}") 
            #TODO: Depending on close code send message to all running Assignations
            await deactivateProviderForClientID(self.scope["bounced"].client_id)
            await self.connection.close()
        except:
            logger.error("Something weird happened in disconnection!")

        
    async def on_provide_done(self, provide_done: ProvideDoneMessage):
        await self.forward(provide_done, provide_done.meta.extensions.callback)

    async def on_provide_critical(self, provide_error: ProvideCriticalMessage):
        await self.forward(provide_error, provide_error.meta.extensions.callback)

    async def on_provide_progress(self, provide_error: ProvideProgressMessage):
        await self.forward(provide_error, provide_error.meta.extensions.progress)



        

