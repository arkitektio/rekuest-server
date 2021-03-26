from pydantic.main import BaseModel
from ....messages.exception import ExceptionMessage
from ....messages.types import  PROVIDE, PROVIDE_CRITICAL, PROVIDE_DONE, RESERVE_CRITICAL
from ....messages.base import MessageDataModel, MessageMetaExtensionsModel, MessageMetaModel, MessageModel
from typing import List, Optional


class MetaExtensionsModel(MessageMetaExtensionsModel):
    # Set by postman consumer
    progress: Optional[str]
    callback: Optional[str]

class MetaModel(MessageMetaModel):
    type: str = RESERVE_CRITICAL
    extensions: Optional[MetaExtensionsModel]

class ReserveCriticalMessage(ExceptionMessage):
    meta: MetaModel