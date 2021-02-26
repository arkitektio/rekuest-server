from facade.enums import PodStatus
from facade.messages import AssignationMessage, AssignationAction, AssignationRequestMessage
from facade.models import Assignation, Pod
import logging
import aiormq
from asgiref.sync import sync_to_async
from .base import BaseHare

logger = logging.getLogger(__name__)






@sync_to_async
def find_templates_for_assignation(assignation):
    assert assignation, "Please first create an Assignation"
    templates = [ template for template in assignation.node.templates.all() ]
    return templates


@sync_to_async
def find_pods_for_assignation(assignation: AssignationMessage):
    assert assignation, "Please first create an Assignation"
    pods = [pod for pod in Pod.objects.filter(template__node_id=assignation.data.node, status=PodStatus.ACTIVE.value) ]
    return pods

@sync_to_async
def update_assignation_with_pod(assignation: AssignationMessage, pod: Pod):
    assignation = Assignation.objects.get(reference=assignation.data.reference)
    assignation.pod = pod
    assignation.save()
    return True




class AssignationRabbit(BaseHare):

    def __init__(self) -> None:
        pass

    async def connect(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()


        self.assignation_request_in = await self.channel.queue_declare('assignation_request')

        # This queue gets called from the HTTP backend (so GraphQL Postman request) with an already created Assignation
        self.assignation_in = await self.channel.queue_declare("assignation_in")
        self.assignation_cancel = await self.channel.queue_declare("assignation_cancel")


        # We will get Results here
        self.assignation_done = await self.channel.queue_declare("assignation_done")
        self.assignation_error = await self.channel.queue_declare("assignation_error")
        self.assignation_yield = await self.channel.queue_declare("assignation_yield")
        self.assignation_progress = await self.channel.queue_declare("assignation_progress")

        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.assignation_request_in.queue, self.on_assignation_request_in)
        await self.channel.basic_consume(self.assignation_done.queue, self.on_assignation_done)
        await self.channel.basic_consume(self.assignation_in.queue, self.on_assignation_in)
        await self.channel.basic_consume(self.assignation_cancel.queue, self.on_assignation_cancel)
        await self.channel.basic_consume(self.assignation_progress.queue, self.on_assignation_progress)
        await self.channel.basic_consume(self.assignation_yield.queue, self.on_assignation_yield)
        await self.channel.basic_consume(self.assignation_error.queue, self.on_assignation_error)

    @AssignationMessage.unwrapped_message
    async def on_assignation_cancel(self, assignation: AssignationMessage, message: aiormq.types.DeliveredMessage):
        logger.warn(f"Received Assignation Cancellation  {str(message.body.decode())}")

        assert assignation.data.pod is not None, "This assignation was never assigned. It will be hard to cancel it... endless loop??????"
        
        pod = await sync_to_async(Pod.objects.get)(id=assignation.data.pod)
        # We are routing it to Pod One / This Pod will then reply to
        logger.info("Found the Following Pods we can assign too!")

        assignation.data.status = "CANCEL"
        
        await message.channel.basic_publish(
            assignation.to_message(), routing_key=pod.channel, # Lets take the first best one
            properties=aiormq.spec.Basic.Properties(
                correlation_id=assignation.data.reference,
                reply_to=self.assignation_done.queue
            )
        )

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)

    @AssignationMessage.unwrapped_message
    async def on_assignation_in(self, assignation: AssignationMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Received Assignation {str(message.body.decode())}")

        pods = await find_pods_for_assignation(assignation)
        # We are routing it to Pod One / This Pod will then reply to
        logger.info("Found the Following Pods we can assign too!")

        if len(pods) >= 1:

            pod = pods[0]
            assignation.data.pod = pod.id

            logger.warning(f"Assigning to {pod.id}")
            await message.channel.basic_publish(
                assignation.to_message(), routing_key=pod.channel, # Lets take the first best one
                properties=aiormq.spec.Basic.Properties(
                    correlation_id=assignation.data.reference,
                    reply_to=self.assignation_done.queue
                )
            )

            await update_assignation_with_pod(assignation, pod)

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)

    @AssignationMessage.unwrapped_message
    async def on_assignation_done(self, assignation: AssignationMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Assignation Done {str(message.body.decode())}")

        # We are routing it to Pod One / This Pod will then reply to
        await message.channel.basic_publish(
            message.body, routing_key=assignation.data.callback,
            properties=aiormq.spec.Basic.Properties(
                correlation_id=assignation.data.reference,
            )
        )

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)


    @AssignationMessage.unwrapped_message
    async def on_assignation_error(self, assignation: AssignationMessage, message: aiormq.types.DeliveredMessage):
        logger.error(f"Assignation Error {str(message.body.decode())}")

        # We are routing it to Pod One / This Pod will then reply to
        await message.channel.basic_publish(
            message.body, routing_key=assignation.meta.extensions.callback,
            properties=aiormq.spec.Basic.Properties(
                correlation_id=assignation.data.reference,
            )
        )

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)


    @AssignationMessage.unwrapped_message
    async def on_assignation_yield(self, assignation: AssignationMessage, message: aiormq.types.DeliveredMessage):
        logger.info(f"Assignation Yielded {str(message.body.decode())}")


        if assignation.data.callback == "gateway_yield":
            pass


        # We are routing it to Pod One / This Pod will then reply to
        await message.channel.basic_publish(
            message.body, routing_key=assignation.data.callback,
            properties=aiormq.spec.Basic.Properties(
                correlation_id=assignation.data.reference,
            )
        )

        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)


    @AssignationMessage.unwrapped_message
    async def on_assignation_progress(self, assignation: AssignationMessage, message: aiormq.types.DeliveredMessage):
        logger.error(f"Assignation Progress {str(message.body.decode())}")


        if assignation.meta.extensions.progress == "gateway_progress":
            pass


        # We are routing it to Pod One / This Pod will then reply to
        await message.channel.basic_publish(
            message.body, routing_key=assignation.meta.extensions.progress,
            properties=aiormq.spec.Basic.Properties(
                correlation_id=assignation.data.reference,
            )
        )
        # This should then expand this to an assignation message that can be delivered to the Providers
        await message.channel.basic_ack(message.delivery.delivery_tag)



    @AssignationRequestMessage.unwrapped_message
    async def on_assignation_request_in(self, assignation_request: AssignationRequestMessage, message: aiormq.types.DeliveredMessage):
        logger.error(f"AssignationRequest for Node {assignation_request.data.node} received")
        
        assignation, created = await get_or_create_assignation_from_request(assignation_request)
        extensions = {
            "progress" : assignation_request.meta.extensions.progress,
            "callback" : assignation_request.meta.extensions.callback
        }


        assignation_message = await sync_to_async(AssignationMessage.fromAssignation)(assignation, **extensions)

        # We have created an assignation and are passing this to the proper authorities

        if assignation_request.data.action == AssignationAction.CANCEL:
            assert created is False, "Well that is weird, we just created that one?"
            logger.warning(f"Cancelation for Node {assignation_message.data.node} forwarded")
            await message.channel.basic_publish(
                assignation_message.to_message(), routing_key="assignation_cancel",
                properties=aiormq.spec.Basic.Properties(
                    correlation_id=assignation.reference, # TODO: Check if we shouldnt use message.header.properties.correlation_id
                    reply_to=assignation.callback,
                )
            )
        
        else:
            logger.info(f"Assignation for Node {assignation_message.data.node} forwarded")
            await message.channel.basic_publish(
                assignation_message.to_message(), routing_key="assignation_in",
                properties=aiormq.spec.Basic.Properties(
                    correlation_id=assignation.reference, # TODO: Check if we shouldnt use message.header.properties.correlation_id
                    reply_to=assignation.callback,
                )
            )
            
        await message.channel.basic_ack(message.delivery.delivery_tag)



