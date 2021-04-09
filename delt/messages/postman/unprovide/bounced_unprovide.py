from ....messages.generics import Token
from pydantic.main import BaseModel
from ....messages.types import  BOUNCED_UNPROVIDE
from ....messages.base import MessageDataModel, MessageMetaExtensionsModel, MessageMetaModel, MessageModel
from typing import List, Optional



class MetaExtensionsModel(MessageMetaExtensionsModel):
    # Set by postman consumer
    progress: Optional[str]
    callback: Optional[str]

class MetaModel(MessageMetaModel):
    type: str = BOUNCED_UNPROVIDE
    extensions: Optional[MetaExtensionsModel]
    token: Token

class DataModel(MessageDataModel):
    provision: str


class BouncedUnprovideMessage(MessageModel):
    data: DataModel
    meta: MetaModel