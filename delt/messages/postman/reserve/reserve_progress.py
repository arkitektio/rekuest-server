from ..progress import ProgressDataModel
from pydantic.main import BaseModel
from ....messages.types import  PROVIDE, PROVIDE_DONE, PROVIDE_PROGRESS, RESERVE_PROGRESS
from ....messages.base import MessageDataModel, MessageMetaExtensionsModel, MessageMetaModel, MessageModel
from typing import List, Optional


class MetaExtensionsModel(MessageMetaExtensionsModel):
    # Set by postman consumer
    progress: Optional[str]
    callback: Optional[str]

class MetaModel(MessageMetaModel):
    type: str = RESERVE_PROGRESS
    extensions: Optional[MetaExtensionsModel]


class ReserveProgressMessage(MessageModel):
    data: ProgressDataModel
    meta: MetaModel