from typing import NewType

import strawberry

Identifier = NewType("Identifier", str)
JSONSerializable = NewType("JSONSerializable", object)
AnyDefault = NewType("AnyDefault", object)
Arg = NewType("Arg", object)
SearchQuery = NewType("SearchQuery", str)
InstanceID = NewType("InstanceID", str)
ActionHash = NewType("ActionHash", str)


scalar_map = {
    Identifier: strawberry.scalar(
        name="Identifier",
        description="The `Identifier` scalar is a structure identifier of the form `@package/key` (e.g. `@mikro/image`) that types STRUCTURE, MEMORY_STRUCTURE and INTERFACE ports",
        serialize=lambda v: v,
        parse_value=lambda v: v,
    ),
    JSONSerializable: strawberry.scalar(
        name="JSONSerializable",
        description="The `JSONSerializable` scalar type represents a JSON-serializable value.",
        serialize=lambda v: v,
        parse_value=lambda v: v,
    ),
    AnyDefault: strawberry.scalar(
        name="AnyDefault",
        description="The `AnyDefault` scalar is any JSON value used as a port default or a choice value; the server checks it against the port's kind",
        serialize=lambda v: v,
        parse_value=lambda v: v,
    ),
    Arg: strawberry.scalar(
        name="Arg",
        description="The `Arg` scalar type represents a an Argument in a Action assignment",
        serialize=lambda v: v,
        parse_value=lambda v: v,
    ),
    SearchQuery: strawberry.scalar(
        name="SearchQuery",
        description="The `SearchQuery` scalar is a GraphQL query string a search widget executes against its ward to populate its choices",
        serialize=lambda v: v,
        parse_value=lambda v: v,
    ),
    InstanceID: strawberry.scalar(
        name="InstanceId",
        description="The `InstanceId` scalar identifies one running instance of an agent",
        serialize=lambda v: v,
        parse_value=lambda v: v,
    ),
    ActionHash: strawberry.scalar(
        name="ActionHash",
        description="The `ActionHash` scalar is the sha256 identity hash of an action definition (key, version, ports, ...)",
        serialize=lambda v: v,
        parse_value=lambda v: v,
    ),
}
