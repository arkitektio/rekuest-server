# JSON RPC Messages
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field
from datetime import datetime
import uuid
from facade.enums import AssignationStatus, ProvisionStatus, ProvisionMode
from enum import Enum


class AgentMessageTypes(str, Enum):

    ASSIGN_CHANGED = "ASSIGN_CHANGED"
    PROVIDE_CHANGED = "PROVIDE_CHANGED"

    LIST_ASSIGNATIONS = "LIST_ASSIGNATIONS"
    LIST_ASSIGNATIONS_REPLY = "LIST_ASSIGNATIONS_REPLY"
    LIST_ASSIGNATIONS_DENIED = "LIST_ASSIGNATIONS_DENIED"

    LIST_PROVISIONS = "LIST_PROVISIONS"
    LIST_PROVISIONS_REPLY = "LIST_PROVISIONS_REPLY"
    LIST_PROVISIONS_DENIED = "LIST_PROVISIONS_DENIED"


class AgentSubMessageTypes(str, Enum):

    ASSIGN = "ASSIGN"
    UNASSIGN = "UNASSIGN"
    PROVIDE = "PROVIDE"
    UNPROVIDE = "UNPROVIDE"


class JSONMeta(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class JSONMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    meta: JSONMeta = Field(default_factory=JSONMeta)
    pass


class AssignationFragment(BaseModel):
    assignation: str
    provision: str
    reservation: str
    args: List[Any]
    kwargs: Dict[str, Any]
    persist: bool = True
    log: bool = True


class ProvideFragment(BaseModel):
    provision: str
    template: str


class AssignationsList(JSONMessage):
    type: Literal[
        AgentMessageTypes.LIST_ASSIGNATIONS
    ] = AgentMessageTypes.LIST_ASSIGNATIONS
    exclude: Optional[List[AssignationStatus]]


class AssignationsListReply(JSONMessage):
    type: Literal[
        AgentMessageTypes.LIST_ASSIGNATIONS_REPLY
    ] = AgentMessageTypes.LIST_ASSIGNATIONS_REPLY
    assignations: List[AssignationFragment]


class AssignationsListDenied(JSONMessage):
    type: Literal[
        AgentMessageTypes.LIST_ASSIGNATIONS_DENIED
    ] = AgentMessageTypes.LIST_ASSIGNATIONS_DENIED
    error: str


class ProvisionList(JSONMessage):
    type: Literal[AgentMessageTypes.LIST_PROVISIONS] = AgentMessageTypes.LIST_PROVISIONS
    exclude: Optional[List[AssignationStatus]]


class ProvisionListReply(JSONMessage):
    type: Literal[
        AgentMessageTypes.LIST_PROVISIONS_REPLY
    ] = AgentMessageTypes.LIST_PROVISIONS_REPLY
    provisions: List[ProvideFragment]


class ProvisionListDenied(JSONMessage):
    type: Literal[
        AgentMessageTypes.LIST_PROVISIONS_DENIED
    ] = AgentMessageTypes.LIST_PROVISIONS_DENIED
    error: str


class ProvisionChangedMessage(JSONMessage):
    type: Literal[AgentMessageTypes.PROVIDE_CHANGED] = AgentMessageTypes.PROVIDE_CHANGED
    provision: str
    status: ProvisionStatus
    message: Optional[str]
    mode: Optional[ProvisionMode]


class AssignSubMessage(JSONMessage, AssignationFragment):
    type: Literal[AgentSubMessageTypes.ASSIGN] = AgentSubMessageTypes.ASSIGN


class ProvideSubMessage(JSONMessage, ProvideFragment):
    type: Literal[AgentSubMessageTypes.PROVIDE] = AgentSubMessageTypes.PROVIDE


class UnassignSubMessage(JSONMessage):
    type: Literal[AgentSubMessageTypes.UNASSIGN] = AgentSubMessageTypes.UNASSIGN
    assignation: str


class UnprovideSubMessage(JSONMessage):
    type: Literal[AgentSubMessageTypes.UNPROVIDE] = AgentSubMessageTypes.UNPROVIDE
    provision: str


class AssignChangedMessage(JSONMessage):
    type: Literal[AgentMessageTypes.ASSIGN_CHANGED] = AgentMessageTypes.ASSIGN_CHANGED
    assignation: str
    status: AssignationStatus
    result: List[Any]
