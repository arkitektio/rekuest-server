from enum import Enum, auto
from typing import List, Optional
from pydantic import BaseModel


class TemplateParams(BaseModel):
    maximumInstances: Optional[int] = 1
    maximumInstancesPerAgent: Optional[int] = 1


class ReserveTactic(str, Enum):
    ALL = auto()
    FILTER_OWN = auto()
    FILTER_ACTIVE = auto()
    FILTER_AGENTS = auto()
    FILTER_TEMPLATES = auto()
    BALANCE = auto()


class ProvideTactic(str, Enum):
    ALL = auto()
    FILTER_OWN = auto()
    FILTER_ACTIVE_AGENTS = auto()
    FILTER_AGENTS = auto()
    FILTER_TEMPLATES = auto()
    BALANCE = auto()


class ReserveParams(BaseModel):
    desiredInstances: int = 1
    minimalInstances: int = 1
    registries: Optional[List[str]]
    agents: Optional[List[str]]
    templates: Optional[List[str]]
    autoUnprovide: Optional[bool] = True
    autoProvide: Optional[bool] = True
    reserveStrategy: List[ReserveTactic] = [
        ReserveTactic.ALL,
        ReserveTactic.FILTER_AGENTS,
        ReserveTactic.FILTER_OWN,
        ReserveTactic.FILTER_ACTIVE,
    ]
    provideStrategy: List[ProvideTactic] = [
        ProvideTactic.ALL,
        ProvideTactic.FILTER_OWN,
        ProvideTactic.FILTER_ACTIVE_AGENTS,
    ]


class ProvideParams(BaseModel):
    autoUnprovide: Optional[bool] = True
    autoProvide: Optional[bool] = True
