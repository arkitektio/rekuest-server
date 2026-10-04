import logging
from typing import cast

from kante.types import Info

from facade import inputs, models, takt, types
from facade.caller_context import CallerContext

logger = logging.getLogger(__name__)


def _caller(info: Info) -> models.Caller:
    """The requesting identity, recorded on the TaskInstruct audit row of a control op."""
    return CallerContext.from_info(info).caller()


def assign(info: Info, input: inputs.AssignInput) -> types.Task:
    return cast("types.Task", takt.assign(CallerContext.from_info(info), input.to_pydantic()))


def pause(info: Info, input: inputs.PauseInput) -> types.Task:
    return cast("types.Task", takt.pause(input.to_pydantic(), caller=_caller(info)))


def resume(info: Info, input: inputs.ResumeInput) -> types.Task:
    return cast("types.Task", takt.resume(input.to_pydantic(), caller=_caller(info)))


def cancel(info: Info, input: inputs.CancelInput) -> types.Task:
    return cast("types.Task", takt.cancel(input.to_pydantic(), caller=_caller(info)))


def interrupt(info: Info, input: inputs.InterruptInput) -> types.Task:
    return cast("types.Task", takt.interrupt(input.to_pydantic(), caller=_caller(info)))


def collect(info: Info, input: inputs.CollectInput) -> list[str]:
    return takt.collect(CallerContext.from_info(info), input.to_pydantic())


def bounce(info: Info, input: inputs.BounceInput) -> types.Agent:
    return cast("types.Agent", takt.bounce(CallerContext.from_info(info), input.to_pydantic()))


def kick(info: Info, input: inputs.KickInput) -> types.Agent:
    return cast("types.Agent", takt.kick(CallerContext.from_info(info), input.to_pydantic()))


def block(info: Info, input: inputs.BlockInput) -> types.Agent:
    return cast("types.Agent", takt.block(CallerContext.from_info(info), input.to_pydantic()))


def unblock(info: Info, input: inputs.UnblockInput) -> types.Agent:
    return cast("types.Agent", takt.unblock(CallerContext.from_info(info), input.to_pydantic()))
