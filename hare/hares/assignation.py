from delt.messages.postman.assign import BouncedCancelAssignMessage, AssignReturnMessage, BouncedAssignMessage, AssignMessage
from delt.messages.exception import ExceptionMessage
from facade.enums import AssignationStatus, PodStatus
from facade.models import Assignation, Pod, Template
import logging
import aiormq
from asgiref.sync import sync_to_async
from .base import BaseHare

logger = logging.getLogger(__name__)


@sync_to_async
def find_pod_for_assign(assign: AssignMessage):

    pod = assign.data.pod
    if pod:
        pod = Pod.objects.get(id=pod)
        assert pod.status == PodStatus.ACTIVE, "We cannot assign to a non active Pod"
        return pod

    template = assign.data.template
    if template:
        template = Template.objects.get(id=template)
        assert template.is_active, "You cannot assign to an unactive Template"
        return template.pods.first()

    node = assign.data.node
    if node:
        template = Template.objects.filter(pods__status=PodStatus.ACTIVE, node=node).first()
        assert template.is_active, "You cannot assign to an unactive Template"
        return template.pods.first()

    raise Exception("Did not find an Pod for Assignation")


class AssignationRabbit(BaseHare):

    def __init__(self) -> None:
        pass

    async def connect(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()


        self.bounced_assign_in = await self.channel.queue_declare('bounced_assign_in')
        self.bounced_cancel_assign_in = await self.channel.queue_declare("bounced_cancel_assign_in")

        # This queue gets called from the HTTP backend (so GraphQL Postman request) with an already created Assignation
        self.assignation_in = await self.channel.queue_declare("assignation_in")
        self.assignation_cancel = await self.channel.queue_declare("assignation_cancel")


        # We will get Results here
        self.assignation_done = await self.channel.queue_declare("assignation_done")
        self.assignation_error = await self.channel.queue_declare("assignation_error")
        self.assignation_yield = await self.channel.queue_declare("assignation_yield")
        self.assignation_progress = await self.channel.queue_declare("assignation_progress")

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.bounced_assign_in.queue, self.on_bounced_assign_in)
        await self.channel.basic_consume(self.bounced_cancel_assign_in.queue, self.on_bounced_cancel_assign_in)

    @BouncedCancelAssignMessage.unwrapped_message
    async def on_bounced_cancel_assign_in(self, cancel_assign: BouncedCancelAssignMessage, message: aiormq.types.DeliveredMessage):
        logger.warn(f"Received Assignation Cancellation  {str(message.body.decode())}")

        assert cancel_assign.data.pod is not None, "This assignation was never assigned. It will be hard to cancel it... RACE CONDITIAON MOTHERFUCKER"
        
        assignation = await sync_to_async(Assignation.objects.select_related("pod").get)(reference=cancel_assign.data.reference)
        
        await self.forward(cancel_assign, assignation.pod.channel)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)

    @BouncedAssignMessage.unwrapped_message
    async def on_bounced_assign_in(self, assign: BouncedAssignMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Assignation {str(message.body.decode())}")


        try:
            pod = await find_pod_for_assign(assign)
            # We are routing it to the Template channel (pods will pick up and then reply to)
            logger.info("Found the Following Templates we can assign too!")
            assign.data.pod = pod.id


            logger.warn(f"Assigning to the Following Templates we can assign too! {pod.channel}, {pod.id}")
            await self.forward(assign, routing_key=pod.channel)

        except Exception as e:

            exception = ExceptionMessage.fromException(e, assign.meta.reference)
            await self.forward(exception, routing_key=assign.meta.extensions.callback)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)

   





