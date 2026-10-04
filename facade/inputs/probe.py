"""Inputs for probes (see :mod:`facade.probes`)."""

import strawberry
from pydantic import BaseModel, Field
from strawberry.experimental import pydantic

from facade import scalars
from facade.json_types import JSON
from rekuest_core import scalars as rscalars


class ProbeInputModel(BaseModel):
    """Base model for creating a probe.

    Deliberately much smaller than ``AssignInputModel``: probes have no parents, hooks,
    dependencies, capture, or policy — anything that needs a persisted task refuses at
    probe time.
    """

    action: str | None = Field(default=None, description="The action ID to probe")
    implementation: str | None = Field(default=None, description="The implementation ID to probe directly")
    action_hash: str | None = Field(default=None, description="The hash of the action to probe")
    args: dict[str, JSON] = Field(description="The args of the probe. A dictionary of ports and values")
    reference: str | None = Field(default=None, description="An optional caller-side reference echoed to the agent")


@pydantic.input(ProbeInputModel, description="The input for a probe — a zero-persistence invocation.")
class ProbeInput:
    action: strawberry.ID | None = None
    implementation: strawberry.ID | None = None
    action_hash: rscalars.ActionHash | None = None
    args: scalars.Args
    reference: str | None = None


class CancelProbeInputModel(BaseModel):
    """Base model for cancelling a probe."""

    probe: str = Field(description="The probe ID to cancel")


@pydantic.input(CancelProbeInputModel, description="The input for cancelling a probe. Idempotent: cancelling a finished probe is a no-op.")
class CancelProbeInput:
    probe: strawberry.ID


class PauseProbeInputModel(BaseModel):
    """Base model for pausing a probe."""

    probe: str = Field(description="The probe ID to pause")


@pydantic.input(PauseProbeInputModel, description="The input for pausing a probe. Idempotent: pausing a finished probe is a no-op.")
class PauseProbeInput:
    probe: strawberry.ID


class ResumeProbeInputModel(BaseModel):
    """Base model for resuming a paused probe."""

    probe: str = Field(description="The probe ID to resume")


@pydantic.input(ResumeProbeInputModel, description="The input for resuming a paused probe. Idempotent: resuming a finished probe is a no-op.")
class ResumeProbeInput:
    probe: strawberry.ID
