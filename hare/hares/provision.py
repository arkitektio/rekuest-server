from delt.messages.exception import ExceptionMessage
from delt.messages.postman.provide import BouncedProvideMessage, BouncedCancelProvideMessage, ProvideDoneMessage
from typing import Tuple
from facade.models import Pod, Template
import logging
import aiormq
from asgiref.sync import sync_to_async
from .base import BaseHare

logger = logging.getLogger(__name__)


@sync_to_async
def find_pod_or_template_for_provide(provide: BouncedProvideMessage) -> Tuple[Pod,Template]:

    params = provide.data.params

    template = provide.data.template

    if template:
        template = Template.objects.select_related("provider").get(id=template)

        if template.is_active:
            pod = template.pods.first()
            # Check if assignable to pod
            logger.warn("Template is already active and provisioned, returning first Pod (Matching params)")

            logger.error("Not implemented yet... creating new Pod")

            # TODO: We should now already cause a Provision thing
            return None, template

        
        return None, template


    node = provide.data.node

    if node:
        qs = Template.objects.select_related("provider").filter(node_id=node)

        if params.providers:
            qs.filter(provider__name_in=params.providers)

        # Manage filtering by params {such and such}
        template =  qs.first()
        assert template is not None, f"Did not find an Template for this Node {provide.data.node} with params {provide.data.params}"
        return None, template


    raise Exception("This should never happen")




class ProvisionRabbit(BaseHare):

    def __init__(self) -> None:
        pass

    async def connect(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        # This queue gets called from the HTTP backend (so GraphQL Postman request) with an already created Assignation
        self.bounced_provide_in = await self.channel.queue_declare("bounced_provide_in")
        self.bounced_cancel_provide_in = await self.channel.queue_declare("bounced_cancel_provide_in")


        # We will get Results here
        self.provision_done = await self.channel.queue_declare("provision_done")

        # Start listening the queue with name 'hello'

        await self.channel.basic_consume(self.bounced_provide_in.queue, self.on_bounced_provide_in)
        await self.channel.basic_consume(self.bounced_cancel_provide_in.queue, self.on_bounced_cancel_provide_in)

    @BouncedCancelProvideMessage.unwrapped_message
    async def on_bounced_cancel_provide_in(self, cancel_provide: BouncedCancelProvideMessage, message: aiormq.types.DeliveredMessage):
        logger.warn(f"Received Provision Cancellation  {str(message.body.decode())}")
        # Thi should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)

    @BouncedProvideMessage.unwrapped_message
    async def on_bounced_provide_in(self, bounced_provide: BouncedProvideMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Bounced Provide {str(message.body.decode())}")

        try:
            pod, template = await find_pod_or_template_for_provide(bounced_provide)
            # We are routing it to the Template channel (pods will pick up and then reply to)
            if pod:
                provide_done = ProvideDoneMessage(data={"pod": pod.id}, meta={"reference": bounced_provide.meta.reference})
                # We were able to provision a Pod and are giving the callback notic
                await self.forward(provide_done, bounced_provide.meta.extensions.callback)


            if template:
                logger.warning(f"Providing {template.name} at {template.provider.name} and channel {template.provider.unique}")
                bounced_provide.data.template = template.id

                print(bounced_provide)

                await self.forward(bounced_provide, f"provision_in_{template.provider.unique}")

        except Exception as e:
            exception = ExceptionMessage.fromException(e, bounced_provide.meta.reference)
            await self.forward(exception, bounced_provide.meta.extensions.callback)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)





