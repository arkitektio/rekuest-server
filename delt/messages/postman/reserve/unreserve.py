from pydantic.main import BaseModel
from ....messages.types import  UNRESERVE
from ....messages.base import MessageDataModel, MessageMetaExtensionsModel, MessageMetaModel, MessageModel
from typing import List, Optional



class MessageMetaExtensionsModel(MessageMetaExtensionsModel):
    # Set by postman consumer
    progress: Optional[str]
    callback: Optional[str]

class MetaModel(MessageMetaModel):
    type: str = UNRESERVE
    extensions: Optional[MessageMetaExtensionsModel]

class DataModel(MessageDataModel):
    reservation: Optional[str] 

class UnReserveMessage(MessageModel):
    data: DataModel
    meta: MetaModel