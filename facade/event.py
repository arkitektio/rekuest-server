from dataclasses import dataclass
from pydantic import BaseModel


@dataclass
class MessageEvent:
    channel: str
    message: BaseModel
