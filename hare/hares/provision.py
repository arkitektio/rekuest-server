import asyncio
from typing import List, Tuple
from delt.messages.provision_request import ProvisionRequestMessage
import logging

import aiormq
from asgiref.sync import sync_to_async
from facade.enums import PodStatus
from facade.messages import AssignationMessage, ProvisionMessage
from delt.messages.provision import ProvisionMessage, ProvisionParams
from delt.messages.assignation_request import AssignationRequestMessage
from delt.models import  Pod, Provision, Template
from delt.wrappers.instances import provisionRequestWrapper, provisionWrapper
from oauth2_provider.models import AccessToken

logger = logging.getLogger(__name__)


@sync_to_async
def create_provision_from_request(request: ProvisionRequestMessage):

    try:
        return Provision.objects.get(reference=request.data.reference)
    except Provision.DoesNotExist:

    
        auth = AccessToken.objects.get(token=request.meta.auth.token)
        #TODO: check for scopes

        # We are dealing with a Backend Application
        user = auth.user if auth.user else auth.application.user
        
        provision = Provision.objects.create(**{
            "parent": request.data.parent,
            "node_id": request.data.node,
            "params": request.data.params.dict(),
            "template_id": request.data.template,
            "user": user,
            "reference": request.data.reference,
        }
        )
        return provision


@sync_to_async
def get_template_and_provider(template_id):

    template = Template.objects.get(template_id)
    return (template.id, template.provider.name)

@sync_to_async
def find_templates_for_node_and_params(node_id, params: ProvisionParams) -> List[Tuple[int, str]]:
    """This returns an ordered list of potential template

    Args:
        node_id ([type]): [description]
        params (ProvisionParams): [description]

    Returns:
        [type]: [description]
    """
    templates = [ (template.id, template.provider.name) for template in Template.objects.filter(node_id=node_id) ]
    return templates






class ProvisionRabbit():

    def __init__(self) -> None:
        pass

    async def connect(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()


        self.provision_request_in = await self.channel.queue_declare('provision_request')

        # This queue gets called from the HTTP backend (so GraphQL Postman request) with an already created Assignation
        self.provision_in = await self.channel.queue_declare("provision_in")

        # We will get Results here
        self.provision_done = await self.channel.queue_declare("provision_done")

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.provision_request_in.queue, self.on_provision_request_in)
        await self.channel.basic_consume(self.provision_in.queue, self.on_provision_in)
        await self.channel.basic_consume(self.provision_done.queue, self.on_provision_done)




    @provisionRequestWrapper.unwrapped()
    async def on_provision_request_in(self, provision_request: ProvisionRequestMessage, message: aiormq.types.DeliveredMessage):
        logger.error(f"ProvisionRequest for Node {provision_request.data.node} received")
        
        provision = await create_provision_from_request(provision_request)


        if provision_request.meta.extensions.progress:
                print("Updating Progress")
                await message.channel.basic_publish(
                "Providing...".encode(), routing_key=provision_request.meta.extensions.progress,
                properties=aiormq.spec.Basic.Properties(
                    correlation_id=provision_request.meta.reference
                )
            )


        provision_message = await sync_to_async(ProvisionMessage.fromProvision)(provision, provision_request.meta)

        logger.error(f"Assignation for Node {provision_message.data.node} forwarded")
        # We have created an assignation and are passing this to the proper authorities
        await message.channel.basic_publish(
            provision_message.to_message(), routing_key="provision_in",
            properties=aiormq.spec.Basic.Properties(
                correlation_id=provision_message.meta.reference, # TODO: Check if we shouldnt use message.header.properties.correlation_id
                reply_to=provision_message.meta.extensions.callback,
            )
        )

        await message.channel.basic_ack(message.delivery.delivery_tag)


    @provisionWrapper.unwrapped()
    async def on_provision_in(self, provision: ProvisionMessage, message: aiormq.types.DeliveredMessage):
        
        
        if provision.data.template:
            # We are dealing with a specific request and will only just get the Template
            
            template_id, provider_name = await get_template_and_provider(provision.data.template)


        elif provision.data.node:
            # We are dealing with a policy required Request
            template_provider_list: List[Tuple[int, str]] = await find_templates_for_node_and_params(provision.data.node, provision.data.params)
            assert len(template_provider_list) >= 1, "We couldn't find a Template for this Node"

            template_id, provider_name = template_provider_list[0]

        else:
            raise Exception("This is not a valid request, neither Template nor Data is set. We should never reach here")
        
        
        provision.data.template = template_id

        await message.channel.basic_publish(
            provision.to_message(), routing_key=f"{provider_name}_provision_in",
            properties=aiormq.spec.Basic.Properties(
                correlation_id=provision.meta.reference, # TODO: Check if we shouldnt use message.header.properties.correlation_id
                reply_to=provision.meta.extensions.callback,
            )
        )

        await message.channel.basic_ack(message.delivery.delivery_tag)


    
    @provisionWrapper.unwrapped()
    async def on_provision_done(self, provision: ProvisionMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Provision Done {str(message.body.decode())}")


        # We are routing it to Pod One / This Pod will then reply to
        await message.channel.basic_publish(
            message.body, routing_key=provision.meta.extensions.callback,
            properties=aiormq.spec.Basic.Properties(
                correlation_id=provision.meta.reference,
            )
        )

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)