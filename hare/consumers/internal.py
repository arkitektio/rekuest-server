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
