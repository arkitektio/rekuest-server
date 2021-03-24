from .exception import ExceptionMessage
from .host import DeActivatePodMessage, ActivatePodMessage
from .postman.assign import AssignMessage, AssignReturnMessage, AssignProgressMessage, AssignCriticalMessage, AssignYieldsMessage, BouncedAssignMessage, BouncedCancelAssignMessage
from .base import MessageModel
from .types import *
from .postman.provide import ProvideDoneMessage, ProvideCriticalMessage, ProvideMessage, ProvideProgressMessage, BouncedProvideMessage, BouncedCancelProvideMessage
from .postman.reserve import *
import json


registry = {
    PROVIDE_DONE:  ProvideDoneMessage,
    BOUNCED_PROVIDE: BouncedProvideMessage,
    BOUNCED_CANCEL_PROVIDE: BouncedCancelProvideMessage,
    PROVIDE_CRITICAL: ProvideCriticalMessage,
    PROVIDE_PROGRESS: ProvideProgressMessage,
    PROVIDE: ProvideMessage,


    RESERVE: ReserveMessage,
    UNRESERVE: UnReserveMessage,
    BOUNCED_RESERVE: BouncedReserveMessage,
    BOUNCED_CANCEL_RESERVE: BouncedCancelReserveMessage,
    RESERVE_CRITICAL: ReserveCriticalMessage,
    RESERVE_PROGRESS: ReserveProgressMessage,
    RESERVE_DONE: ReserveDoneMessage,


    ASSIGN: AssignMessage,
    ASSIGN_CRITICAL: AssignCriticalMessage,
    ASSIGN_PROGRES: AssignProgressMessage,
    ASSIGN_RETURN: AssignReturnMessage,
    ASSIGN_YIELD: AssignYieldsMessage,
    BOUNCED_ASSIGN: BouncedAssignMessage,
    BOUNCED_CANCEL_ASSIGN: BouncedCancelAssignMessage,

    ACTIVATE_POD: ActivatePodMessage,
    DEACTIVATE_POD: DeActivatePodMessage,

    EXCEPTION: ExceptionMessage
}


class MessageError(Exception):
    pass


def expandToMessage(message: dict) -> MessageModel:
    assert isinstance(message, dict), "Please provide already serialized Messages"
    try:
        cls: MessageModel = registry[message["meta"]["type"]]
    except:
        raise MessageError(f"Didn't find an expander for message {message} {message['meta']['type']}")

    return cls(**message)
    

def expandFromRabbitMessage(message) -> MessageModel:
    text = message.body.decode()
    return expandToMessage(json.loads(text))