from .host import DeActivatePodMessage, ActivatePodMessage
from .postman.assign import *
from .base import MessageModel
from .types import *
from .postman.provide import *
from .postman.unprovide import *
from .postman.reserve import *
from .postman.unreserve import *
from .postman.unassign import *
from .exception import ExceptionMessage
import json


registry = {
    PROVIDE_DONE:  ProvideDoneMessage,
    BOUNCED_PROVIDE: BouncedProvideMessage,
    PROVIDE_CRITICAL: ProvideCriticalMessage,
    PROVIDE_PROGRESS: ProvideProgressMessage,
    PROVIDE: ProvideMessage,

    UNPROVIDE_DONE:  UnprovideDoneMessage,
    BOUNCED_UNPROVIDE: BouncedUnprovideMessage,
    UNPROVIDE_CRITICAL: UnprovideCriticalMessage,
    UNPROVIDE_PROGRESS: UnprovideProgressMessage,
    UNPROVIDE: UnprovideMessage,

    RESERVE: ReserveMessage,
    BOUNCED_RESERVE: BouncedReserveMessage,
    BOUNCED_FORWARDED_RESERVE: BouncedForwardedReserveMessage,
    RESERVE_CRITICAL: ReserveCriticalMessage,
    RESERVE_PROGRESS: ReserveProgressMessage,
    RESERVE_DONE: ReserveDoneMessage,

    UNRESERVE: UnreserveMessage,
    BOUNCED_UNRESERVE: BouncedUnreserveMessage,
    UNRESERVE_CRITICAL: UnreserveCriticalMessage,
    UNRESERVE_PROGRESS: UnreserveProgressMessage,
    UNRESERVE_DONE: UnreserveDoneMessage,

    ASSIGN: AssignMessage,
    BOUNCED_ASSIGN: BouncedAssignMessage,
    BOUNCED_FORWARDED_ASSIGN: BouncedForwardedAssignMessage,
    ASSIGN_CRITICAL: AssignCriticalMessage,
    ASSIGN_PROGRES: AssignProgressMessage,
    ASSIGN_RETURN: AssignReturnMessage,
    ASSIGN_DONE: AssignDoneMessage,
    ASSIGN_YIELD: AssignYieldsMessage,

    UNASSIGN: UnassignMessage,
    BOUNCED_UNASSIGN: BouncedUnassignMessage,
    UNASSIGN_CRITICAL: UnassignCriticalMessage,
    UNASSIGN_PROGRES: UnassignProgressMessage,
    UNASSIGN_DONE: UnassignDoneMessage,

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
