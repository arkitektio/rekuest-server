"""The wiregram document: an organization's automation, written down once and importable anywhere.

The pydantic models are the document's definition (a JSON or YAML file of this shape is a
wiregram); the strawberry inputs are the same thing over GraphQL. A rule names its target as an
agent's name and one of that agent's interfaces — never an id — so one document fits every
organization that has those agents.
"""

from __future__ import annotations

import datetime
from typing import Any, Self

import strawberry
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rekuest_core.inputs import models as rimodels
from rekuest_core.inputs import types as ritypes
from strawberry.experimental import pydantic

from facade import enums, scalars


class _WireRuleModel(BaseModel):
    """What every rule of a wiregram says: what it is called, and what it runs."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=200, description="What the document calls this rule. Unique among the document's rules of its kind; importing again updates the rule with this key.")
    name: str = Field(min_length=1, max_length=200, description="A human-readable name.")
    description: str | None = Field(default=None, description="What the rule is for.")
    agent: str = Field(min_length=1, description="The name of the agent whose action runs, as the importing organization sees it (e.g. 'kuvert').")
    interface: str = Field(min_length=1, description="The interface of that agent to run (e.g. 'sync_all_mailboxes').")
    args: dict[str, Any] = Field(default_factory=dict, description="The args every run is assigned with.")
    enabled: bool = Field(default=True, description="Whether the rule starts out enabled. An organization's own later switch is kept on re-import.")
    ends_at: datetime.datetime | None = Field(default=None, description="The rule stops after this moment.")
    max_runs: int | None = Field(default=None, ge=1, description="The rule stops once it created this many runs.")


class WireScheduleModel(_WireRuleModel):
    """A schedule: run the target on an interval or a cron line."""

    interval_seconds: int | None = Field(default=None, ge=1, description="Run every N seconds (exclusive with cron).")
    cron: str | None = Field(default=None, description="A five-field cron line, read in `timezone` (exclusive with interval_seconds).")
    timezone: str = Field(default="UTC", description="The IANA zone the cron line is read in.")
    ephemeral_runs: bool = Field(default=False, description="Create the runs as ephemeral tasks (housekeeping: retention may drop them early).")
    overlap: enums.ScheduleOverlap = Field(default=enums.ScheduleOverlap.SKIP, description="Whether a run may start while the previous one is open.")
    catch_up: bool = Field(default=False, description="Run slots missed during downtime late, in order, instead of skipping them.")

    @model_validator(mode="after")
    def one_timing(self) -> Self:
        if (self.interval_seconds is None) == (self.cron is None):
            raise ValueError(f"Schedule {self.key!r} needs exactly one of interval_seconds or cron")
        return self


class WireTriggerModel(_WireRuleModel):
    """A trigger: run the target when a service signals a matching object."""

    kind: enums.SignalKind = Field(description="The signal kind it reacts to.")
    identifier: str = Field(min_length=1, description="The structure identifier it reacts to, e.g. @mikro/arraydataset.")
    port: str = Field(min_length=1, description="The STRUCTURE argument that receives the signalled object.")
    conditions: list[rimodels.RequiresInputModel] = Field(default_factory=list, description="Extra descriptor conditions the signal must satisfy.")
    debounce_seconds: int | None = Field(default=None, ge=1, description="Fire at most once per object within this many seconds.")


class WiregramModel(BaseModel):
    """One document of automation: the schedules and triggers an organization wants."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=200, description="What the document calls itself. Importing the same key again updates what the earlier import created.")
    name: str = Field(min_length=1, max_length=200, description="A human-readable name.")
    description: str | None = Field(default=None, description="What the document is for.")
    schedules: list[WireScheduleModel] = Field(default_factory=list, description="The schedules it wants.")
    triggers: list[WireTriggerModel] = Field(default_factory=list, description="The triggers it wants.")

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        for what, rules in (("schedule", self.schedules), ("trigger", self.triggers)):
            keys = [rule.key for rule in rules]
            twice = sorted({key for key in keys if keys.count(key) > 1})
            if twice:
                raise ValueError(f"The wiregram lists the {what} key(s) {', '.join(twice)} more than once")
        return self


@pydantic.input(WireScheduleModel, description="A schedule of a wiregram: run an agent's interface on an interval or a cron line.")
class WireScheduleInput:
    key: str
    name: str
    agent: str
    interface: str
    description: str | None = None
    args: scalars.Args | None = None
    enabled: bool = True
    ends_at: datetime.datetime | None = None
    max_runs: int | None = None
    interval_seconds: int | None = None
    cron: str | None = None
    timezone: str = "UTC"
    ephemeral_runs: bool = False
    overlap: enums.ScheduleOverlap = enums.ScheduleOverlap.SKIP
    catch_up: bool = False


@pydantic.input(WireTriggerModel, description="A trigger of a wiregram: run an agent's interface when a service signals a matching object.")
class WireTriggerInput:
    key: str
    name: str
    agent: str
    interface: str
    kind: enums.SignalKind
    identifier: str
    port: str
    description: str | None = None
    args: scalars.Args | None = None
    enabled: bool = True
    ends_at: datetime.datetime | None = None
    max_runs: int | None = None
    conditions: list[ritypes.RequiresInput] | None = None
    debounce_seconds: int | None = None


@pydantic.input(WiregramModel, description="A wiregram: one document of automation. Importing it creates its schedules and triggers; importing the same key again brings them in line with the new document.")
class WiregramInput:
    key: str
    name: str
    description: str | None = None
    schedules: list[WireScheduleInput] | None = None
    triggers: list[WireTriggerInput] | None = None


@strawberry.input(description="Identify a wiregram.")
class WiregramIdInput:
    id: strawberry.ID


@strawberry.input(description="The existing rules to write down as a wiregram document.")
class ExportWiregramInput:
    key: str
    name: str
    description: str | None = None
    schedules: list[strawberry.ID] | None = None
    triggers: list[strawberry.ID] | None = None
