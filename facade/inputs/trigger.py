"""Inputs for triggers."""

import strawberry
from rekuest_core import scalars as rscalars

from facade import enums, scalars


@strawberry.input(description="Create a trigger: on a signal of `kind` for `identifier` whose descriptors satisfy `conditions` (and the port's own requires), run `action` with the object in `port`.")
class CreateTriggerInput:
    name: str
    kind: enums.SignalKind
    identifier: str
    action: strawberry.ID
    port: str
    args: scalars.Args | None = None
    conditions: rscalars.AnyDefault | None = None
    agent: strawberry.ID | None = None
    interface: str | None = None
    enabled: bool = True


@strawberry.input(description="Change a trigger. Omitted fields stay as they are.")
class UpdateTriggerInput:
    id: strawberry.ID
    name: str | None = None
    args: scalars.Args | None = None
    conditions: rscalars.AnyDefault | None = None
    enabled: bool | None = None


@strawberry.input(description="Identify a trigger.")
class TriggerIdInput:
    id: strawberry.ID
