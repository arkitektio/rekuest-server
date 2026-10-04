"""The Implementation GraphQL type."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, cast

import strawberry
import strawberry_django
from django.db.models import QuerySet
from kante.types import Info

from facade import filters, models
from facade.types.base import build_prescoped_queryset, row_of
from rekuest_core import enums as renums
from rekuest_core import scalars as rscalars
from rekuest_core.objects import models as rmodels
from rekuest_core.objects import types as rtypes

if TYPE_CHECKING:
    # Named in annotations only: strawberry resolves them when it builds the schema.
    from facade.types.action import Action
    from facade.types.agent import Agent, Lock
    from facade.types.dependency import Dependency, Resolution
    from facade.types.state import State
    from facade.types.task import Task


@strawberry_django.type(models.Implementation, filters=filters.ImplementationFilter, ordering=filters.ImplementationOrder, pagination=True, description="Represents a concrete implementation of an action.")
class Implementation:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the implementation.")
    interface: str = strawberry_django.field(description="Interface string representing the implementation entrypoint.")
    agent: "Agent" = strawberry_django.field(description="Agent running this implementation.")
    action: "Action" = strawberry_django.field(description="The action this implements.")
    params: rscalars.AnyDefault = strawberry_django.field(description="Arbitrary parameters for the implementation.")
    locks: list["Lock"] = strawberry_django.field(field_name="required_locks", description="The agent's locks this implementation takes while it runs.")
    resolutions: list["Resolution"] = strawberry_django.field(description="The resolved dependencies")
    dependencies: list["Dependency"] = strawberry_django.field(description="Dependencies required by this action.")
    manipulates: list["State"] = strawberry.field(description="States that this implementation manipulates.")
    higher_order_for: Optional["Implementation"] = strawberry_django.field(description="If this is a higher-order (wrapper) implementation, the lower implementation it wraps.")
    lower_order_implementations: list["Implementation"] = strawberry_django.field(description="The higher-order implementations that wrap this implementation.")
    higher_order_config: rscalars.AnyDefault = strawberry_django.field(description="Projection config (bound params, arg/dependency/return maps) when this is a higher-order implementation.")
    needs_token: bool = strawberry_django.field(description="Whether a signed provenance token is minted when this implementation is assigned.")
    provenance_audience: Optional[list[str]] = strawberry_django.field(description="Declared audience for the provenance token's `aud`, or null to derive it at dispatch.")
    effects: renums.Effects = strawberry_django.field(description="What running this implementation again would do to the world. Informational: shown to whoever decides about a lost task.")
    execution: renums.Execution = strawberry_django.field(description="How this implementation runs: a WORKFLOW may call other actions and is resumed from its journal when its agent dies.")
    code_hash: Optional[str] = strawberry_django.field(description="A hash of the implementation's code; a workflow is only resumed by an implementation with the same hash.")

    @strawberry_django.field(description="Constructed name for display, combining interface and agent name.")
    def name(self) -> str:
        return self.interface + "@" + self.agent.name

    @strawberry_django.field(description="Implementations on this agent whose action is a test for this implementation's action.")
    def tests(self, info: Info) -> list["Implementation"]:
        row = row_of(self, models.Implementation)
        return cast("list[Implementation]", list(models.Implementation.objects.filter(agent=row.agent, action__in=row.action.tests.all())))

    @strawberry_django.field(description="List of action demands")
    def tracks(self) -> list[rtypes.Track]:
        return cast("list[rtypes.Track]", [rmodels.TrackModel(**i) for i in self.tracks])

    @strawberry_django.field(description="Non-fatal registration findings, e.g. validator/effect calls naming operations that neither the base catalog nor the definition's catalog provides.")
    def diagnostics(self) -> list[rtypes.Diagnostic]:
        return cast("list[rtypes.Diagnostic]", [rmodels.DiagnosticModel(**i) for i in self.diagnostics])

    @strawberry_django.field(description="Get the latest completed task created by the current user.")
    def my_latest_task(self, info: Info) -> Optional["Task"]:
        row = row_of(self, models.Implementation)
        user = info.context.request.user
        return cast(
            "Optional[Task]",
            (
                row.tasks.filter(
                    implementation=row.pk,
                    is_done=True,
                    caller__user=user,
                )
                .order_by("-created_at")
                .first()
            ),
        )

    @classmethod
    def get_queryset(cls, queryset: QuerySet[models.Implementation], info: Info, **kwargs: object) -> QuerySet[models.Implementation]:
        return build_prescoped_queryset(info, queryset, field="action__organization")
