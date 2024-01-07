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
from .inputs import PortDemandInput
from django.db import connection

qt = re.compile(r"@(?P<package>[^\/]*)\/(?P<interface>[^\/]*)")


def build_child_recursively(item, prefix, value_path, parts, params):
    if "key" in item:
        parts.append(f"{prefix}->>'key' = %({value_path}_key)s")
        params[f"{value_path}_key"] = item["key"]

    if "kind" in item:
        parts.append(f"{prefix}->>'kind' = %({value_path}_kind)s")
        params[f"{value_path}_kind"] = item["kind"]

    if "identifier" in item:
        parts.append(f"{prefix}->>'identifier' = %({value_path}_identifier)s")
        params[f"{value_path}_identifier"] = item["identifier"]

    if "child" in item:
        build_child_recursively(
            item["child"], prefix + "->'child'", f"{value_path}_child", parts, params
        )


def build_sql_for_item_recursive(item, at_value=None):
    sql_parts = []
    params = {}

    if at_value is not None:
        sql_parts.append(f"idx = %(at_{at_value})s")
        params[f"at_{at_value}"] = at_value + 1

    if "key" in item:
        sql_parts.append(f"item->>'key' = %(key_{at_value})s")
        params[f"key_{at_value}"] = item["key"]

    if "kind" in item:
        sql_parts.append(f"item->>'kind' = %(kind_{at_value})s")
        params[f"kind_{at_value}"] = item["kind"]

    if "identifier" in item:
        sql_parts.append(f"item->>'identifier' = %(identifier_{at_value})s")
        params[f"identifier_{at_value}"] = item["identifier"]

    if "child" in item:
        # Adjusting the prefix for recursion
        child_parts = []
        child_params = {}
        build_child_recursively(
            item["child"],
            "item->'child'",
            f"child_{at_value}",
            child_parts,
            child_params,
        )
        sql_parts += child_parts
        params.update(child_params)

    return (" AND ".join(sql_parts), params)


def build_params(search_params):
    individual_queries = []
    all_params = {}

    for item in search_params:
        sql_part, params = build_sql_for_item_recursive(item, at_value=item["at"])
        subquery = f"EXISTS (SELECT 1 FROM jsonb_array_elements(args) WITH ORDINALITY AS j(item, idx) WHERE {sql_part})"

        individual_queries.append(subquery)
        all_params.update(params)

    full_sql = "SELECT id FROM facade_node WHERE " + " AND ".join(individual_queries)
    return full_sql, all_params


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

    def matching_demands(
        self,
        input_demands: List[PortDemandInput] = None,
        output_demands: List[PortDemandInput] = None,
    ):
        """Get the nodes that match the demans

        Args:
            demands (List[PortDemandInput]): _description_

        Returns:
            [type]: _description_
        """

        if input_demands:
            full_sql, all_params = build_params(input_demands)

            with connection.cursor() as cursor:
                cursor.execute(full_sql, all_params)
                rows = cursor.fetchall()
                ids = [row[0] for row in rows]

            qs = self.filter(id__in=ids)
            return qs
        if output_demands:
            raise NotImplementedError("Output demands not implemented yet")

        return self.all()


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
