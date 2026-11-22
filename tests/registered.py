"""Test fixtures: the rows a registration writes, built directly.

Registration is takt's (rekuest-takt ``registration::implement_agent``, parity-tested against
what this server used to do). Tests of the Python server's own features — matching, filters,
structure usages, dependency resolution, triggers — only need realistic rows to work on: an
Action with its relational port tree (compiled requires/provides), an Implementation on an
agent, its Dependencies and manipulated States. This builds exactly those, and nothing of what
registration decides (catalogs, diagnostics, ownership, reaping, protocols, test targets).
"""

from __future__ import annotations

import hashlib
import json
import typing as t

from authentikate.models import Client, Organization, User

from facade import models
from facade.descriptors import compile_descriptors_to_jsonpath
from facade.inputs.agent import ImplementAgentInputModel
from rekuest_core.inputs.models import (
    BlokImplementationInputModel,
    DefinitionInputModel,
    ImplementationInputModel,
    PortInputModel,
)


def _ports(
    port_datas: t.Sequence[PortInputModel],
    owner: dict[str, models.Action | models.Blok],
    port_model: type,
    descriptor_field: str,
) -> None:
    """The port tree as rows of ``owner`` (``{"action": ...}``, ``{"blok": ...}``), parents before
    children (``key_path`` dotted from the root)."""

    def build(
        port_data: PortInputModel, parent: object, index: int, parent_path: str
    ) -> tuple[PortInputModel, str, object]:
        path = f"{parent_path}.{port_data.key}" if parent_path else port_data.key
        row = port_model(
            **owner,
            parent=parent,
            index=index,
            key=port_data.key,
            key_path=path,
            kind=getattr(port_data.kind, "value", port_data.kind),
            identifier=port_data.identifier,
            compiled_jsonpath=compile_descriptors_to_jsonpath(
                getattr(port_data, descriptor_field, None) or []
            ),
            nullable=port_data.nullable,
            dimension=port_data.dimension,
        )
        return port_data, path, row

    level = [build(port, None, i, "") for i, port in enumerate(port_datas or [])]
    while level:
        port_model.objects.bulk_create([row for _, _, row in level])
        level = [
            build(child, row, i, path)
            for port, path, row in level
            for i, child in enumerate(port.children or [])
        ]


def rebuild_relational_ports(
    action: models.Action, definition: DefinitionInputModel
) -> None:
    """Replace the action's ArgPort/ReturnPort rows and root counts from its definition."""
    action.arg_ports.all().delete()
    action.return_ports.all().delete()
    _ports(definition.args, {"action": action}, models.ArgPort, "requires")
    _ports(definition.returns, {"action": action}, models.ReturnPort, "provides")
    action.arg_count = len(definition.args or [])
    action.return_count = len(definition.returns or [])
    action.save(update_fields=["arg_count", "return_count"])


# --------------------------------------------------------------------------- blok trees
# Every blok is one ``<Blok kind context>`` whose declarations (``<local>``, ``<query>``,
# ``<subscription>``) are its leading children. Ids are derived as the BSX parser derives them
# (``Blok[0]/local[0]``, ``Blok[0]/Card[0]``, ...); an explicit id is kept.

_PROP_KEYS = {
    "static_value": "staticValue",
    "dynamic_value": "dynamicValue",
    "util_call": "utilCall",
    "agent_call": "agentCall",
    "declares_value": "declaresValue",
}


def node(component: str, *children: dict, id: str = "", **props: t.Any) -> dict:
    """A component node, its props literals (a dict with ``path`` is a path)."""
    out: dict[str, t.Any] = {"id": id, "component": component}
    if props:
        out["props"] = [
            {"key": key, "dynamic_value": value}
            if isinstance(value, dict) and set(value) == {"path"}
            else {"key": key, "static_value": value}
            for key, value in props.items()
        ]
    if children:
        out["children"] = list(children)
    return out


def local(name: str, **fields: t.Any) -> dict:
    """``<local name=... />``: a port's fields as literals (``kind``, ``default``, ``persist``...)."""
    props = [
        {"key": "name", "static_value": name, "declares_value": name},
        *({"key": key, "static_value": value} for key, value in fields.items()),
    ]
    return {"id": "", "component": "local", "props": props}


def query(
    name: str,
    service: str,
    document: str,
    *variables: tuple[str, t.Any],
    component: str = "query",
    **fields: t.Any,
) -> dict:
    """``<query name service document>`` with a ``<var key value>`` per variable (a dict with ``path`` is a path)."""
    return node(
        component,
        *(node("var", key=key, value=value) for key, value in variables),
        name=name,
        service=service,
        document=document,
        **fields,
    )


