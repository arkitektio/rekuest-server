from typing import List
from pydantic.main import BaseModel
from delt.messages import BouncedReserveMessage
from dataclasses import dataclass

from delt.messages.postman.unreserve.bounced_unreserve import BouncedUnreserveMessage


class SchedulerError(Exception):
    pass


@dataclass
class MessageEvent:
    channel: str
    message: BaseModel


class BaseScheduler:
    def on_reserve(reserve: BouncedReserveMessage) -> List[MessageEvent]:
        raise NotImplementedError("Please Overwrite")

    def on_unreserve(reserve: BouncedUnreserveMessage) -> List[MessageEvent]:
        raise NotImplementedError("Please Overwrite")
