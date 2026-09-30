"""Mutations for probes."""

import logging

import strawberry

from facade import inputs, types
from facade.agentd import AgentdProbeBackend

probe_backend = AgentdProbeBackend()
from kante.types import Info

logger = logging.getLogger(__name__)


def probe(info: Info, input: inputs.ProbeInput) -> types.Probe:
    model = input.to_pydantic()
    state = probe_backend.probe(info, model)
    return types.Probe.from_state(state)


def cancel_probe(info: Info, input: inputs.CancelProbeInput) -> types.Probe:
    model = input.to_pydantic()
    state = probe_backend.cancel(info, model.probe)
    return types.Probe.from_state(state)


def pause_probe(info: Info, input: inputs.PauseProbeInput) -> types.Probe:
    model = input.to_pydantic()
    state = probe_backend.pause(info, model.probe)
    return types.Probe.from_state(state)


def resume_probe(info: Info, input: inputs.ResumeProbeInput) -> types.Probe:
    model = input.to_pydantic()
    state = probe_backend.resume(info, model.probe)
    return types.Probe.from_state(state)
