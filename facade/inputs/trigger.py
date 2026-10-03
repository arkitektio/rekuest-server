"""Inputs for triggers."""

import datetime

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
    description: str | None = None
    ends_at: datetime.datetime | None = strawberry.field(default=None, description="Nothing fires after this moment.")
    max_runs: int | None = strawberry.field(default=None, description="Nothing fires once it created this many runs.")
    debounce_seconds: int | None = strawberry.field(default=None, description="Fire at most once per object within this many seconds: the first signal fires, later ones are rejected.")


@strawberry.input(description="Change a trigger. Omitted fields stay as they are; the result is checked as a whole, like a new trigger. Give `agent: null` (with `interface: null`) to unpin it; give a policy as null to lift it.")
class UpdateTriggerInput:
    id: strawberry.ID
    name: str | None = None
    kind: enums.SignalKind | None = None
    identifier: str | None = None
    action: strawberry.ID | None = None
    port: str | None = None
    args: scalars.Args | None = None
    conditions: rscalars.AnyDefault | None = None
    agent: strawberry.ID | None = strawberry.UNSET
    interface: str | None = strawberry.UNSET
    enabled: bool | None = None
    description: str | None = strawberry.UNSET
    ends_at: datetime.datetime | None = strawberry.UNSET
    max_runs: int | None = strawberry.UNSET
    debounce_seconds: int | None = strawberry.UNSET


@strawberry.input(description="Fire a trigger on a stored signal by hand (a replay): the run is created whether or not the signal satisfies the trigger.")
class FireTriggerInput:
    trigger: strawberry.ID
    signal: strawberry.ID


@strawberry.input(description="Identify a trigger.")
class TriggerIdInput:
    id: strawberry.ID
