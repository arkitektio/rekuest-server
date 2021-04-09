from ..progress import ProgressDataModel
from pydantic.main import BaseModel
from ....messages.types import  ASSIGN_PROGRES, PROVIDE, PROVIDE_DONE, UNASSIGN_PROGRES
from ....messages.base import MessageDataModel, MessageMetaExtensionsModel, MessageMetaModel, MessageModel
from typing import List, Optional


class MetaExtensionsModel(MessageMetaExtensionsModel):
    # Set by postman consumer
    progress: Optional[str]
    callback: Optional[str]

class MetaModel(MessageMetaModel):
    type: str = UNASSIGN_PROGRES
    extensions: Optional[MetaExtensionsModel]


class UnassignProgressMessage(MessageModel):
    data: ProgressDataModel
    meta: MetaModel