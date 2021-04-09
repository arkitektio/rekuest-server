from .params import ReserveParams
from ....messages.generics import Token
from ....messages.types import  BOUNCED_RESERVE
from ....messages.base import MessageDataModel, MessageMetaExtensionsModel, MessageMetaModel, MessageModel
from typing import List, Optional

class MetaExtensionsModel(MessageMetaExtensionsModel):
    # Set by postman consumer
    progress: Optional[str]
    callback: Optional[str]

class MetaModel(MessageMetaModel):
    type: str = BOUNCED_RESERVE
    extensions: Optional[MetaExtensionsModel]
    token: Token

class DataModel(MessageDataModel):
    node: Optional[str] #TODO: Maybe not optional
    template: Optional[str]
    params: Optional[ReserveParams]


class BouncedReserveMessage(MessageModel):
    data: DataModel
    meta: MetaModel