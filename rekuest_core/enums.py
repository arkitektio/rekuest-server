import strawberry
from enum import Enum


@strawberry.enum(description="The kind of action.")
class ActionKind(str, Enum):
    FUNCTION = "FUNCTION"
    GENERATOR = "GENERATOR"


@strawberry.enum(description="The kind of a port: its structural type. Decides which of children, identifier and choices the port must, may or must not carry (see docs/design/ports.md).")
class PortKind(str, Enum):
    INT = "INT"
    STRING = "STRING"
    STRUCTURE = "STRUCTURE"
    LIST = "LIST"
    BOOL = "BOOL"
    DICT = "DICT"
    FLOAT = "FLOAT"
    DATE = "DATE"
    UNION = "UNION"
    ENUM = "ENUM"
    MODEL = "MODEL"
    MEMORY_STRUCTURE = "MEMORY_STRUCTURE"
    INTERFACE = "INTERFACE"
    QUANTITY = "QUANTITY"


# Per-member SDL descriptions. Set on the strawberry definition rather than via
# ``strawberry.enum_value`` so the Python-side values stay plain strings (pydantic and the
# relational matcher compare ``kind.value``).
_PORT_KIND_DESCRIPTIONS = {
    "INT": "An integer. No children; choices optional.",
    "STRING": "A string. No children; choices optional.",
    "STRUCTURE": "A reference to an object held by a service, typed by `identifier` (@package/key, required). Values are ids. No children.",
    "LIST": "A list; exactly one child describes the item type (conventionally keyed '...').",
    "BOOL": "A boolean. No children.",
    "DICT": "A string-keyed map. One child keyed '...' describes a homogeneous value type; several named children describe the known keys.",
    "FLOAT": "A floating point number. No children; choices optional.",
    "DATE": "An ISO-8601 date or datetime string. No children.",
    "UNION": "One of several variants; at least two children, each a variant.",
    "ENUM": "One of a fixed set of values; `choices` required.",
    "MODEL": "An object with named fields; at least one child per field, `identifier` optional.",
    "MEMORY_STRUCTURE": "A reference to an object that lives in the agent's memory, typed by `identifier` (required). Makes the action LOCAL-scoped. No children.",
    "INTERFACE": "A reference to any object implementing an interface, typed by `identifier` (required). No children.",
    "QUANTITY": "A physical quantity with a unit; `reference_unit` required, `dimension` derived. No children.",
}
for _port_kind_value in PortKind.__strawberry_definition__.values:
    _port_kind_value.description = _PORT_KIND_DESCRIPTIONS[_port_kind_value.name]


@strawberry.enum(description="The kind of assign widget.")
class AssignWidgetKind(str, Enum):
    SEARCH = "SEARCH"
    CHOICE = "CHOICE"
    SLIDER = "SLIDER"
    CUSTOM = "CUSTOM"
    STRING = "STRING"
    STATE_CHOICE = "STATE_CHOICE"
    PROXY = "PROXY"


@strawberry.enum(description="The kind of return widget.")
class ReturnWidgetKind(str, Enum):
    CHOICE = "CHOICE"
    CUSTOM = "CUSTOM"


@strawberry.enum(description="The kind of effect.")
class EffectKind(str, Enum):
    MESSAGE = "MESSAGE"
    HIDE = "HIDE"
    CUSTOM = "CUSTOM"


@strawberry.enum(description="What running an implementation again would do to the world. Purely informational: shown to whoever decides about a lost task.")
class Effects(str, Enum):
    NONE = "NONE"
    REPEATABLE = "REPEATABLE"
    UNKNOWN = "UNKNOWN"
    IRREVERSIBLE = "IRREVERSIBLE"


@strawberry.enum(description="How an implementation runs: a WORKFLOW may call other actions and is resumed from its journal when its agent dies; a PLAIN one's task ends LOST.")
class Execution(str, Enum):
    PLAIN = "PLAIN"
    WORKFLOW = "WORKFLOW"


@strawberry.enum(description="The kind of action scope.")
class ActionScope(str, Enum):
    GLOBAL = "GLOBAL"
    LOCAL = "LOCAL"
    BRIDGE_GLOBAL_TO_LOCAL = "BRIDGE_GLOBAL_TO_LOCAL"
    BRIDGE_LOCAL_TO_GLOBAL = "BRIDGE_LOCAL_TO_GLOBAL"


@strawberry.enum(description="The operator of a requires/provides descriptor: how a port's constraint compares the object's value at `key` with `value`.")
class DescriptorOperator(str, Enum):
    MATCHES = "MATCHES"
    EXISTS = "EXISTS"
    LTE = "LTE"
    GTE = "GTE"
    EQUALS = "EQUALS"
    CONTAINS = "CONTAINS"
    NOT_EQUALS = "NOT_EQUALS"
    IN = "IN"
    NOT_IN = "NOT_IN"


# Requires and provides descriptors share one operator vocabulary; the old names stay as aliases.
RequiresOperator = DescriptorOperator
ProvidesOperator = DescriptorOperator


@strawberry.enum
class AssignPolicy(str, Enum):
    AUTOMATIC = "AUTOMATIC"
    BALANCED = "BALANCED"
    ROUND_ROBIN = "ROUND_ROBIN"
    LEAST_BUSY = "LEAST_BUSY"
    FASTEST_RESPONSE = "FASTEST_RESPONSE"


@strawberry.enum
class OptionKey(str, Enum):
    LABEL = "LABEL"
    DESCRIPTION = "DESCRIPTION"
    LOGO = "LOGO"
    VALUE = "VALUE"


@strawberry.enum(description="The kind of value a catalog component prop accepts or a catalog operation argument/return carries.")
class CatalogValueKind(str, Enum):
    STRING = "STRING"
    INT = "INT"
    FLOAT = "FLOAT"
    BOOL = "BOOL"
    DICT = "DICT"
    LIST = "LIST"
    ANY = "ANY"
    CALLBACK = "CALLBACK"


@strawberry.enum(description="Severity of a registration finding. Errors are never stored (they abort registration), so only WARNING exists.")
class DiagnosticLevel(str, Enum):
    WARNING = "WARNING"


@strawberry.enum(description="Aggregation computed over a tracked value within a window.")
class WindowFunction(str, Enum):
    MEAN = "MEAN"
    MIN = "MIN"
    MAX = "MAX"
    SUM = "SUM"
    COUNT = "COUNT"
    LAST = "LAST"
    FIRST = "FIRST"
    STD = "STD"
