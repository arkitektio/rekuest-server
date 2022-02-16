# JSON RPC Messages
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field
from datetime import datetime
import uuid
from facade.enums import AssignationStatus, ReservationStatus


class JSONMeta(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class JSONMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    meta: JSONMeta = Field(default_factory=JSONMeta)
    pass


class ReserveFragment(BaseModel):
    id: str
    status: ReservationStatus


class ReserveList(JSONMessage):
    type: Literal["RESERVE_LIST"] = "RESERVE_LIST"
    exclude: Optional[List[ReservationStatus]]


class ReserveListReply(JSONMessage):
    type: Literal["RESERVE_LIST_REPLY"] = "RESERVE_LIST_REPLY"
    reservations: Optional[List[ReserveFragment]]


class ReserveListDenied(JSONMessage):
    type: Literal["RESERVE_LIST_DENIED"] = "RESERVE_LIST_DENIED"
    error: str


class ReservePub(JSONMessage):
    type: Literal["RESERVE"] = "RESERVE"
    node: Optional[str]
    template: Optional[str]
    title: Optional[str]
    params: Dict[str, Any] = {}


class ReservePubReply(JSONMessage):
    type: Literal["RESERVE_REPLY"] = "RESERVE_REPLY"
    reservation: str
    status: ReservationStatus


class ReservePubDenied(JSONMessage):
    type: Literal["RESERVE_DENIED"] = "RESERVE_DENIED"
    error: str


class UnreservePub(JSONMessage):
    type: Literal["UNRESERVE"] = "UNRESERVE"
    reservation: str


class UnreservePubReply(JSONMessage):
    type: Literal["UNRESERVE_REPLY"] = "UNRESERVE_REPLY"
    reservation: str


class ReserveSubUpdate(JSONMessage):
    type: Literal["RESERVE_UPDATE"] = "RESERVE_UPDATE"
    reservation: str
    status: Optional[ReservationStatus]  # Status Update
    provisions: Optional[List[str]]  # Status plus Provision Update


class AssignPub(JSONMessage):
    type: Literal["ASSIGN"] = "ASSIGN"
    reservation: str
    args: List[Any]
    kwargs: Dict[str, Any]
    persist: bool = True
    log: bool = True


class AssignPubReply(JSONMessage):
    type: Literal["ASSIGN_REPLY"] = "ASSIGN_REPLY"
    assignation: str
    status: AssignationStatus


class AssignPubDenied(JSONMessage):
    type: Literal["ASSIGN_DENIED"] = "ASSIGN_DENIED"
    error: str


class UnassignPub(JSONMessage):
    type: Literal["UNASSIGN"] = "UNASSIGN"
    assignation: str


class UnassignPubReply(JSONMessage):
    type: Literal["UNASSIGN_REPLY"] = "UNASSIGN_REPLY"
    assignation: str


class AssignSubUpdate(JSONMessage):
    type: Literal["ASSIGN_UPDATE"] = "ASSIGN_UPDATE"
    ref: UUID
    status: Optional[AssignationStatus]
    bound: Optional[str]
    result: Optional[List[Any]]
    log: Optional[str]


class AssignSnapshot(JSONMessage):
    type: Literal["ASSIGN_UPDATE"] = "ASSIGN_SNAPSHOT"
    refs: List[UUID]
