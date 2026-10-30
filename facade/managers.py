"""The matching engine: which actions and states satisfy a demand.

It works on the demands' pydantic models (``rekuest_core.inputs.models``). A GraphQL input is
converted at the resolver's edge (``input.to_pydantic()``, or :meth:`PortDemand.of` for the one
demand that is a plain strawberry input).
"""

from __future__ import annotations

import json
import re
import typing as t
from dataclasses import dataclass

from datetime import timedelta

from django.db import connection
from django.db.models import FloatField
from django.db.models.expressions import RawSQL
from django.utils import timezone

from facade.liveness import stale_after_seconds

from rekuest_core.inputs.models import ActionDemandInputModel, PortMatchInputModel, StateDemandInputModel

if t.TYPE_CHECKING:
    from facade.inputs import PortDemandInput

#: The named bind parameters of a statement built here.
Params = dict[str, int | str | bool]


@dataclass(frozen=True)
class PortDemand:
    """What the ports on one side of an action (its args, or its returns) must look like."""

    kind: t.Literal["args", "returns"]
    matches: list[PortMatchInputModel] | None = None
    force_length: int | None = None
    force_non_nullable_length: int | None = None
    force_structure_length: int | None = None

    @classmethod
    def of(cls, demand: PortDemandInput) -> PortDemand:
        """The demand a GraphQL ``PortDemandInput`` states."""
        return cls(
            kind=_demand_kind(demand.kind.value),
            matches=[match.to_pydantic() for match in demand.matches] if demand.matches is not None else None,
            force_length=demand.force_length,
            force_non_nullable_length=demand.force_non_nullable_length,
            force_structure_length=demand.force_structure_length,
        )


# =========================================================================
# Relational port-matching engine (Actions, and Blok args)
#
# Actions flatten their ports into the indexed ``facade_argport`` /
# ``facade_returnport`` tables (see facade.mutations.implementation). Matching
# an ``Action`` therefore becomes a set of correlated ``EXISTS`` subqueries over
# those tables instead of a sequential scan over the ``args``/``returns`` JSONB
# blobs. This uses the (action_id, parent_id), kind and identifier indexes,
# supports arbitrary nesting depth (via the self-referential ``parent`` FK), and
# enforces the compiled ``requires``/``provides`` micro-constraints via
# ``jsonb_path_match`` against a candidate descriptor object.
# =========================================================================

# Physical table names for the relational port rows, keyed by demand type.
PORT_TABLE = {"args": "facade_argport", "returns": "facade_returnport"}


@dataclass(frozen=True)
class PortOwner:
    """A row that owns relational port rows: the table it lives in, the column its port rows
    point back with, its port tables by demand type, and the root counts it keeps itself."""

    table: str
    column: str
    port_tables: dict[str, str]
    count_columns: dict[str, str] | None = None


ACTION_PORTS = PortOwner("facade_action", "action_id", PORT_TABLE, {"args": "arg_count", "returns": "return_count"})
#: A blok's args (``facade_blokargport``): the items it is opened for. A blok has no returns.
BLOK_PORTS = PortOwner("facade_blok", "blok_id", {"args": "facade_blokargport"})


def _live_agent_sql(alias: str, cutoff_key: str, params: Params) -> str:
    """Agents that can receive work now: ``liveness.agent_available_q`` as SQL."""
    params[cutoff_key] = timezone.now() - timedelta(seconds=stale_after_seconds())
    return f"({alias}.kind = 'WEBHOOK' OR ({alias}.connected AND {alias}.last_seen > %({cutoff_key})s))"


def _agent_vars_sql(alias: str) -> str:
    """A formula's ``$agent``, always bound: a path naming a missing variable raises even when silent."""
    return f"jsonb_build_object('agent', COALESCE({alias}.hardware, '{{}}'::jsonb))"


