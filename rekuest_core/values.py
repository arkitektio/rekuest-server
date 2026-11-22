"""Does a runtime value fit a port? Shared by definition-time default checks and assign-time argument checks.

Rules per kind: INT int (not bool), FLOAT int|float, STRING str, BOOL bool, DATE ISO-8601 string,
LIST list of the child kind, DICT dict of the child kind, MODEL dict keyed by child ports, ENUM one
of the choices, STRUCTURE/MEMORY_STRUCTURE/INTERFACE a ``{"__identifier", "object"}`` reference, QUANTITY a number or a
``{"value": number, "unit": str}`` object, UNION anything one of its variants accepts.
"""

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any, Protocol


class PortLike(Protocol):
    """The subset of a port both the input and the output models expose."""

    key: str
    kind: Any
    nullable: bool
    children: Any
    choices: Any


def _kind_name(kind: Any) -> str:
    return kind.value if hasattr(kind, "value") else str(kind)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def value_mismatch(
    port: PortLike,
    value: Any,
    path: str | None = None,
    *,
    reference_envelope: bool = False,
) -> str | None:
    """Why ``value`` does not fit ``port``, or ``None`` when it does. ``None`` values are the caller's business.

    ``reference_envelope`` selects how a structure reference is spelled. An
    assignment argument carries the ``{"__identifier", "object"}`` envelope a
    client sends; a port *default* is declared in the definition and is a bare
    id, so the two callers ask for different things.
    """
    path = path or port.key
    kind = _kind_name(port.kind)
    children = list(port.children or [])
    if kind == "INT" and not (isinstance(value, int) and not isinstance(value, bool)):
        return f"{path}: expected an INT, got {type(value).__name__}"
    if kind == "FLOAT" and not _is_number(value):
        return f"{path}: expected a FLOAT, got {type(value).__name__}"
    if kind == "STRING" and not isinstance(value, str):
        return f"{path}: expected a STRING, got {type(value).__name__}"
    if kind == "BOOL" and not isinstance(value, bool):
        return f"{path}: expected a BOOL, got {type(value).__name__}"
    if kind == "DATE":
        if not isinstance(value, str):
            return (
                f"{path}: expected an ISO-8601 DATE string, got {type(value).__name__}"
            )
        try:
            datetime.fromisoformat(value)
        except ValueError:
            try:
                date.fromisoformat(value)
            except ValueError:
                return f"{path}: {value!r} is not an ISO-8601 date"
    if kind == "LIST":
        if not isinstance(value, list):
            return f"{path}: expected a LIST, got {type(value).__name__}"
        for index, item in enumerate(value):
            if item is None and not children[0].nullable:
                return f"{path}[{index}]: null is not allowed"
            if item is not None and (
                mismatch := value_mismatch(
                    children[0],
                    item,
                    f"{path}[{index}]",
                    reference_envelope=reference_envelope,
                )
            ):
                return mismatch
    if kind == "DICT":
        if not isinstance(value, dict):
            return f"{path}: expected a DICT, got {type(value).__name__}"
        if len(children) == 1 and children[0].key == "...":
            # homogeneous map: every value is of the item type
            for name, item in value.items():
                if item is None and not children[0].nullable:
                    return f"{path}[{name!r}]: null is not allowed"
                if item is not None and (
                    mismatch := value_mismatch(
                        children[0],
                        item,
                        f"{path}[{name!r}]",
                        reference_envelope=reference_envelope,
                    )
                ):
                    return mismatch
        else:
            # named children: known keys are typed, other keys pass through
            fields = {child.key: child for child in children}
            for name, item in value.items():
                child = fields.get(name)
                if child is None:
                    continue
                if item is None and not child.nullable:
                    return f"{path}[{name!r}]: null is not allowed"
                if item is not None and (
                    mismatch := value_mismatch(
                        child,
                        item,
                        f"{path}[{name!r}]",
                        reference_envelope=reference_envelope,
                    )
                ):
                    return mismatch
    if kind == "MODEL":
        if not isinstance(value, dict):
            return f"{path}: expected a MODEL object, got {type(value).__name__}"
        fields = {child.key: child for child in children}
        unknown = sorted(set(value) - set(fields) - {"__identifier"})
        if unknown:
            return f"{path}: unknown fields {unknown}"
        for name, child in fields.items():
            item = value.get(name)
            if (
                item is None
                and not child.nullable
                and getattr(child, "default", None) is None
            ):
                return f"{path}.{name}: required field is missing"
            if item is not None and (
                mismatch := value_mismatch(
                    child, item, f"{path}.{name}", reference_envelope=reference_envelope
                )
            ):
                return mismatch
    if kind == "ENUM":
        values = {choice.value for choice in port.choices or []}
        if value not in values:
            return f"{path}: {value!r} is not one of the choices {sorted(map(str, values))}"
    if (
        kind in ("STRUCTURE", "MEMORY_STRUCTURE", "INTERFACE")
        and not reference_envelope
    ):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return f"{path}: expected a {kind} id (string or int), got {type(value).__name__}"
    if kind in ("STRUCTURE", "MEMORY_STRUCTURE", "INTERFACE") and reference_envelope:
        # Clients send a structure reference as an envelope, never a bare id: the
        # identifier rides along so the receiver can check the reference belongs to
        # the port it landed on before resolving it.
        if not isinstance(value, dict):
            return f"{path}: expected a {kind} reference {{'__identifier', 'object'}}, got {type(value).__name__}"
        if "__identifier" not in value:
            return f"{path}: {kind} reference is missing its `__identifier` key"
        if "object" not in value:
            return f"{path}: {kind} reference is missing its `object` key"
        identifier = getattr(port, "identifier", None)
        if identifier is not None and value["__identifier"] != identifier:
            return f"{path}: {kind} reference identifier mismatch: expected {identifier!r}, got {value['__identifier']!r}"
        referenced = value["object"]
        if isinstance(referenced, bool) or not isinstance(referenced, (str, int)):
            return f"{path}: {kind} reference `object` must be a str or int id, got {type(referenced).__name__}"
    if kind == "QUANTITY":
        if isinstance(value, dict):
            if not _is_number(value.get("value")) or (
                "unit" in value and not isinstance(value["unit"], str)
            ):
                return f"{path}: a QUANTITY object needs a numeric `value` and an optional string `unit`"
        elif not _is_number(value):
            return f"{path}: expected a QUANTITY (number or {{value, unit}}), got {type(value).__name__}"
    if kind == "UNION":
        if not any(
            value_mismatch(child, value, path, reference_envelope=reference_envelope)
            is None
            for child in children
        ):
            return f"{path}: {value!r} matches none of the UNION variants {[_kind_name(child.kind) for child in children]}"
    return None


def validate_assignment_args(ports: Iterable[PortLike], args: dict[str, Any]) -> None:
    """Assignment arguments against the action's root arg ports.

    Unknown keys are rejected; a non-nullable port without a default must be present and
    non-null; every present value must fit its port's kind (recursively for containers).
    """
    ports = list(ports)
    known = {port.key for port in ports}
    unknown = sorted(set(args) - known)
    if unknown:
        raise ValueError(
            f"Unknown arguments {unknown}; this action accepts {sorted(known)}"
        )
    for port in ports:
        value = args.get(port.key)
        if value is None:
            if port.nullable or getattr(port, "default", None) is not None:
                continue
            raise ValueError(f"Argument {port.key!r} is required and has no default")
        mismatch = value_mismatch(port, value, reference_envelope=True)
        if mismatch:
            raise ValueError(f"Argument {mismatch}")
