from ....messages.generics import Token
from ....messages.types import BOUNCED_CANCEL_RESERVE
from ....messages.base import MessageDataModel, MessageMetaExtensionsModel, MessageMetaModel, MessageModel
from typing import Optional


class MetaExtensionsModel(MessageMetaExtensionsModel):
    # Set by postman consumer
    progress: Optional[str]
    callback: Optional[str]

class MetaModel(MessageMetaModel):
    type: str = BOUNCED_CANCEL_RESERVE
    extensions: MetaExtensionsModel
    token: Token

class DataModel(MessageDataModel):
    reference: str


class BouncedCancelReserveMessage(MessageModel):
    data: DataModel
    meta: MetaModel