from ....messages.generics import Token
from ....messages.types import BOUNCED_UNRESERVE
from ....messages.base import MessageDataModel, MessageMetaExtensionsModel, MessageMetaModel, MessageModel
from typing import List, Optional

class MetaExtensionsModel(MessageMetaExtensionsModel):
    # Set by postman consumer
    progress: Optional[str]
    callback: Optional[str]

class MetaModel(MessageMetaModel):
    type: str = BOUNCED_UNRESERVE
    extensions: Optional[MetaExtensionsModel]
    token: Token

class DataModel(MessageDataModel):
    reservation: str

class BouncedUnreserveMessage(MessageModel):
    data: DataModel
    meta: MetaModel