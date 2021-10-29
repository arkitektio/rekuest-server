from delt.events.base import Event
from facade.enums import ProvisionStatus


class ReserveEvent(Event):
    pass


class ProvisionTransitionEvent(ReserveEvent):
    type = "PROVISION_TRANSITION"
    provision: str
    state: ProvisionStatus = ProvisionStatus.ACTIVE
 






