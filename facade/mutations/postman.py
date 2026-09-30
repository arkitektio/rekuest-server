import logging

from facade.backend import controll_backend, get_caller_for_context
from facade.caller_context import CallerContext
from facade import inputs, models, types
from kante.types import Info

logger = logging.getLogger(__name__)


def _caller(info: Info) -> models.Caller:
    """The requesting identity, recorded on the TaskInstruct audit row of a control op."""
    return get_caller_for_context(CallerContext.coerce(info))


def assign(info: Info, input: inputs.AssignInput) -> types.Task:
    model = input.to_pydantic()
    return controll_backend.assign(info, model)


def pause(info: Info, input: inputs.PauseInput) -> types.Task:
    return controll_backend.pause(input, caller=_caller(info))


def resume(info: Info, input: inputs.ResumeInput) -> types.Task:
    return controll_backend.resume(input, caller=_caller(info))


def cancel(info: Info, input: inputs.CancelInput) -> types.Task:
    return controll_backend.cancel(input, caller=_caller(info))


def interrupt(info: Info, input: inputs.InterruptInput) -> types.Task:
    return controll_backend.interrupt(input, caller=_caller(info))


def collect(info: Info, input: inputs.CollectInput) -> list[str]:
    return controll_backend.collect(info, input)


def bounce(info: Info, input: inputs.BounceInput) -> types.Agent:
    return controll_backend.bounce(info, input)


def kick(info: Info, input: inputs.KickInput) -> types.Agent:
    return controll_backend.kick(info, input)


def block(info: Info, input: inputs.BlockInput) -> types.Agent:
    return controll_backend.block(info, input)


def unblock(info: Info, input: inputs.UnblockInput) -> types.Agent:
    return controll_backend.unblock(info, input)
