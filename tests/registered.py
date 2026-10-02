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

from facade import models
from facade.descriptors import compile_descriptors_to_jsonpath
from rekuest_core.inputs.models import DefinitionInputModel, ImplementationInputModel


def _ports(port_datas: t.Sequence[t.Any], action: models.Action, port_model: type, descriptor_field: str) -> None:
    """The port tree as rows, parents before children (``key_path`` dotted from the root)."""

    def build(port_data: t.Any, parent: t.Any, index: int, parent_path: str) -> tuple:
        path = f"{parent_path}.{port_data.key}" if parent_path else port_data.key
        row = port_model(
            action=action,
            parent=parent,
            index=index,
            key=port_data.key,
            key_path=path,
            kind=getattr(port_data.kind, "value", port_data.kind),
            identifier=port_data.identifier,
            compiled_jsonpath=compile_descriptors_to_jsonpath(getattr(port_data, descriptor_field, None) or []),
            nullable=port_data.nullable,
            dimension=port_data.dimension,
        )
        return port_data, path, row

    level = [build(port, None, i, "") for i, port in enumerate(port_datas or [])]
    while level:
        port_model.objects.bulk_create([row for _, _, row in level])
        level = [build(child, row, i, path) for port, path, row in level for i, child in enumerate(port.children or [])]


def rebuild_relational_ports(action: models.Action, definition: DefinitionInputModel) -> None:
    """Replace the action's ArgPort/ReturnPort rows and root counts from its definition."""
    action.arg_ports.all().delete()
    action.return_ports.all().delete()
    _ports(definition.args, action, models.ArgPort, "requires")
    _ports(definition.returns, action, models.ReturnPort, "provides")
    action.arg_count = len(definition.args or [])
    action.return_count = len(definition.returns or [])
    action.save(update_fields=["arg_count", "return_count"])


def create_implementation(input: ImplementationInputModel, agent: models.Agent) -> models.Implementation:
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
                action_demands=[d.model_dump() for d in dependency.action_dependencies or []],
                state_demands=[d.model_dump() for d in dependency.state_dependencies or []],
                app_filter=dependency.app,
                version_filter=dependency.version,
                min_viable_instances=dependency.min_viable_instances,
                max_viable_instances=dependency.max_viable_instances,
                prefered_instances=dependency.prefered_instances,
                auto_resolvable=dependency.auto_resolvable,
            ),
        )
    if input.manipulates:
        implementation.manipulates.set(models.State.objects.filter(agent=agent, interface__in=input.manipulates))
    return implementation


def implement_agent(client: t.Any, user: t.Any, organization: t.Any, payload: t.Any) -> tuple[models.Agent, list]:
    """The agent of the identity, with every declared implementation (``implement_agent``'s rows)."""
    agent, _ = models.Agent.objects.get_or_create(
        client=client,
        user=user,
        organization=organization,
        defaults=dict(name=payload.name or client.client_id, app=client.release.app, release=client.release),
    )
    for implementation in payload.implementations or []:
        create_implementation(implementation, agent)
    return agent, []
