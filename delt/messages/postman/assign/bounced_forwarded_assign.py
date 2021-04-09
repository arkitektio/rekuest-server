from ....messages.generics import Token
from pydantic.main import BaseModel
from ....messages.types import BOUNCED_FORWARDED_ASSIGN
from ....messages.base import MessageDataModel, MessageMetaExtensionsModel, MessageMetaModel, MessageModel
from typing import List, Optional



class AssignParams(BaseModel):
    providers: Optional[List[str]]

class MetaExtensionsModel(MessageMetaExtensionsModel):
    # Set by postman consumer
    progress: Optional[str]
    callback: Optional[str]

class MetaModel(MessageMetaModel):
    type: str = BOUNCED_FORWARDED_ASSIGN
    extensions: Optional[MetaExtensionsModel]
    token: Token

class DataModel(MessageDataModel):
    provision: str # The reservation reference we are going to assign to
    args: dict
    kwargs: Optional[dict]

class BouncedForwardedAssignMessage(MessageModel):
    data: DataModel
    meta: MetaModel