def _fits_condition(port_alias: str, object_key: str, cutoff_key: str, params: Params) -> str:
    """Whether a live implementation can handle the object: hidden only when every live agent refuses.

    One without a ``Fits`` on this port has stated no limit, and with no live implementation
    nothing can be tested. Reached from the port's own ``action_id``, not the action row: a
    reference to that keeps the planner from starting at the port rows.
    """
    live = _live_agent_sql("g", cutoff_key, params)
    return (
        f"(NOT EXISTS (SELECT 1 FROM facade_implementation i JOIN facade_agent g ON g.id = i.agent_id"
        f" JOIN facade_implementationfit f ON f.implementation_id = i.id AND f.port_key = {port_alias}.key"
        f" WHERE i.action_id = {port_alias}.action_id AND {live})"
        f" OR EXISTS (SELECT 1 FROM facade_implementation i JOIN facade_agent g ON g.id = i.agent_id"
        f" LEFT JOIN facade_implementationfit f ON f.implementation_id = i.id AND f.port_key = {port_alias}.key"
        f" WHERE i.action_id = {port_alias}.action_id AND {live}"
        f" AND (f.id IS NULL OR jsonb_path_match(%({object_key})s::jsonb, f.compiled_jsonpath::jsonpath, {_agent_vars_sql('g')}, true))))"
    )


def estimated_seconds(identifier: str, descriptors: t.Mapping[str, object]) -> RawSQL:
    """An action's estimate for this object: its fastest live implementation that fits it.

    Null when no implementation has an estimate that can be computed for this object. For a
    query on ``facade_action`` itself: the action query annotates it.
    """
    params: Params = {"object": json.dumps(descriptors), "identifier": identifier}
    live = _live_agent_sql("g", "cutoff", params)
    sql = (
        f"(SELECT MIN(jsonb_path_query_first(%(object)s::jsonb, i.estimated_runtime_path::jsonpath, {_agent_vars_sql('g')}, true)::float8)"
        f" FROM facade_implementation i JOIN facade_agent g ON g.id = i.agent_id"
        f" LEFT JOIN facade_argport p ON p.action_id = i.action_id AND p.parent_id IS NULL AND p.key = i.estimated_runtime_port"
        f" WHERE i.action_id = facade_action.id AND {live} AND i.estimated_runtime_path IS NOT NULL"
        f" AND (i.estimated_runtime_port IS NULL OR p.identifier = %(identifier)s)"
        f" AND NOT EXISTS (SELECT 1 FROM facade_implementationfit f"
        f" JOIN facade_argport fp ON fp.action_id = i.action_id AND fp.parent_id IS NULL AND fp.key = f.port_key"
        f" WHERE f.implementation_id = i.id AND fp.identifier = %(identifier)s"
        f" AND COALESCE(jsonb_path_match(%(object)s::jsonb, f.compiled_jsonpath::jsonpath, {_agent_vars_sql('g')}, true), false) = false))"
    )
    return RawSQL(*_to_positional(sql, params), output_field=FloatField())


