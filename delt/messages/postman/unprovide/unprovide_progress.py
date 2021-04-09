from ..progress import ProgressDataModel
from ....messages.types import  PROVIDE, PROVIDE_DONE, PROVIDE_PROGRESS, UNPROVIDE_PROGRESS
from ....messages.base import MessageDataModel, MessageMetaExtensionsModel, MessageMetaModel, MessageModel
from typing import List, Optional


class MetaExtensionsModel(MessageMetaExtensionsModel):
    # Set by postman consumer
    progress: Optional[str]
    callback: Optional[str]

class MetaModel(MessageMetaModel):
    type: str = UNPROVIDE_PROGRESS
    extensions: Optional[MetaExtensionsModel]


class UnprovideProgressMessage(MessageModel):
    data: ProgressDataModel
    meta: MetaModel