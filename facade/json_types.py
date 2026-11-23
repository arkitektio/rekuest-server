"""What JSON is, as a type: for the values that really are open (port values, documents)."""

from typing import TYPE_CHECKING, TypeAlias, TypeAliasType

from pydantic import TypeAdapter

if TYPE_CHECKING:
    # What the checker reads: a plain recursive alias, the way pydantic declares ``JsonValue``.
    # The call form below resolves its quoted self-reference only in some import orders, and
    # then every ``JSON`` silently turned Unknown.
    JSON: TypeAlias = None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]
    JSONObject: TypeAlias = dict[str, JSON]
else:
    # The call form of ``type JSON = ...``: the same TypeAliasType at runtime, recursive
    # through the quoted name, which pydantic resolves as it does the statement's.
    JSON = TypeAliasType(
        "JSON", None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]
    )
    JSONObject = TypeAliasType("JSONObject", dict[str, JSON])

_VALUE: TypeAdapter[JSON] = TypeAdapter(JSON)
_OBJECT: TypeAdapter[JSONObject] = TypeAdapter(JSONObject)


def json_value(value: object) -> JSON:
    """``value`` as JSON, checked: for what arrives as an opaque scalar or a JSON column."""
    return _VALUE.validate_python(value)


def json_object(value: object) -> JSONObject:
    """``value`` as a JSON object, checked; ``ValueError`` when it is anything else."""
    return _OBJECT.validate_python(value)