def _ids(tree: dict, node_id: str) -> dict:
    out = {**tree, "id": node_id}
    counts: dict[str, int] = {}
    children = []
    for child in tree.get("children") or []:
        if child["component"] == "#text":
            index = counts.get("#", 0)
            counts["#"] = index + 1
            children.append({**child, "id": f"{node_id}/#{index}"})
            continue
        index = counts.get(child["component"], 0)
        counts[child["component"]] = index + 1
        children.append(
            _ids(child, child["id"] or f"{node_id}/{child['component']}[{index}]")
        )
    if children:
        out["children"] = children
    return out


def text(value: str) -> dict:
    """A run of text."""
    return {
        "id": "",
        "component": "#text",
        "props": [{"key": "value", "static_value": value}],
    }


def blok(kind: str, context: str, *children: dict, chrome: str | None = None) -> dict:
    """``<Blok kind context>`` holding ``children`` (declarations first), every id derived."""
    root = node(
        "Blok",
        *children,
        kind=kind,
        context=context,
        **({"chrome": chrome} if chrome else {}),
    )
    return _ids(root, "Blok[0]")


def gql(tree: dict) -> dict:
    """A tree built here as a GraphQL ComponentNodeInput (camelCase prop fields)."""
    out = dict(tree)
    if out.get("props"):
        out["props"] = [
            {_PROP_KEYS.get(key, key): value for key, value in prop.items()}
            for prop in out["props"]
        ]
    if out.get("children"):
        out["children"] = [gql(child) for child in out["children"]]
    return out


def create_blok(
    organization: Organization,
    creator: User,
    name: str,
    args: t.Sequence[dict] = (),
    **fields: t.Any,
) -> models.Blok:
    """A blok with its declared args stored and as BlokArgPort rows, as takt registers them."""
    declared = BlokImplementationInputModel(
        key=name,
        vocab=2,
        components=[blok("agent", "panel", node("Card"))],
        args=list(args),
    )
    catalog = models.Module.objects.get_or_create(
        name="orkestrator", organization=organization
    )[0]
    row = models.Blok.objects.create(
        name=name,
        organization=organization,
        creator=creator,
        module=catalog,
        args=declared.model_dump(mode="json")["args"],
        **fields,
    )
    _ports(declared.args, {"blok": row}, models.BlokArgPort, "requires")
    return row


def create_implementation(
    input: ImplementationInputModel, agent: models.Agent
) -> models.Implementation:
    """The Action (by app, key, version), its ports, and the Implementation on ``agent``."""
    definition = input.definition
    dump = definition.model_dump(mode="json")
    action, _ = models.Action.objects.update_or_create(
        app=agent.app,
        organization=agent.organization,
        key=definition.key,
        version=definition.version,
        defaults=dict(
            hash=hashlib.sha256(json.dumps(dump, sort_keys=True).encode()).hexdigest(),
            name=definition.name,
            description=definition.description or "No description",
            kind=getattr(definition.kind, "value", definition.kind),
            args=dump["args"],
            returns=dump["returns"],
            port_groups=dump["port_groups"],
            stateful=definition.stateful,
            pure=definition.pure,
            idempotent=definition.idempotent or definition.pure,
            allow_probe=definition.allow_probe,
            is_dev=definition.is_dev,
        ),
    )
    rebuild_relational_ports(action, definition)
    implementation, _ = models.Implementation.objects.update_or_create(
        agent=agent,
        interface=input.interface,
        defaults=dict(
            action=action,
            params=input.params or {},
            needs_token=input.needs_token,
            effects=getattr(input.effects, "value", input.effects),
            execution=getattr(input.execution, "value", input.execution),
        ),
    )
    for dependency in input.dependencies or []:
        models.Dependency.objects.update_or_create(
            implementation=implementation,
            key=dependency.key,
            defaults=dict(
                action_demands=[
                    d.model_dump() for d in dependency.action_dependencies or []
                ],
                state_demands=[
                    d.model_dump() for d in dependency.state_dependencies or []
                ],
                app_filter=dependency.app,
                version_filter=dependency.version,
                min_viable_instances=dependency.min_viable_instances,
                max_viable_instances=dependency.max_viable_instances,
                prefered_instances=dependency.prefered_instances,
                auto_resolvable=dependency.auto_resolvable,
                optional=dependency.optional,
                description=dependency.description,
            ),
        )
    if input.manipulates:
        implementation.manipulates.set(
            models.State.objects.filter(agent=agent, interface__in=input.manipulates)
        )
    return implementation


def implement_agent(
    client: Client,
    user: User,
    organization: Organization,
    payload: ImplementAgentInputModel,
) -> tuple[models.Agent, list]:
    """The agent of the identity, with every declared implementation (``implement_agent``'s rows)."""
    agent, _ = models.Agent.objects.get_or_create(
        client=client,
        user=user,
        organization=organization,
        defaults=dict(
            name=payload.name or client.client_id,
            app=client.release.app,
            release=client.release,
        ),
    )
    for implementation in payload.implementations or []:
        create_implementation(implementation, agent)
    return agent, []
