from multiprocessing.managers import BaseManager
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
from guardian.shortcuts import get_objects_for_user

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
    def schedule(
        self,
        params: Optional[ReserveParams] = None,
        node: Optional[str] = None,
        template: Optional[str] = None,
        title: Optional[str] = None,
        waiter=None,
        provision: Optional[str] = None,
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

        params: ReserveParams = params or ReserveParams()

        forwards = []
        # messages with bind information that need to be send to the agent

        try:
            res = super().get(node_id=node, params=params.dict(), waiter=waiter)

        except self.model.DoesNotExist:

            res = super().create(
                node_id=node,
                template_id=template,
                waiter=waiter,
                params=params.dict(),
                provision=provision,
                title=title,
            )

            linked_provisions = []

            if node is not None:

                # templates: BaseManager = get_objects_for_user(
                #     waiter.registry.user, "facade.providable"
                # )
                #  TODO: Do we really need the providable permission?
                templates = Template.objects
                templates = templates.filter(node_id=node).all()

                # We are doing a round robin here, all templates
                for template in templates:
                    if len(linked_provisions) >= params.desiredInstances:
                        break

                    linkable_provisions = get_objects_for_user(
                        waiter.registry.user, "facade.can_link_to"
                    )

                    for prov in linkable_provisions.filter(template=template).all():
                        if len(linked_provisions) >= params.desiredInstances:
                            break

                        prov, linkforwards = prov.link(res)
                        linked_provisions.append(prov)
                        forwards += linkforwards

                if len(linked_provisions) < params.desiredInstances:

                    for template in templates:
                        if len(linked_provisions) >= params.desiredInstances:
                            break

                        linkable_agents = get_objects_for_user(
                            waiter.registry.user, "facade.can_provide_on"
                        )

                        available_agents = linkable_agents.filter(
                            registry=template.registry
                        ).all()

                        for agent in available_agents:
                            if len(linked_provisions) >= params.desiredInstances:
                                break

                            prov = Provision.objects.create(
                                template=template, agent=agent, reservation=res
                            )

                            prov, linkforwards = prov.link(res)
                            linked_provisions.append(prov)
                            forwards += linkforwards

            else:
                raise NotImplementedError(
                    "No node specified. Template reservation not implemented yet."
                )

            res.provisions.add(*linked_provisions)

            if len(res.provisions.all()) >= params.minimalInstances:
                res.viable = True

            if len(res.provisions.all()) >= params.desiredInstances:
                res.happy = True

            res.save()

        return res, forwards

    def unschedule(id: str):

        # TODO: Refactor unscehduling to here
        pass
