# JSON RPC Messages
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field
from datetime import datetime
import uuid
from facade.enums import AssignationStatus, ReservationStatus
from enum import Enum


class RPCMessageTypes(str, Enum):

    RESERVE_LIST = "RESERVE_LIST"
    RESERVE_LIST_REPLY = "RESERVE_LIST_REPLY"
    RESERVE_LIST_DENIED = "RESERVE_LIST_DENIED"

    RESERVE = "RESERVE"
    RESERVE_REPLY = "RESERVE_REPLY"
    RESERVE_DENIED = "RESERVE_DENIED"

    UNRESERVE = "UNRESERVE"
    UNRESERVE_REPLY = "UNRESERVE_REPLY"
    UNRESERVE_DENIED = "UNRESERVE_DENIED"

    ASSIGN_LIST = "ASSIGN_LIST"
    ASSIGN_LIST_REPLY = "ASSIGN_LIST_REPLY"
    ASSIGN_LIST_DENIED = "ASSIGN_LIST_DENIED"

    ASSIGN = "ASSIGN"
    ASSIGN_REPLY = "ASSIGN_REPLY"
    ASSIGN_DENIED = "ASSIGN_DENIED"

    UNASSIGN = "UNASSIGN"
    UNASSIGN_REPLY = "UNASSIGN_REPLY"
    UNASSIGN_DENIED = "UNASSIGN_DENIED"


class SubMessageTypes(str, Enum):

    ASSIGN_UPDATE = "ASSIGN_UPDATE"
    RESERVE_UPDATE = "RESERVE_UPDATE"


class JSONMeta(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class JSONMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    meta: JSONMeta = Field(default_factory=JSONMeta)
    pass


class ReserveFragment(BaseModel):
    reservation: str
    status: Optional[ReservationStatus]  # Status Update
    provisions: Optional[List[str]]  # Status plus Provision Update


class ReserveList(JSONMessage):
    type: Literal[RPCMessageTypes.RESERVE_LIST] = RPCMessageTypes.RESERVE_LIST
    exclude: Optional[List[ReservationStatus]]


class ReserveListReply(JSONMessage):
    type: Literal[
        RPCMessageTypes.RESERVE_LIST_REPLY
    ] = RPCMessageTypes.RESERVE_LIST_REPLY
    reservations: List[ReserveFragment]


class ReserveListDenied(JSONMessage):
    type: Literal[
        RPCMessageTypes.RESERVE_LIST_DENIED
    ] = RPCMessageTypes.RESERVE_LIST_DENIED
    error: str


class ReservePub(JSONMessage):
    type: Literal[RPCMessageTypes.RESERVE] = RPCMessageTypes.RESERVE
    node: Optional[str]
    template: Optional[str]
    title: Optional[str]
    params: Dict[str, Any] = {}


class ReservePubReply(JSONMessage):
    type: Literal[RPCMessageTypes.RESERVE_REPLY] = RPCMessageTypes.RESERVE_REPLY
    reservation: str
    status: ReservationStatus


class ReservePubDenied(JSONMessage):
    type: Literal[RPCMessageTypes.RESERVE_DENIED] = RPCMessageTypes.RESERVE_DENIED
    error: str


class UnreservePub(JSONMessage):
    type: Literal[RPCMessageTypes.UNRESERVE] = RPCMessageTypes.UNRESERVE
    reservation: str


class UnreservePubReply(JSONMessage):
    type: Literal[RPCMessageTypes.UNRESERVE_REPLY] = RPCMessageTypes.UNRESERVE_REPLY
    reservation: str


class UnreservePubDenied(JSONMessage):
    type: Literal[RPCMessageTypes.UNRESERVE_DENIED] = RPCMessageTypes.UNRESERVE_DENIED
    error: str


class ReserveSubUpdate(JSONMessage, ReserveFragment):
    type: Literal[SubMessageTypes.RESERVE_UPDATE] = SubMessageTypes.RESERVE_UPDATE


class AssignFragment(BaseModel):
    assignation: str
    status: Optional[AssignationStatus]
    provision: Optional[str]
    returns: Optional[List[Any]]
    log: Optional[str]


class AssignList(JSONMessage):
    type: Literal[RPCMessageTypes.ASSIGN_LIST] = RPCMessageTypes.ASSIGN_LIST
    exclude: Optional[List[AssignationStatus]]


class AssingListReply(JSONMessage):
    type: Literal[RPCMessageTypes.ASSIGN_LIST_REPLY] = RPCMessageTypes.ASSIGN_LIST_REPLY
    assignations: List[AssignFragment]


class AssignListDenied(JSONMessage):
    type: Literal[
        RPCMessageTypes.ASSIGN_LIST_DENIED
    ] = RPCMessageTypes.ASSIGN_LIST_DENIED
    error: str


class AssignPub(JSONMessage):
    type: Literal[RPCMessageTypes.ASSIGN] = RPCMessageTypes.ASSIGN
    reservation: str
    args: List[Any]
    kwargs: Dict[str, Any]
    persist: bool = True
    log: bool = True


class AssignPubReply(JSONMessage):
    type: Literal[RPCMessageTypes.ASSIGN_REPLY] = RPCMessageTypes.ASSIGN_REPLY
    assignation: str
    status: AssignationStatus


class AssignPubDenied(JSONMessage):
    type: Literal[RPCMessageTypes.ASSIGN_DENIED] = RPCMessageTypes.ASSIGN_DENIED
    error: str


class UnassignPub(JSONMessage):
    type: Literal[RPCMessageTypes.UNASSIGN] = RPCMessageTypes.UNASSIGN
    assignation: str


class UnassignPubReply(JSONMessage):
    type: Literal[RPCMessageTypes.UNASSIGN_REPLY] = RPCMessageTypes.UNASSIGN_REPLY
    assignation: str


class UnassignPubDenied(JSONMessage):
    type: Literal[RPCMessageTypes.UNASSIGN_DENIED] = RPCMessageTypes.UNASSIGN_DENIED
    assignation: str


class AssignSubUpdate(JSONMessage, AssignFragment):
    type: Literal[SubMessageTypes.ASSIGN_UPDATE] = SubMessageTypes.ASSIGN_UPDATE
