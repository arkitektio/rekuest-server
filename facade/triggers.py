"""What a trigger is checked against when it is written (see :mod:`facade.models.signal`).

Matching signals to triggers and firing them is takt's (its reaper's ``triggers`` sweep). This
server owns the trigger rows: its GraphQL creates them, and refuses one whose port does not take
the structure it reacts to or whose conditions do not compile, so a broken trigger is refused
once instead of failing on every signal.
"""

from __future__ import annotations

from authentikate.models import Organization
from django.db.models import QuerySet
from pydantic import TypeAdapter

from facade import models, rules, takt, takt_api
from facade.caller_context import CallerContext
from facade.descriptors import compile_descriptors_to_jsonpath
from facade.json_types import JSON, JSONObject
from facade.takt_api import Principal
from rekuest_core.inputs.models import RequiresInputModel
from rekuest_core.objects.models import ArgPortModel
from rekuest_core.values import validate_assignment_args

_CONDITIONS = TypeAdapter(list[RequiresInputModel])


def compile_conditions(conditions: JSON | list[RequiresInputModel]) -> tuple[list[RequiresInputModel], str | None]:
    """``(the conditions, their compiled JSONPath)``; raises ``ValueError`` on a malformed one.

    Conditions arrive as a document's own models, or as JSON (a GraphQL argument, a stored row).
    """
    parsed = _CONDITIONS.validate_python(conditions or [])
    return parsed, compile_descriptors_to_jsonpath(parsed)


def target_port(action: models.Action, port: str, identifier: str) -> models.ArgPort:
    """The top-level STRUCTURE arg ``port`` of ``action`` taking ``identifier``, or ``ValueError``."""
    found = models.ArgPort.objects.filter(action=action, parent__isnull=True, key=port).first()
    if found is None:
        raise ValueError(f"The action has no argument {port!r}")
    if found.kind != "STRUCTURE" or found.identifier != identifier:
        raise ValueError(f"Argument {port!r} takes {found.kind} {found.identifier or ''}, not a {identifier} structure")
    return found


def check_args(action: models.Action, port: str, identifier: str, args: dict[str, JSON]) -> None:
    """The fixed args plus a stand-in object in ``port`` must fit the action's ports."""
    if action.args:
        validate_assignment_args([ArgPortModel(**p) for p in action.args], {**args, port: {"__identifier": identifier, "object": "0"}})


def check_declared(kind: str, identifier: str, conditions: list[RequiresInputModel]) -> None:
    """The trigger waits for something a service declares it emits, on keys that service sends."""
    declarations = list(models.SignalDeclaration.objects.filter(identifier=identifier))
    matching = [d for d in declarations if d.kind == kind]
    if not matching:
        if declarations:
            kinds = sorted({d.kind for d in declarations})
            raise ValueError(f"No service emits {kind} for {identifier}; declared: {', '.join(kinds)}")
        raise ValueError(f"No service declares signals for {identifier}")
    keys = {key for d in matching for key in d.descriptor_keys}
    unknown = sorted({condition.key for condition in conditions} - keys)
    if unknown:
        raise ValueError(f"No service sends the descriptor(s) {', '.join(unknown)} for {kind} {identifier}; sent: {', '.join(sorted(keys)) or 'none'}")


def validate(*, action: models.Action, agent: models.Agent | None, interface: str | None, kind: str, identifier: str, port: str, args: dict[str, JSON], conditions: JSON | list[RequiresInputModel]) -> tuple[list[JSONObject], str | None]:
    """Everything a trigger must satisfy to be written; ``(stored conditions, compiled JSONPath)``.

    One place, used when a trigger is created, changed or imported: its pin is an implementation
    of the action, its port takes the structure it reacts to, its args fit, its conditions compile
    and test keys a service really sends.
    """
    rules.check_pin(action, agent, interface)
    target_port(action, port, identifier)
    check_args(action, port, identifier, args)
    parsed, compiled = compile_conditions(conditions)
    check_declared(kind, identifier, parsed)
    rules.check_provenance(action, agent, interface, "This action needs a provenance token; under strict provenance a trigger can only feed it signals caused by a task, which cannot be guaranteed")
    return [condition.model_dump(mode="json") for condition in parsed], compiled


#: How takt tests a signal's descriptors against a compiled path (its ``MATCHING``): silent, so
#: a path that errors on these descriptors is simply not a match.
_PATH_MATCHES = "jsonb_path_match(descriptors, %s::jsonpath, '{}'::jsonb, true) IS TRUE"


def matching_signals(organization: Organization | int, kind: str, identifier: str, paths: list[str | None], limit: int = 20) -> QuerySet[models.Signal]:
    """The organization's stored signals a rule with these compiled paths would have matched, newest first.

    A dry run over what is still retained: it reads the same rows and applies the same test as
    takt's sweep, and fires nothing.
    """
    signals = models.Signal.objects.filter(organization=organization, kind=kind, identifier=identifier)
    for path in paths:
        if path:
            signals = signals.extra(where=[_PATH_MATCHES], params=[path])
    return signals.order_by("-received_at")[: max(0, min(limit, 200))]


def fire(trigger: models.Trigger, signal: models.Signal, principal: CallerContext) -> models.Firing:
    """Fire ``trigger`` on a stored ``signal`` by hand: a replay.

    takt creates the run whether or not the signal satisfies the trigger and whatever its
    policies say (that is what a replay is for), and logs it as a firing of its own.
    """
    answer = takt.call(takt_api.FIRE_TRIGGER, takt_api.FireTriggerRequest(principal=Principal.of(principal), trigger=str(trigger.pk), signal=str(signal.pk)))
    return models.Firing.objects.get(pk=answer.firing)
