
from hare.hares.utils import log_aiormq
from facade.consumers.postman import get_topic_for_bounced_assign
from facade.utils import create_assignation_from_bounced_assign, create_reservation_from_bounced_reserve, end_assignation, end_reservation, log_to_assignation, log_to_reservation, set_assignation_status, set_reservation_status, update_reservation
from delt.messages.postman.provide.params import ProvideParams
from delt.messages.postman.progress import ProgressLevel
from facade.enums import AssignationStatus, LogLevel, PodStatus, ProvisionStatus, ReservationStatus
from aiormq import channel
from delt.messages.exception import ExceptionMessage
from delt.messages import *
from typing import Tuple, Union
from facade.models import Assignation, Pod, Provision, Reservation, Template
import logging
import aiormq
from asgiref.sync import sync_to_async
from .base import BaseHare
from arkitekt.console import console
logger = logging.getLogger(__name__)



class PersisterRabbit(BaseHare):

    def __init__(self) -> None:
        pass

    async def connect(self):
        # Perform connection
        self.connection = await aiormq.connect(f"amqp://guest:guest@mister/")
        self.channel = await self.connection.channel()

        # This queue gets called from the HTTP backend (so GraphQL Postman request) with an already created Assignation
        self.persist_in = await self.channel.queue_declare("persist_in")


        # Start listening the queue with name 'hello'
        await self.channel.basic_consume(self.persist_in.queue, self.on_persist_in)


    @log_aiormq
    async def on_persist_in(self, message: aiormq.abc.DeliveredMessage):
        await message.channel.basic_ack(message.delivery.delivery_tag)



