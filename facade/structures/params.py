from enum import Enum, auto
from typing import List, Optional
from pydantic import BaseModel


class TemplateParams(BaseModel):
    maximumInstances: Optional[int] = 1
    maximumInstancesPerAgent: Optional[int] = 1


class ReserveTactic(str, Enum):
    ALL = "ALL"
    FILTER_OWN = "FILTER_OWN"
    FILTER_ACTIVE = "FILTER_ACTIVE"
    FILTER_AGENTS = "FILTER_AGENTS"
    FILTER_TEMPLATES = "FILTER_TEMPLATES"
    BALANCE = "BALANCE"


class ProvideTactic(str, Enum):
    ALL = "ALL"
    FILTER_OWN = "FILTER_OWN"
    FILTER_ACTIVE_AGENTS = "FILTER_ACTIVE_AGENTS"
    FILTER_AGENTS = "FILTER_AGENTS"
    FILTER_TEMPLATES = "FILTER_TEMPLATES"
    BALANCE = "BALANCE"


class ReserveParams(BaseModel):
    desiredInstances: int = 1
    minimalInstances: int = 1
    registries: Optional[List[str]]
    agents: Optional[List[str]]
    templates: Optional[List[str]]
    autoUnprovide: Optional[bool] = True
    autoProvide: Optional[bool] = True