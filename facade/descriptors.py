"""Compilation of requires/provides descriptors into PostgreSQL JSONPath strings.

A port's ``requires`` (inputs) or ``provides`` (outputs) descriptors express micro-constraints
on the *data* that flows through the port (e.g. ``{"axes": "c"}``). They are compiled once, at
write time, into a PostgreSQL JSONPath predicate stored on the relational ArgPort/ReturnPort
row, and evaluated at query time via ``jsonb_path_match`` against a candidate descriptor object.

This module is intentionally dependency-light (only ``json`` and the operator enum) so it
can be imported from migrations and unit tests without pulling in the mutation layer.
"""

import json

from rekuest_core.enums import RequiresOperator

# Descriptor keys are interpolated into a JSONPath string, so they must not be able to break out
# of the member accessor. The key is one *flat* member name (the query side builds the candidate
# object as ``{key: value}``, see ``facade.managers``), rendered as a quoted jsonpath string
# literal via ``json.dumps`` — JSON string escaping is a subset of jsonpath string-literal escaping,
# so any character (``@``, ``/``, ``.``, quotes, backslashes) is safe. Values are always rendered
# via json.dumps as well.


def _normalize_jsonpath_key(key: str) -> str:
    """Validate a descriptor key and render it as a rooted, quoted JSONPath member (``$."key"``)."""
    # A non-string or empty key is a client error, surfaced as ValueError like every other
    # descriptor problem so the mutation reports it uniformly.
    raw = key.removeprefix("$.") if isinstance(key, str) else ""
    if not raw:
        raise ValueError(f"Invalid descriptor key for JSONPath compilation: {key!r}")
    return f"$.{json.dumps(raw)}"


def _compile_descriptor_condition(desc) -> str:
    """Map a single requires/provides descriptor to one PostgreSQL JSONPath predicate.

    ``desc`` is any object exposing ``key``, ``operator`` and ``value`` (pydantic models,
    ``SimpleNamespace``, etc.). ``RequiresOperator`` and ``ProvidesOperator`` share the same
    members, so a single mapping serves both. ``operator`` may be the enum member or its raw
    string value (``str`` enums compare equal to their value).
    """
    pg_path = _normalize_jsonpath_key(desc.key)
    # json.dumps renders Python True -> 'true', strings -> '"s"', ints -> '1' (valid JSONPath).
    formatted_val = json.dumps(desc.value)
    op = desc.operator

    if op == RequiresOperator.EXISTS:
        # Existence check; the boolean value decides whether we assert presence or absence.
        # Only real booleans are accepted: anything else ("true", 1, None) would previously
        # compile to the absence branch — the exact inverse of what the client meant.
        if not isinstance(desc.value, bool):
            raise ValueError(f"Operator {op} requires a boolean value (True = must exist, False = must not exist), got {desc.value!r}")
        return f"exists({pg_path})" if desc.value else f"!(exists({pg_path}))"

    if op in (RequiresOperator.MATCHES, RequiresOperator.EQUALS):
        return f"{pg_path} == {formatted_val}"

    if op == RequiresOperator.NOT_EQUALS:
        return f"{pg_path} != {formatted_val}"

    if op == RequiresOperator.GTE:
        return f"{pg_path} >= {formatted_val}"

    if op == RequiresOperator.LTE:
        return f"{pg_path} <= {formatted_val}"

    if op == RequiresOperator.CONTAINS:
        # Array membership: the array at the path contains the given value.
        return f"{pg_path}[*] == {formatted_val}"

    if op in (RequiresOperator.IN, RequiresOperator.NOT_IN):
        # Standard JSONPath has no IN operator, so expand into a boolean combination.
        if not isinstance(desc.value, (list, tuple)):
            raise ValueError(f"Operator {op} requires a list value, got {desc.value!r}")
        if op == RequiresOperator.IN:
            parts = [f"{pg_path} == {json.dumps(v)}" for v in desc.value]
            return "(" + " || ".join(parts) + ")" if parts else "(false)"
        parts = [f"{pg_path} != {json.dumps(v)}" for v in desc.value]
        return "(" + " && ".join(parts) + ")" if parts else "(true)"

    raise ValueError(f"Unsupported JSONPath operator: {op}")


def compile_descriptors_to_jsonpath(descriptors) -> str | None:
    """Translate a list of requires/provides descriptors into a PostgreSQL JSONPath string.

    Returns ``None`` when there are no descriptors (so Django stores NULL). Multiple
    descriptors are AND-ed together with the JSONPath ``&&`` operator.
    """
    if not descriptors:
        return None

    return " && ".join(_compile_descriptor_condition(desc) for desc in descriptors)


# Provides descriptors compile identically to requires descriptors (same operator set); the
# alias keeps call sites self-documenting about which side they are compiling.
compile_returndescriptors_to_jsonpath = compile_descriptors_to_jsonpath
