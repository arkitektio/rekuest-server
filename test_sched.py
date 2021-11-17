import os
import sys
from uuid import uuid4
import django
from delt.messages.generics import Context
from delt.messages import ReserveLogMessage
from delt.messages.postman.provide.bounced_provide import BouncedProvideMessage
from delt.messages.postman.reserve.bounced_reserve import BouncedReserveMessage
from delt.messages.postman.reserve.reserve_transition import (
    ReserveState,
    ReserveTransitionMessage,
)

from delt.types import ProvideTactic, ReserveParams, ReserveTactic, TemplateParams
from facade.enums import AgentStatus, LogLevel, ProvisionStatus
import logging


logger = logging.getLogger(__name__)


def main():
    from facade.models import Reservation, Template, Provision
    from django.db.models import Count
    from hare.scheduler.default import DefaultScheduler

    scheduler = DefaultScheduler()

    return scheduler._reserve_reservation(
        Reservation.objects.get(reference="2832745c-7409-4820-842f-fe178b904b63")
    )


if __name__ == "__main__":
    sys.path.insert(0, "/workspace")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "arkitekt.settings")
    django.setup()

    messages = main()
    for message in messages:
        logger.info(f"Created Message: {message}")
