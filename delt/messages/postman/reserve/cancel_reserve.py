from ....messages.types import CANCEL_RESERVE
from ....messages.base import MessageDataModel, MessageMetaExtensionsModel, MessageMetaModel, MessageModel
from typing import Optional


class CancelProvideMetaModel(MessageMetaModel):
    type: str = CANCEL_RESERVE

class CancelProvideDataModel(MessageDataModel):
    reference: str


class CancelReserveMessage(MessageModel):
    data: CancelProvideDataModel
    meta: CancelProvideMetaModel