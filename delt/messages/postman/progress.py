

from enum import Enum
from ..base import MessageDataModel


class ProgressLevel(str, Enum):
    INFO = "INFO"
    DEBUG = "DEBUG"


class ProgressDataModel(MessageDataModel):
    level: ProgressLevel
    message: str