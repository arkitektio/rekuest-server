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

from django.db import connection
from django.db.models.expressions import RawSQL

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
# Relational port-matching engine (Actions only)
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


def _build_match_exists(
    match: PortMatchInputModel,
    table: str,
    action_alias: str,
    parent_alias: str | None,
    id_path: str,
    params: Params,
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
        conditions.append(f"{alias}.action_id = {action_alias}.id")
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
        conditions.append(f"({alias}.compiled_jsonpath IS NULL OR jsonb_path_match(%({key})s::jsonb, {alias}.compiled_jsonpath::jsonpath, '{{}}'::jsonb, true))")

    for child_index, child in enumerate(match.children or []):
        conditions.append(_build_match_exists(child, table, action_alias, alias, f"{id_path}_{child_index}", params))

    inner = " AND ".join(conditions)
    return f"EXISTS (SELECT 1 FROM {table} {alias} WHERE {inner})"


def _root_count_subquery(table: str, action_alias: str, extra_condition: str, param_key: str, value: int, params: Params) -> str:
    """Build a ``(SELECT COUNT(*) ...) = N`` clause over an action's root ports."""
    params[param_key] = value
    return f"(SELECT COUNT(*) FROM {table} pc WHERE pc.action_id = {action_alias}.id AND pc.parent_id IS NULL AND {extra_condition}) = %({param_key})s"


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
) -> tuple[str, Params]:
    """Build the (sql, named params) selecting facade_action ids satisfying EVERY demand."""
    action_alias = "a"
    clauses: list[str] = []
    params: Params = {}

    if organization_id is not None:
        params["org"] = organization_id
        clauses.append(f"{action_alias}.organization_id = %(org)s")

    for demand_index, demand in enumerate(demands):
        table = PORT_TABLE[demand.kind]

        for match_index, match in enumerate(demand.matches or []):
            clauses.append(_build_match_exists(match, table, action_alias, None, f"{demand_index}_{match_index}", params))

        if demand.force_length is not None:
            column = "arg_count" if table == PORT_TABLE["args"] else "return_count"
            key = f"force_length_{demand_index}"
            params[key] = demand.force_length
            clauses.append(f"{action_alias}.{column} = %({key})s")

        if demand.force_non_nullable_length is not None:
            clauses.append(_root_count_subquery(table, action_alias, "pc.nullable = false", f"force_non_nullable_{demand_index}", demand.force_non_nullable_length, params))

        if demand.force_structure_length is not None:
            clauses.append(_root_count_subquery(table, action_alias, "pc.kind = 'STRUCTURE'", f"force_structure_{demand_index}", demand.force_structure_length, params))

    if not clauses:
        raise ValueError("No search params provided")

    sql = f"SELECT {action_alias}.id FROM facade_action {action_alias} WHERE " + " AND ".join(clauses)
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


def get_action_port_demand_subquery(
    demands: t.Sequence[PortDemand],
    organization_id: int | str | None = None,
) -> RawSQL:
    """The port-demand statement as a ``RawSQL`` id subquery for ``filter(id__in=...)``.

    Embedding the matching statement as a nested subquery keeps the whole filter one
    round trip and lets Postgres drive it — instead of materializing every matching id
    in Python and shipping the list back as an ``IN`` literal (unbounded for
    unselective demands).
    """
    sql, params = _port_demand_sql(demands, organization_id=organization_id)
    positional_sql, positional_params = _to_positional(sql, params)
    return RawSQL(positional_sql, positional_params)


def get_action_ids_by_port_demands(
    demands: t.Sequence[PortDemand],
    model: str = "facade_action",
    organization_id: int | str | None = None,
) -> list[int]:
    """Return ids of rows in ``model`` whose ports satisfy EVERY demand, in one query.

    All demands' clauses are conjunctive, so ANDing them into a single statement is exactly
    the set intersection of per-demand results — without the N round trips.

    For ``facade_action`` this uses the indexed relational port engine. Other models
    (e.g. ``facade_shortcut``) keep their own ``args``/``returns`` JSONB and fall back to
    the JSONB-scan matcher per demand, since only Actions own relational port rows.
    Queryset filters should prefer ``get_action_port_demand_subquery`` (no id
    materialization); this id-list form serves callers that consume the ids in Python.
    """
    if model != "facade_action":
        ids: set[int] | None = None
        for demand in demands:
            new_ids = _json_scan_ids(
                demand.matches,
                type=demand.kind,
                force_length=demand.force_length,
                force_non_nullable_length=demand.force_non_nullable_length,
                force_structure_length=demand.force_structure_length,
                model=model,
            )
            ids = set(new_ids) if ids is None else ids.intersection(new_ids)
        return list(ids or [])

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


def _reject_unsupported_legacy_match_fields(matches: t.Sequence[PortMatchInputModel] | None, model: str) -> None:
    """Refuse demands the JSONB scanner cannot express instead of silently degrading them.

    The legacy scanner only compares key/kind/identifier (children positionally, one level
    deep). ``descriptors`` and ``nullable`` used to be dropped without a word, so a
    descriptor-bearing demand against e.g. shortcuts would quietly return purely structural
    matches — results that look right and aren't.
    """
    for match in matches or []:
        if match.descriptors:
            raise ValueError(f"Descriptor matching (requires/provides) is not supported for {model}: only Actions have relational port rows with compiled constraints. Remove 'descriptors' from the demand.")
        if match.nullable is not None:
            raise ValueError(f"'nullable' matching is not supported for {model}: the legacy JSONB scanner only compares key/kind/identifier. Remove 'nullable' from the demand.")
        _reject_unsupported_legacy_match_fields(match.children, model)


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


def _json_scan_ids(
    demands: t.Sequence[PortMatchInputModel] | None = None,
    type: t.Literal["args", "returns"] = "args",
    force_length: t.Optional[int] = None,
    force_non_nullable_length: t.Optional[int] = None,
    force_structure_length: t.Optional[int] = None,
    model: str = "facade_shortcut",
) -> list[int]:
    full_sql, all_params = _json_scan_params(
        demands,
        type=type,
        force_length=force_length,
        force_non_nullable_length=force_non_nullable_length,
        force_structure_length=force_structure_length,
        model=model,
    )
    return _execute_ids(full_sql, all_params)


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
