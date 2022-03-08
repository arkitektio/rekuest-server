# JSON RPC Messages
import json
from typing import Any, Dict, List, Literal, Optional, Type, TypeVar
from uuid import UUID
from hare.messages import Reservation, Assignation
from datetime import datetime
import uuid
from enum import Enum
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

class HareMessageTypes(str, Enum):
    ROUTE = "ROUTE"
    UNROUTE = "UNROUTE"

    CONNECT = "CONNECT"
    UNCONNECT = "UNCONNECT"

    ASSIGN = "ASSIGN"
    UNASSIGN = "UNASSIGN"

    RESERVE_CHANGED = "RESERVE_CHANGED"
    ASSIGN_CHANGED = "ASSIGN_CHANGED"


class HareMessage(BaseModel):
    queue: str
    pass

    def to_message(self) -> bytes:
        return json.dumps(self.dict()).encode()

    @classmethod
    def from_message(cls: Type[T], message) -> T:
        return cls(**json.loads(message.body.decode()))


class RouteHareMessage(HareMessage):
    queue = "route"
    type: Literal[HareMessageTypes.ROUTE] = HareMessageTypes.ROUTE
    reservation: str

class RouteHareMessage(HareMessage):
    queue = "route"
    type: Literal[HareMessageTypes.ROUTE] = HareMessageTypes.ROUTE
    reservation: str


class UnrouteHareMessage(HareMessage):
    queue = "unroute"
    type: Literal[HareMessageTypes.UNROUTE] = HareMessageTypes.UNROUTE
    reservation: str


class ConnectHareMessage(HareMessage):
    type: Literal[HareMessageTypes.CONNECT] = HareMessageTypes.CONNECT
    reservation: str
    provision: str


class ConnectHareMessage(HareMessage):
    type: Literal[HareMessageTypes.UNCONNECT] = HareMessageTypes.UNCONNECT
    reservation: str
    provision: str



class AssignHareMessage(HareMessage):
    type: Literal[HareMessageTypes.ASSIGN] = HareMessageTypes.ASSIGN
    assignation: str
    reservation: str
    args: List[Any]
    kwargs: Dict[str, Any]
    log: bool = True


class UnassignHareMessage(HareMessage):
    type: Literal[HareMessageTypes.UNASSIGN] = HareMessageTypes.UNASSIGN
    reservation: str


class ReservationChangedMessage(HareMessage, Reservation):
    type: Literal[HareMessageTypes.RESERVE_CHANGED] = HareMessageTypes.RESERVE_CHANGED
    pass


class AssignationChangedHareMessage(HareMessage, Assignation):
    type: Literal[HareMessageTypes.ASSIGN_CHANGED] = HareMessageTypes.ASSIGN_CHANGED
    pass