from dataclasses import dataclass
from typing import List, Literal, Optional
import aiormq
from pydantic import BaseModel
from facade.enums import ReservationStatus
from hare.consumers.connection import rmq
import asyncio
import os
from asgiref.sync import sync_to_async
import ujson

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "arkitekt.settings")

import django

django.setup()


class HareReserveTransitionBody(BaseModel):
    type: Literal["RESERVE_UPDATE"] = "RESERVE_UPDATE"
    reservation: str
    status: Optional[ReservationStatus]
    provisions: Optional[List[str]]


@sync_to_async
def get_queue():

    from facade.models import Reservation
    from facade.enums import ReservationStatus

    res = Reservation.objects.get(id=14)
    res.status = ReservationStatus.ACTIVE
    res.save()
    queue = res.waiter.queue
    return queue


async def main():
    channel = await rmq.open_channel()

    queue = await get_queue()
    await channel.basic_publish(
        HareReserveTransitionBody(
            reservation="14", status=ReservationStatus.CANCELING, provisions=["3"]
        )
        .json()
        .encode(),
        routing_key=queue,
        properties=aiormq.spec.Basic.Properties(
            delivery_mode=1,
        ),
    )

    print(" [x] Sent %r" % "nana")


asyncio.run(main())
