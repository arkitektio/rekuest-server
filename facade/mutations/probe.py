"""Mutations for probes."""

import logging

from kante.types import Info

from facade import inputs, takt, types
from facade.caller_context import CallerContext

logger = logging.getLogger(__name__)


def probe(info: Info, input: inputs.ProbeInput) -> types.Probe:
    return types.Probe.from_state(takt.probe(CallerContext.from_info(info), input.to_pydantic()))


def cancel_probe(info: Info, input: inputs.CancelProbeInput) -> types.Probe:
    return types.Probe.from_state(takt.cancel_probe(CallerContext.from_info(info), input.to_pydantic().probe))


def pause_probe(info: Info, input: inputs.PauseProbeInput) -> types.Probe:
    return types.Probe.from_state(takt.pause_probe(CallerContext.from_info(info), input.to_pydantic().probe))


def resume_probe(info: Info, input: inputs.ResumeProbeInput) -> types.Probe:
    return types.Probe.from_state(takt.resume_probe(CallerContext.from_info(info), input.to_pydantic().probe))