def _build_match_exists(
    match: PortMatchInputModel,
    table: str,
    action_alias: str,
    parent_alias: str | None,
    id_path: str,
    params: Params,
    owner_column: str = "action_id",
) -> str:
    """Build one correlated ``EXISTS`` clause for a single match.

    Structural fields target the port shape, and the optional ``descriptors`` activate the
    object-level ``jsonb_path_match`` branch.

    Root matches correlate to the outer action row (``action_id = <action>.id`` and
    ``parent_id IS NULL``); nested matches correlate to their parent port row
    (``parent_id = <parent>.id``). Children recurse, so nesting depth is unbounded.
    """
    alias = f"p_{id_path}"
    conditions: list[str] = []

    if parent_alias is None:
        conditions.append(f"{alias}.{owner_column} = {action_alias}.id")
        conditions.append(f"{alias}.parent_id IS NULL")
    else:
        conditions.append(f"{alias}.parent_id = {parent_alias}.id")

    if match.at is not None:
        key = f"at_{id_path}"
        params[key] = match.at
        conditions.append(f"{alias}.index = %({key})s")

    if match.key is not None:
        key = f"key_{id_path}"
        params[key] = match.key
        conditions.append(f"{alias}.key = %({key})s")

    if match.kind is not None:
        key = f"kind_{id_path}"
        params[key] = match.kind.value
        conditions.append(f"{alias}.kind = %({key})s")

    if match.identifier is not None:
        key = f"ident_{id_path}"
        params[key] = match.identifier
        conditions.append(f"{alias}.identifier = %({key})s")

    if match.dimension is not None:
        key = f"dim_{id_path}"
        params[key] = match.dimension
        conditions.append(f"{alias}.dimension = %({key})s")

    if match.nullable is not None:
        key = f"null_{id_path}"
        params[key] = match.nullable
        conditions.append(f"{alias}.nullable = %({key})s")

    descriptors = match.descriptors
    if descriptors and parent_alias is None:
        # A root match with ONLY descriptors would evaluate jsonb_path_match against every
        # root port in the organization — the compiled predicate is unindexable in that
        # direction, so a structural field must narrow the candidate set first. Nested
        # children are exempt: their parent already narrows.
        has_structural_narrowing = any(value is not None for value in (match.at, match.key, match.kind, match.identifier, match.dimension))
        if not has_structural_narrowing:
            raise ValueError("A root port match with descriptors must also narrow structurally (identifier, kind, key, at or dimension) — descriptor-only matches would scan every port in the organization.")
    if descriptors:
        # Micro-constraint: the port's compiled requires/provides JSONPath must be satisfied by
        # the candidate object, assembled here from the runtime descriptor key/value pairs
        # (duplicate keys: last wins). A NULL compiled_jsonpath means the port declares no
        # constraints, so it accepts any object. ``silent => true`` makes structurally-invalid
        # evaluations return NULL instead of raising. Matches without descriptors skip this
        # branch and stay purely structural.
        candidate_object = {descriptor.key: descriptor.value for descriptor in descriptors}
        key = f"obj_{id_path}"
        params[key] = json.dumps(candidate_object)
        if parent_alias is None and owner_column == "action_id" and table == PORT_TABLE["args"]:
            conditions.append(_fits_condition(alias, key, f"cutoff_{id_path}", params))
        conditions.append(f"({alias}.compiled_jsonpath IS NULL OR jsonb_path_match(%({key})s::jsonb, {alias}.compiled_jsonpath::jsonpath, '{{}}'::jsonb, true))")

    for child_index, child in enumerate(match.children or []):
        conditions.append(_build_match_exists(child, table, action_alias, alias, f"{id_path}_{child_index}", params, owner_column))

    inner = " AND ".join(conditions)
    return f"EXISTS (SELECT 1 FROM {table} {alias} WHERE {inner})"


def _root_count_subquery(table: str, action_alias: str, extra_condition: str, param_key: str, value: int, params: Params, owner_column: str = "action_id") -> str:
    """Build a ``(SELECT COUNT(*) ...) = N`` clause over an action's (or another owner's) root ports."""
    params[param_key] = value
    return f"(SELECT COUNT(*) FROM {table} pc WHERE pc.{owner_column} = {action_alias}.id AND pc.parent_id IS NULL AND {extra_condition}) = %({param_key})s"


def _execute_ids(sql: str, params: Params) -> list[int]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [int(row[0]) for row in cursor.fetchall()]


