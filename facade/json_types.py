"""What JSON is, as a type: for the values that really are open (port values, documents)."""

from pydantic import TypeAdapter

type JSON = None | bool | int | float | str | list[JSON] | dict[str, JSON]
type JSONObject = dict[str, JSON]

_VALUE: TypeAdapter[JSON] = TypeAdapter(JSON)
_OBJECT: TypeAdapter[JSONObject] = TypeAdapter(JSONObject)


def json_value(value: object) -> JSON:
    """``value`` as JSON, checked: for what arrives as an opaque scalar or a JSON column."""
    return _VALUE.validate_python(value)


def json_object(value: object) -> JSONObject:
    """``value`` as a JSON object, checked; ``ValueError`` when it is anything else."""
    return _OBJECT.validate_python(value)
