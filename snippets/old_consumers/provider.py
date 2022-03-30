from facade.subscriptions.provider import ProvidersEvent
from delt.messages.postman.log import LogLevel
from facade.utils import log_to_provision
from delt.messages.postman.unprovide.unprovide_done import UnprovideDoneMessage
from facade.enums import ProvisionStatus
from delt.messages import (
    UnprovideLogMessage,
    UnprovideCriticalMessage,
    ProvideLogMessage,
    ProvideDoneMessage,
    ProvideCriticalMessage,
)
from herre.bouncer.utils import bounced_ws
from ..models import Provider, Provision
from asgiref.sync import sync_to_async
from .base import BaseConsumer
import logging
import aiormq


logger = logging.getLogger(__name__)


def activate_provider_and_get_active_provisions(app, user):

    if user is None or user.is_anonymous:
        provider = Provider.objects.get(app=app, user=None)
    else:
        provider = Provider.objects.get(app=app, user=user)

    provider.active = True
    provider.save()

    if provider.user:
        ProvidersEvent.broadcast(
            {"action": "started", "data": str(provider.id)},
            [f"providers_user_{provider.user.id}"],
        )
    else:
        ProvidersEvent.broadcast(
            {"action": "started", "data": provider.id}, [f"all_providers"]
        )

    provisions = (
        Provision.objects.filter(template__provider=provider)
        .exclude(status__in=[ProvisionStatus.ENDED, ProvisionStatus.CANCELLED])
        .all()
    )

    requests = []
    for prov in provisions:
        requests.append(prov.to_message())

    print(requests)
    return provider, requests


@sync_to_async
def deactivateProvider(provider: Provider):
    provisions = Provision.objects.filter(template__provider_id=provider.id)

    for provision in provisions:
        print(provision)
        # TODO: Maybe send a signal here to the provisions

    provider.active = False
    provider.save()
    if provider.user:
        ProvidersEvent.broadcast(
            {"action": "ended", "data": provider.id},
            [f"providers_user_{provider.user.id}"],
        )
    else:
        ProvidersEvent.broadcast(
            {"action": "ended", "data": provider.id}, [f"all_providers"]
        )

    return provider


def initial_cleanup():
    for provider in Provider.objects.all():
        provider.active = False
        provider.save()


initial_cleanup()


class ProviderConsumer(BaseConsumer):  # TODO: Seperate that bitch
    mapper = {
        ProvideDoneMessage: lambda cls: cls.on_provide_done,
        ProvideLogMessage: lambda cls: cls.on_provide_log,
        ProvideCriticalMessage: lambda cls: cls.on_provide_critical,
        UnprovideLogMessage: lambda cls: cls.on_unprovide_log,
        UnprovideCriticalMessage: lambda cls: cls.on_unprovide_critical,
    }

    @bounced_ws(only_jwt=True)
    async def connect(self):
        await self.accept()
        # TODO: Check if in provider mode
        self.provider, self.start_provisions = await sync_to_async(
            activate_provider_and_get_active_provisions
        )(self.scope["bounced"].app, self.scope["bounced"].user)

        logger.warning(f"Connecting {self.provider.name}")
        logger.info("This provide is now active and will be able to provide Pods")

        await self.connect_to_rabbit()
        await self.send_initial_provisions()

    async def send_initial_provisions(self):
        for prov in self.start_provisions:
            await self.send_message(prov)

    async def connect_to_rabbit(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        # Declaring queue
        self.on_provide_queue = await self.channel.queue_declare(
            f"provision_in_{self.provider.unique}", auto_delete=True
        )

        await self.channel.basic_consume(
            self.on_provide_queue.queue, self.on_provide_related
        )

    async def on_provide_related(self, message: aiormq.abc.DeliveredMessage):
        """Provide Forward

        Simply forwards provide messages to the Provider on the Other end

        Args:
            message (aiormq.abc.DeliveredMessage): The delivdered message
        """
        logger.error(
            "OINAOINSOINAOISNOASNPOIUFENPIOUJBNAEPFUIABNWDPOIUBNAWPIDUBNAIDUJIUDJUDJ"
        )
        await self.send(text_data=message.body.decode())
        # No need to go through pydantic???
        await message.channel.basic_ack(message.delivery.delivery_tag)

    async def disconnect(self, close_code):
        try:
            logger.warning(
                f"Disconnecting Provider {self.provider.name} with close_code {close_code}"
            )
            # We are deleting all associated Provisions for this Provider
            await deactivateProvider(self.provider)
            await self.connection.close()
        except:
            logger.error("Something weird happened in disconnection!")

    async def on_provide_critical(self, provide_error: ProvideCriticalMessage):
        await self.forward(provide_error, provide_error.meta.extensions.callback)

    async def on_provide_log(self, message: ProvideLogMessage):
        await sync_to_async(log_to_provision)(
            message.meta.reference, message.data.message, level=message.data.level
        )
        if message.meta.extensions.progress is not None:
            await self.forward(message, message.meta.extensions.progress)

    async def on_unprovide_log(self, message: UnprovideLogMessage):
        await sync_to_async(log_to_provision)(
            message.data.provision, message.data.message, level=message.data.level
        )

    async def on_unprovide_critical(self, message: UnprovideCriticalMessage):
        await sync_to_async(log_to_provision)(
            message.data.provision, message.data.message, level=LogLevel.CRITICAL
        )
