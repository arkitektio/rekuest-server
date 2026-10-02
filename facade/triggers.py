"""What a trigger is checked against when it is written (see :mod:`facade.models.signal`).

Matching signals to triggers and firing them is takt's (its reaper's ``triggers`` sweep). This
server owns the trigger rows: its GraphQL creates them, and refuses one whose port does not take
the structure it reacts to or whose conditions do not compile, so a broken trigger is refused
once instead of failing on every signal.
"""

from __future__ import annotations

from typing import Any

from facade import models
from facade.descriptors import compile_descriptors_to_jsonpath


def compile_conditions(conditions: list[Any]) -> tuple[list[dict], str | None]:
    """``(stored conditions, compiled JSONPath)``; raises ``ValueError`` on a malformed one."""
    from rekuest_core.inputs.models import RequiresInputModel

    parsed = [c if isinstance(c, RequiresInputModel) else RequiresInputModel.model_validate(c) for c in conditions or []]
    return [c.model_dump(mode="json") for c in parsed], compile_descriptors_to_jsonpath(parsed)


def target_port(action: models.Action, port: str, identifier: str) -> models.ArgPort:
    """The top-level STRUCTURE arg ``port`` of ``action`` taking ``identifier``, or ``ValueError``."""
    found = models.ArgPort.objects.filter(action=action, parent__isnull=True, key=port).first()
    if found is None:
        raise ValueError(f"The action has no argument {port!r}")
    if found.kind != "STRUCTURE" or found.identifier != identifier:
        raise ValueError(f"Argument {port!r} takes {found.kind} {found.identifier or ''}, not a {identifier} structure")
    return found
