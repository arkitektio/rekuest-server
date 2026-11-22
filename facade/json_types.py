"""What JSON is, as a type: for the values that really are open (port values, documents)."""

from typing import TypeAliasType

from pydantic import TypeAdapter

# The call form of ``type JSON = ...``: the same TypeAliasType at runtime, recursive through
# the quoted name, which pydantic resolves as it does the statement's.
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
