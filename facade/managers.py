import re
from typing import Any, List, Optional, Tuple
from django.db.models.manager import Manager
from facade.enums import ProvisionStatus, ReservationStatus
from hare.carrots import (
    HareMessage,
    ProvideHareMessage,
    ReserveHareMessage,
    UnreserveHareMessage,
)
from hare.consumers.postman.protocols.postman_json import ReserveParams

qt = re.compile(r"@(?P<package>[^\/]*)\/(?P<interface>[^\/]*)")


class NodeManager(Manager):
    def get(self, q=None, **kwargs):
        """Takes an DataArray and the model arguments and returns the created Model

        Arguments:
            array {xr.DataArray} -- An xr.DataArray as a LarvikArray

        Returns:
            [models.Model] -- [The Model]
        """
        if q is not None:
            m = qt.match(q)
            if m:
                kwargs["package"] = m.group("package")
                kwargs["interface"] = m.group("interface")

        return super().get(**kwargs)


class ScheduleException(Exception):
    pass


class ReservationManager(Manager):
    def reschedule(self, id: str) -> Tuple[Any, List[HareMessage]]:
        from .models import Agent, Provision, Template

        res = super().get(id=id)

        params = ReserveParams(
            **res.params
        )  # TODO: Get default from settings or policy?

        forwards = (
            []
        )  # messages with bind information that need to be send to the agent

        template = Template.objects.filter(node=res.node).first()
        if not template:
            raise ScheduleException(f"Could not find templates for node {res.node}")

        for prov in res.provisions.all():
            # All provisions queues will receive a reserve request (even if they are not created?)
            t = UnreserveHareMessage(
                queue=prov.bound.queue, reservation=res.id, provision=prov.id
            )
            forwards.append(t)

        res.provisions.clear()

        provisions = []

        for prov in (
            Provision.objects.filter(template=template)
            .exclude(status__in=[ProvisionStatus.CANCELLED, ProvisionStatus.CRITICAL])
            .all()
        ):
            # All provisions queues will receive a reserve request (even if they are not created?)

            t = ReserveHareMessage(
                queue=prov.agent.queue, reservation=res.id, provision=prov.id
            )

            provisions.append(prov)
            forwards.append(t)

        # filter unnecessary messages

        while len(provisions) < (params.minimalInstances or 1):

            agent = Agent.objects.filter(registry=template.registry).first()
            if not agent:
                ScheduleException("No Agent found")

            prov = Provision.objects.create(
                template=template, agent=agent, reservation=res
            )
            prov.reservations.add(res)
            prov.save()

            t = ProvideHareMessage(
                queue=prov.agent.queue,
                provision=prov.id,
                template=template.id,
                status=prov.status,
                reservation=res.id,
            )

            forwards.append(t)
            provisions.append(prov)

        res.provisions.add(*provisions)
        res.status = ReservationStatus.REROUTING

        res.save()

        print(res.status)

        return res, forwards

    def schedule(
        self,
        params: Optional[ReserveParams] = None,
        node: Optional[str] = None,
        template: Optional[str] = None,
        title: Optional[str] = None,
        waiter=None,
    ) -> Tuple[Any, List[HareMessage]]:
        """_summary_

        Args:
            params (Optional[dict]): _description_
            template (Optional[str]): _description_
            title (Optional[str]): _description_

        Returns:
            Tuple[Any, List[HareMessage]]: _description_
        """
        from .models import Provision, Template, Agent, Node

        params: ReserveParams = (
            params or ReserveParams()
        )  # TODO: Get default from settings or policy?

        forwards = (
            []
        )  # messages with bind information that need to be send to the agent

        t = Template.objects.filter(node_id=node).first()
        if not t:
            node = Node.objects.get(id=node)
            raise ScheduleException(f"Could not find templates for node {node}")

        res = super().create(
            node_id=node, template_id=template, waiter=waiter, params=params.dict()
        )

        provisions = []

        for prov in Provision.objects.filter(template=t).all():
            # All provisions queues will receive a reserve request (even if they are not created?)

            t = ReserveHareMessage(
                queue=prov.agent.queue, reservation=res.id, provision=prov.id
            )

            provisions.append(prov)
            forwards.append(t)

        while len(provisions) < (params.minimalInstances or 1):

            agent = Agent.objects.filter(registry=t.registry).first()
            if not agent:
                ScheduleException("No Agent found")

            prov = Provision.objects.create(template=t, agent=agent, reservation=res)
            prov.reservations.add(res)
            prov.save()

            t = ProvideHareMessage(
                queue=prov.agent.queue,
                provision=prov.id,
                template=t.id,
                status=prov.status,
                reservation=res.id,
            )

            forwards.append(t)
            provisions.append(prov)

        res.provisions.add(*provisions)

        res.save()

        return res, forwards

    def unschedule(id: str):

        # TODO: Refactor unscehduling to here
        pass