def _execute_pairs(sql: str, params: Params) -> list[tuple[int, int]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [(int(first), int(second)) for first, second in cursor.fetchall()]


def _demand_kind(kind: str) -> t.Literal["args", "returns"]:
    """Which side of an action a demand is about."""
    if kind == "args":
        return "args"
    if kind == "returns":
        return "returns"
    raise ValueError("Type must be either 'args' or 'returns'")


def _port_demand_sql(
    demands: t.Sequence[PortDemand],
    organization_id: int | str | None = None,
    owner: PortOwner = ACTION_PORTS,
) -> tuple[str, Params]:
    """Build the (sql, named params) selecting the owner's ids (facade_action by default) satisfying EVERY demand."""
    action_alias = "a"
    clauses: list[str] = []
    params: Params = {}

    if organization_id is not None:
        params["org"] = organization_id
        clauses.append(f"{action_alias}.organization_id = %(org)s")

    for demand_index, demand in enumerate(demands):
        table = owner.port_tables.get(demand.kind)
        if table is None:
            raise ValueError(f"{owner.table} rows have no {demand.kind} ports to demand: only {', '.join(sorted(owner.port_tables))}")

        for match_index, match in enumerate(demand.matches or []):
            clauses.append(_build_match_exists(match, table, action_alias, None, f"{demand_index}_{match_index}", params, owner.column))

        if demand.force_length is not None:
            key = f"force_length_{demand_index}"
            if owner.count_columns is not None:
                params[key] = demand.force_length
                clauses.append(f"{action_alias}.{owner.count_columns[demand.kind]} = %({key})s")
            else:
                clauses.append(_root_count_subquery(table, action_alias, "TRUE", key, demand.force_length, params, owner.column))

        if demand.force_non_nullable_length is not None:
            clauses.append(_root_count_subquery(table, action_alias, "pc.nullable = false", f"force_non_nullable_{demand_index}", demand.force_non_nullable_length, params, owner.column))

        if demand.force_structure_length is not None:
            clauses.append(_root_count_subquery(table, action_alias, "pc.kind = 'STRUCTURE'", f"force_structure_{demand_index}", demand.force_structure_length, params, owner.column))

    if not clauses:
        raise ValueError("No search params provided")

    sql = f"SELECT {action_alias}.id FROM {owner.table} {action_alias} WHERE " + " AND ".join(clauses)
    return sql, params


_NAMED_PARAM_RE = re.compile(r"%\((\w+)\)s")


def _to_positional(sql: str, params: Params) -> tuple[str, list[int | str | bool]]:
    """Convert pyformat named placeholders to positional ``%s`` ones.

    ``RawSQL`` params are combined with the rest of the query's params into one flat
    sequence by the ORM compiler, so a mapping cannot be passed through — the same
    statement needs its params positional to be embeddable as a subquery.
    """
    ordered_keys = _NAMED_PARAM_RE.findall(sql)
    return _NAMED_PARAM_RE.sub("%s", sql), [params[key] for key in ordered_keys]


#: Positional bind parameters, as a ``RawSQL`` embedded in an ORM query takes them.
PositionalParams = list[int | str | bool]
#: The rows a port-demand statement selects: actions, bloks (by their args) or shortcuts.
DemandOwner = t.Literal["action", "blok", "shortcut"]


def _shortcut_port_demand_sql(demands: t.Sequence[PortDemand], organization_id: int | str) -> tuple[str, PositionalParams]:
    """The organization's shortcuts whose ports satisfy every demand.

    Shortcuts have no relational port rows: each demand is a scan of the shortcut's own
    ``args``/``returns`` JSONB, and a descriptor-bearing match is checked on its action's port
    rows (``ACTION_COLUMN``). The scans are nested in one statement over the organization's toolboxes.
    """
    sql = "SELECT s.id FROM facade_shortcut s JOIN facade_toolbox t ON t.id = s.toolbox_id WHERE t.organization_id = %s"
    params: PositionalParams = [organization_id]
    for demand in demands:
        scan_sql, scan_params = _json_scan_params(
            demand.matches,
            type=demand.kind,
            force_length=demand.force_length,
            force_non_nullable_length=demand.force_non_nullable_length,
            force_structure_length=demand.force_structure_length,
        )
        positional_sql, positional_params = _to_positional(scan_sql, scan_params)
        sql += f" AND s.id IN ({positional_sql})"
        params.extend(positional_params)
    return sql, params


def _positional_port_demand_sql(demands: t.Sequence[PortDemand], organization_id: int | str, owner: DemandOwner) -> tuple[str, PositionalParams]:
    """The ``owner`` rows of the organization satisfying every demand, with positional params."""
    if owner == "shortcut":
        return _shortcut_port_demand_sql(demands, organization_id)
    return _to_positional(*_port_demand_sql(demands, organization_id=organization_id, owner=BLOK_PORTS if owner == "blok" else ACTION_PORTS))


def get_port_demand_subquery(demands: t.Sequence[PortDemand], organization_id: int | str, owner: DemandOwner = "action") -> RawSQL:
    """The port-demand statement as a ``RawSQL`` id subquery for ``Q(id__in=...)``.

    Embedding the matching statement as a nested subquery keeps the whole filter one
    round trip and lets Postgres drive it — instead of materializing every matching id
    in Python and shipping the list back as an ``IN`` literal (unbounded for
    unselective demands). For bloks, the ones whose args satisfy the demands (the bloks an
    item opens in), with ``requires`` honoured as for actions.
    """
    return RawSQL(*_positional_port_demand_sql(demands, organization_id, owner))


def get_any_port_demand_subquery(sets: t.Sequence[t.Sequence[PortDemand]], organization_id: int | str, owner: DemandOwner = "action") -> RawSQL:
    """The rows satisfying every demand of at least one set, as one ``UNION`` id subquery.

    The union is spelled out rather than left to ``OR``: ``id IN (A) OR id IN (B)`` cannot be
    a semi-join, so Postgres tests every row of the organization against both sets, while
    ``id IN (A UNION B)`` still starts from each set's port rows (``tests/models/test_demand_union_plan.py``).
    """
    if any(len(demands) == 0 for demands in sets):
        raise ValueError("Every set of anyDemands needs at least one demand.")
    statements = [_positional_port_demand_sql(demands, organization_id, owner) for demands in sets]
    return RawSQL(" UNION ".join(sql for sql, _ in statements), [param for _, params in statements for param in params])


def get_action_ids_by_port_demands(
    demands: t.Sequence[PortDemand],
    organization_id: int | str | None = None,
) -> list[int]:
    """Return ids of the actions whose ports satisfy EVERY demand, in one query.

    All demands' clauses are conjunctive, so ANDing them into a single statement is exactly
    the set intersection of per-demand results — without the N round trips.
    Queryset filters should prefer ``get_port_demand_subquery`` (no id
    materialization); this id-list form serves callers that consume the ids in Python.
    """
    sql, params = _port_demand_sql(demands, organization_id=organization_id)
    return _execute_ids(sql, params)


def _action_demand_clauses(
    action_demand: ActionDemandInputModel,
    action_alias: str,
    prefix: str,
    params: Params,
) -> list[str]:
    """Build the WHERE clauses for one action demand (args + returns together).

    A ``hash`` names the action outright. Otherwise ``app`` + ``key`` are the preferred
    identification ("imagej/open_image"), and the structural matches loosen the demand to
    equivalent actions of other apps.
    ``prefix`` namespaces every param key so several demands can share one statement.
    """
    clauses: list[str] = []

    if action_demand.hash:
        params[f"{prefix}_hash"] = action_demand.hash
        clauses.append(f"{action_alias}.hash = %({prefix}_hash)s")
    else:
        if action_demand.key:
            params[f"{prefix}_key"] = action_demand.key
            clauses.append(f"{action_alias}.key = %({prefix}_key)s")

        if action_demand.app:
            params[f"{prefix}_app"] = action_demand.app
            clauses.append(f"{action_alias}.app_id IN (SELECT id FROM authentikate_app WHERE identifier = %({prefix}_app)s)")

        if action_demand.version:
            params[f"{prefix}_version"] = action_demand.version
            clauses.append(f"{action_alias}.version = %({prefix}_version)s")

        if action_demand.name:
            params[f"{prefix}_name"] = action_demand.name
            clauses.append(f"{action_alias}.name = %({prefix}_name)s")

        for index, match in enumerate(action_demand.arg_matches or []):
            clauses.append(_build_match_exists(match, PORT_TABLE["args"], action_alias, None, f"{prefix}_arg_{index}", params))

        for index, match in enumerate(action_demand.return_matches or []):
            clauses.append(_build_match_exists(match, PORT_TABLE["returns"], action_alias, None, f"{prefix}_ret_{index}", params))

        if action_demand.force_arg_length is not None:
            params[f"{prefix}_force_arg_length"] = action_demand.force_arg_length
            clauses.append(f"{action_alias}.arg_count = %({prefix}_force_arg_length)s")

        if action_demand.force_return_length is not None:
            params[f"{prefix}_force_return_length"] = action_demand.force_return_length
            clauses.append(f"{action_alias}.return_count = %({prefix}_force_return_length)s")

        # The action must implement ALL requested protocols (one EXISTS per name, ANDed) —
        # mirrors the name-based matching of ``ActionFilter.protocols``.
        for protocol_index, protocol_name in enumerate(action_demand.protocols or []):
            key = f"{prefix}_protocol_{protocol_index}"
            params[key] = protocol_name
            clauses.append(f"EXISTS (SELECT 1 FROM facade_action_protocols ap_{key} JOIN facade_protocol p_{key} ON p_{key}.id = ap_{key}.protocol_id WHERE ap_{key}.action_id = {action_alias}.id AND p_{key}.name = %({key})s)")

        # Semantic qualifiers: tri-state — None matches either.
        for qualifier, value in (("pure", action_demand.pure), ("idempotent", action_demand.idempotent), ("stateful", action_demand.stateful)):
            if value is not None:
                params[f"{prefix}_{qualifier}"] = value
                clauses.append(f"{action_alias}.{qualifier} = %({prefix}_{qualifier})s")

    if not clauses:
        raise ValueError(f"No search params provided {action_demand}")

    return clauses


def get_action_ids_by_action_demands(
    action_demands: t.Sequence[ActionDemandInputModel],
    organization_id: int | str | None = None,
) -> list[list[int]]:
    """Return the matching Action ids for EACH demand, index-aligned, in one round trip.

    The demands stay independent — a caller enforcing "must satisfy all demands" (e.g. the
    agent filter) does so on its side, where each demand may be met by a different action.
    What is consolidated here is the SQL: one ``UNION ALL`` statement instead of one query
    per demand.
    """
    action_alias = "a"
    params: Params = {}
    selects: list[str] = []

    if organization_id is not None:
        params["org"] = organization_id

    for index, action_demand in enumerate(action_demands):
        clauses = _action_demand_clauses(action_demand, action_alias, f"d{index}", params)
        if organization_id is not None:
            clauses.insert(0, f"{action_alias}.organization_id = %(org)s")
        # ``index`` is a loop counter, never user input — safe to inline as the demand tag.
        selects.append(f"SELECT {index} AS demand_index, {action_alias}.id FROM facade_action {action_alias} WHERE " + " AND ".join(clauses))

    if not selects:
        return []

    results: list[list[int]] = [[] for _ in action_demands]
    for demand_index, action_id in _execute_pairs("\nUNION ALL\n".join(selects), params):
        results[demand_index].append(action_id)
    return results


# =========================================================================
# Legacy JSONB-scan matcher
#
# Retained for models that carry ``args``/``returns`` (or ``ports``) as JSONB but
# have no relational port rows: Shortcuts and State schemas. It only compares the
# coarse key/kind/identifier fields and matches children positionally one level
# deep; this is acceptable for those secondary lookups.
# =========================================================================


#: Models whose JSONB ports are a subset of their Action's ports (a shortcut's args are the
#: action's args it has not saved). The JSONB itself carries no compiled constraints, but the
#: action's relational port rows do: a descriptor is checked on the row with the same key.
ACTION_COLUMN = {"facade_shortcut": "action_id"}

#: How deep below a root port the JSONB scanner looks at children.
_JSON_SCAN_CHILD_DEPTH = 1


def _reject_unsupported_legacy_match_fields(matches: t.Sequence[PortMatchInputModel] | None, model: str, depth: int = 0) -> None:
    """Refuse demands the JSONB scanner cannot express instead of silently degrading them.

    The legacy scanner only compares key/kind/identifier (children positionally, one level
    deep). ``descriptors`` and ``nullable`` used to be dropped without a word, so a
    descriptor-bearing demand would quietly return purely structural matches — results that
    look right and aren't. Descriptors are expressible for a model that names its action
    (``ACTION_COLUMN``), down to the depth the scanner reaches.
    """
    for match in matches or []:
        if match.descriptors:
            if model not in ACTION_COLUMN:
                raise ValueError(f"Descriptor matching (requires/provides) is not supported for {model}: it has no action whose relational port rows carry the compiled constraints. Remove 'descriptors' from the demand.")
            if depth > _JSON_SCAN_CHILD_DEPTH:
                raise ValueError(f"Descriptor matching (requires/provides) is not supported for {model} below the first level of children: the JSONB scanner does not look deeper. Remove 'descriptors' from the nested match.")
        if match.nullable is not None:
            raise ValueError(f"'nullable' matching is not supported for {model}: the legacy JSONB scanner only compares key/kind/identifier. Remove 'nullable' from the demand.")
        _reject_unsupported_legacy_match_fields(match.children, model, depth + 1)


def _json_scan_descriptor_clauses(item: PortMatchInputModel, index: int, type: t.Literal["args", "returns"], model: str, params: Params) -> list[str]:
    """The descriptor checks of one root match (and its children) against the action's port rows.

    Evaluated inside the scan's ``jsonb_array_elements`` subquery, where ``item`` is the
    candidate JSONB port: the action's root port row with the same key must accept the
    candidate object, and a child's row (same position under that root) its own. Same
    predicate as the relational engine — a NULL ``compiled_jsonpath`` accepts anything. A row
    without an action (or whose action lost the port) has nothing to accept it, so it does
    not match.
    """
    table = PORT_TABLE[type]
    action_column = f"{model}.{ACTION_COLUMN[model]}"
    clauses: list[str] = []

    def accepts(alias: str, match: PortMatchInputModel, key: str) -> str:
        params[key] = json.dumps({descriptor.key: descriptor.value for descriptor in match.descriptors or []})
        return f"({alias}.compiled_jsonpath IS NULL OR jsonb_path_match(%({key})s::jsonb, {alias}.compiled_jsonpath::jsonpath, '{{}}'::jsonb, true))"

    root = f"dp_{type}_{index}"
    root_conditions = f"{root}.action_id = {action_column} AND {root}.parent_id IS NULL AND {root}.key = item->>'key'"

    if item.descriptors:
        clauses.append(f"EXISTS (SELECT 1 FROM {table} {root} WHERE {root_conditions} AND {accepts(root, item, f'{type}_obj_{index}')})")

    for child_index, child in enumerate(item.children or []):
        if not child.descriptors:
            continue
        alias = f"dc_{type}_{index}_{child_index}"
        clauses.append(f"EXISTS (SELECT 1 FROM {table} {root} JOIN {table} {alias} ON {alias}.parent_id = {root}.id WHERE {root_conditions} AND {alias}.index = {child_index} AND {accepts(alias, child, f'{type}_obj_{index}_{child_index}')})")

    return clauses


def build_child_recursively(item: PortMatchInputModel, prefix: str, value_path: str, parts: list[str], params: Params) -> None:
    if item.key:
        parts.append(f"{prefix}->>'key' = %({value_path}_key)s")
        params[f"{value_path}_key"] = item.key

    if item.kind:
        parts.append(f"{prefix}->>'kind' = %({value_path}_kind)s")
        params[f"{value_path}_kind"] = item.kind.value

    if item.identifier:
        parts.append(f"{prefix}->>'identifier' = %({value_path}_identifier)s")
        params[f"{value_path}_identifier"] = item.identifier


def build_sql_for_item_recursive(item: PortMatchInputModel, index: int, at_value: int | None = None, prefix: str = "arg") -> tuple[str, Params]:
    sql_parts: list[str] = []
    params: Params = {}

    if at_value is not None:
        sql_parts.append(f"idx = %({prefix}_at_{index})s")
        params[f"{prefix}_at_{index}"] = at_value + 1

    if item.key:
        sql_parts.append(f"item->>'key' = %({prefix}_key_{index})s")
        params[f"{prefix}_key_{index}"] = item.key

    if item.kind:
        sql_parts.append(f"item->>'kind' = %({prefix}_kind_{index})s")
        params[f"{prefix}_kind_{index}"] = item.kind.value

    if item.identifier:
        sql_parts.append(f"item->>'identifier' = %({prefix}_identifier_{index})s")
        params[f"{prefix}_identifier_{index}"] = item.identifier

    if item.children:
        child_parts: list[str] = []
        child_params: Params = {}
        for idx, child in enumerate(item.children):
            build_child_recursively(
                child,
                # jsonb `->` on an array is 0-based (unlike the 1-based WITH ORDINALITY idx above).
                f"item->'children'->{idx}",
                f"children_{index}_{idx}",
                child_parts,
                child_params,
            )
        sql_parts += child_parts
        params.update(child_params)

    return (" AND ".join(sql_parts), params)


def _json_scan_params(
    search_params: t.Sequence[PortMatchInputModel] | None,
    type: t.Literal["args", "returns"] = "args",
    force_length: t.Optional[int] = None,
    force_non_nullable_length: t.Optional[int] = None,
    force_structure_length: t.Optional[int] = None,
    model: str = "facade_shortcut",
) -> tuple[str, Params]:
    individual_queries: list[str] = []
    all_params: Params = {}
    if search_params:
        _reject_unsupported_legacy_match_fields(search_params, model)
        for index, item in enumerate(search_params):
            sql_part, params = build_sql_for_item_recursive(item, index, at_value=item.at)
            if model in ACTION_COLUMN:
                sql_part = " AND ".join([part for part in [sql_part, *_json_scan_descriptor_clauses(item, index, type, model, params)] if part])
            subquery = f"EXISTS (SELECT 1 FROM jsonb_array_elements({type}) WITH ORDINALITY AS j(item, idx) WHERE {sql_part})"
            individual_queries.append(subquery)
            all_params.update(params)

    if force_length is not None:
        all_params["force_length"] = force_length
        individual_queries.append(f"jsonb_array_length({type}) = %(force_length)s")

    if force_non_nullable_length is not None:
        sql_part = "item->>'nullable'::text = 'false'"
        all_params["force_non_nullable_length"] = force_non_nullable_length
        individual_queries.append(f"""(SELECT COUNT(*) FROM jsonb_array_elements({type}) AS j(item) WHERE {sql_part}) = %(force_non_nullable_length)s""")

    if force_structure_length is not None:
        sql_part = "item->>'kind' = 'STRUCTURE'"
        all_params["force_structure_length"] = force_structure_length
        individual_queries.append(f"""(SELECT COUNT(*) FROM jsonb_array_elements({type}) AS j(item) WHERE {sql_part}) = %(force_structure_length)s""")

    if not individual_queries:
        raise ValueError("No search params provided")

    full_sql = f"SELECT id FROM {model} WHERE " + " AND ".join(individual_queries)
    return full_sql, all_params


def build_state_params(
    search_params: t.Sequence[PortMatchInputModel] | None,
    model: str = "facade_statedefinition",
) -> tuple[str, Params]:
    individual_queries: list[str] = []
    all_params: Params = {}
    if search_params:
        _reject_unsupported_legacy_match_fields(search_params, model)
        for index, item in enumerate(search_params):
            sql_part, params = build_sql_for_item_recursive(item, index, at_value=item.at)
            subquery = f"EXISTS (SELECT 1 FROM jsonb_array_elements(ports) WITH ORDINALITY AS j(item, idx) WHERE {sql_part})"
            individual_queries.append(subquery)
            all_params.update(params)

    if not individual_queries:
        raise ValueError("No search params provided")

    full_sql = f"SELECT id FROM {model} WHERE " + " AND ".join(individual_queries)
    return full_sql, all_params


def get_state_ids_by_demands(
    matches: t.Sequence[PortMatchInputModel] | None = None,
    model: str = "facade_statedefinition",
) -> list[int]:
    full_sql, all_params = build_state_params(matches, model=model)
    return _execute_ids(full_sql, all_params)


def state_demand_state_filters(demand: StateDemandInputModel) -> dict[str, str | list[int]]:
    """State-queryset filter kwargs for one state demand.

    ``app`` + ``key`` match the State's own identity columns (the preferred identification);
    non-empty ``matches`` resolve StateDefinition ids via the port matcher. Shared by the
    agent filter, dependency resolution and the ``state_for`` query so their semantics stay
    in lockstep. Raises when the demand carries no criteria at all.
    """
    filters: dict[str, str | list[int]] = {}
    if demand.key:
        filters["key"] = demand.key
    if demand.app:
        filters["app_identifier"] = demand.app
    if demand.hash:
        filters["definition__hash"] = demand.hash
    if demand.matches:
        filters["definition_id__in"] = get_state_ids_by_demands(demand.matches)
    if not filters:
        raise ValueError(f"No search params provided {demand}")
    return filters
