"""Wiregram mutations: import, delete, export (see :mod:`facade.wiregrams`)."""

import strawberry
from kante.types import Info
from rekuest_core import scalars as rscalars

from facade import inputs, models, types, wiregrams
from facade.backend import get_caller_for_context
from facade.caller_context import CallerContext
from facade.types.base import scoped_get


def import_wiregram(info: Info, input: inputs.WiregramInput) -> types.Wiregram:
    """Import a wiregram: create its schedules and triggers, or bring an earlier import of the same key in line with it."""
    return wiregrams.import_wiregram(input.to_pydantic(), get_caller_for_context(CallerContext.coerce(info)), principal=info)


def delete_wiregram(info: Info, input: inputs.WiregramIdInput) -> strawberry.ID:
    """Delete a wiregram and the rules it owns. Waiting runs are cancelled; the history of runs is kept."""
    wiregrams.delete_wiregram(scoped_get(models.Wiregram, info, input.id), principal=info)
    return input.id


def export_wiregram(info: Info, input: inputs.ExportWiregramInput) -> rscalars.AnyDefault:
    """Write existing schedules and triggers down as a wiregram document. Changes nothing."""
    listed_schedules = [scoped_get(models.Schedule, info, id, field="caller__organization") for id in input.schedules or []]
    listed_triggers = [scoped_get(models.Trigger, info, id, field="caller__organization") for id in input.triggers or []]
    return wiregrams.export_wiregram(input.key, input.name, input.description, listed_schedules, listed_triggers)
