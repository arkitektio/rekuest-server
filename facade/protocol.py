from datetime import datetime
from typing import List
from uuid import UUID
import uuid
from pydantic import BaseModel, Field


class JSONMeta(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class JSONMessage(BaseModel):
    id: UUID = Field(default_factory=uuid.uuid4)
    type: str
    meta: JSONMeta = Field(default_factory=JSONMeta)


class ReserveList(JSONMessage):
    reservations: List[str]
