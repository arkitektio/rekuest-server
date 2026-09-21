# Ports

A port is one input (`ArgPort`) or output (`ReturnPort`) of an action, identified by its `key` and
typed by its `kind`. Ports are declared by agents in a definition, validated at registration
(`rekuest_core.inputs.models`), stored as JSON on the action, mirrored into relational rows for
matching (see [action-matching.md](action-matching.md)), and rendered by UIs through widgets.

## Kinds

| kind | value | children | `identifier` | `choices` |
| --- | --- | --- | --- | --- |
| `INT` | integer (not bool) | none | no | optional |
| `FLOAT` | number | none | no | optional |
| `STRING` | string | none | no | optional |
| `BOOL` | boolean | none | no | no |
| `DATE` | ISO-8601 date or datetime string | none | no | no |
| `ENUM` | one of `choices` | none | optional | **required** |
| `QUANTITY` | number, or `{value, unit}` | none | no | no |
| `STRUCTURE` | id of an object held by a service | none | **required** | no |
| `MEMORY_STRUCTURE` | id of an object in the agent's memory (action becomes `LOCAL`) | none | **required** | no |
| `INTERFACE` | id of any object implementing an interface | none | **required** | no |
| `LIST` | list of the item type | exactly 1 | no | no |
| `DICT` | string-keyed map; one `...` child = homogeneous value type, named children = known keys | 1 or more | no | no |
| `UNION` | a value of one of the variants | 2 or more | no | no |
| `MODEL` | object with one field per child | 1 or more | optional | no |

An `ENUM` or `MODEL` port may name the enum or class it was built from in `identifier`; the
value on the wire is still one of `choices` (or the fields), the identifier only lets an agent map
it back to its own type.

`identifier` has the form `@package/key` (for example `@mikro/image`). Ports with the same
identifier are compatible; the identifier is the only identity a structure has
(`facade/types/structure.py`).

## Keys

- A key is non-empty, is not `value` (reserved for the port's own value inside calls), and does
  not contain `..` (the port-path separator). The conventional key of a LIST or DICT item port is
  the literal `...`.
- Keys are unique among root args, among root returns, and among the children of one port.
  Args and returns may not share a root key, because validator and effect dependencies resolve
  port paths against both.
- A port path is `key[..key…]` walking `children`, e.g. `options..mask`.

## Defaults and values

`default` exists on arg ports only and must fit the kind (`rekuest_core/values.py`). The same
function checks assignment arguments at assign time: unknown keys are rejected, a non-nullable
port without a default must be present and non-null, and every value must fit its port
(recursively for LIST, DICT, MODEL and UNION). `nullable` says whether `null` is an acceptable
value; it does not make a port optional at registration. An action that declares no ports at all is
not checked (there is nothing to check against).

## Choices

`choices` are the values a port accepts. They are the data a CHOICE widget renders, their values
are unique, and a default must be one of them. Values are JSON (`AnyDefault`) and must fit the
port's kind.

## Widgets

The assign widget must fit the port kind: SLIDER on INT, FLOAT, QUANTITY; STRING on STRING;
SEARCH on STRUCTURE, MEMORY_STRUCTURE or a LIST of them; CHOICE where the port declares choices;
CUSTOM, STATE_CHOICE and PROXY anywhere. Widget `dependencies` and `follow_value` are port paths.
See `docs/design/domain-model.md` for the relational mirror and `rekuest_core/catalogs` for the
base catalog of operations validators, effects and widgets may call.

## Port groups

A port group has a unique key and lists root arg keys; every listed key must exist and a port
belongs to at most one group. Group effects are validated like port effects.

## Descriptors

`requires` (args) and `provides` (returns) are `{key, operator, value}` constraints compiled to
JSONPath (`facade/descriptors.py`). One operator vocabulary, `DescriptorOperator`: `IN`/`NOT_IN`
take a list, `LTE`/`GTE` a number, `EXISTS` no value.
