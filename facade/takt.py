"""What this server asks of takt (the agent protocol in Rust, ``takt/``).

Every assign, control, agent operation, probe, registration and drawer the GraphQL mutations,
schedules, triggers and the hub-service provisioning make goes to takt's internal API: takt
owns the agent sockets and is the one writer of task state, agents and their declarations.
Nothing is signed: takt serves the internal API on a listener only this server reaches
(``rekuest.takt_socket``, or the address in ``rekuest.takt_url``), and reaching it is the gate.
What each route is sent and answers is typed in :mod:`facade.takt_api`.

takt answers the in-process backends' refusals with their messages: ``400`` is their
``ValueError``, ``403`` their ``PermissionError``; either is raised as such here, so GraphQL
reports it exactly as before.

``rekuest.takt_url`` is required: without takt there is nothing to serve these.
"""

from __future__ import annotations

import logging

import httpx
from django.conf import settings

from facade import inputs, models, takt_api
from facade.caller_context import CallerContext
from facade.json_types import JSON
from facade.takt_api import Answer, Principal, Request, Route

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


def socket() -> str | None:
    """takt's internal listener as a unix socket (``rekuest.takt_socket``), when it is one."""
    return settings.TAKT_SOCKET


_client = httpx.Client(timeout=_TIMEOUT, transport=httpx.HTTPTransport(uds=socket()))


class TaktUnavailable(RuntimeError):
    """takt did not answer, or refused the request itself (not the operation)."""


def post(path: str, body: bytes) -> JSON:
    """POST ``body`` (JSON) to takt's ``internal/<path>``; its JSON answer, or the refusal raised."""
    base = settings.TAKT_URL
    if not base:
        raise TaktUnavailable("rekuest.takt_url is not configured: takt serves this")
    url = f"{base.rstrip('/')}/internal/{path}"
    try:
        response = _client.post(url, content=body, headers={"Content-Type": "application/json"})
    except httpx.HTTPError as error:
        raise TaktUnavailable(f"takt is unreachable at {url}: {error}") from error
    try:
        answer: JSON = response.json()
    except ValueError:
        answer = {"error": response.text}
    if response.status_code == 200:
        return answer
    refusal = answer.get("error") if isinstance(answer, dict) else answer
    message = str(refusal)
    if response.status_code == 400:
        raise ValueError(message)
    if response.status_code == 403:
        raise PermissionError(message)
    raise TaktUnavailable(f"takt refused internal/{path} ({response.status_code}): {message}")


def call[Req: Request, Ans: Answer](route: Route[Req, Ans], request: Req) -> Ans:
    """Ask takt for ``route``; its answer, or the refusal raised."""
    return route.answer.model_validate(post(route.path, request.body()))


def assign(context: CallerContext, input: inputs.AssignInputModel) -> models.Task:
    """The task for ``input``."""
    answer = call(takt_api.ASSIGN, takt_api.AssignRequest(principal=Principal.of(context), input=input))
    return models.Task.objects.get(pk=answer.task)


def resolve_dependencies(context: CallerContext, input: inputs.DependencyTreeInputModel) -> takt_api.ResolveAnswer:
    """A dry run of an assign's dependency tree: what it would bind, and what is unmet."""
    return call(takt_api.RESOLVE, takt_api.ResolveRequest(principal=Principal.of(context), input=input))


def _control(route: Route[takt_api.ControlRequest, takt_api.TaskAnswer], task: str, caller: models.Caller | None, step: bool | None = None) -> models.Task:
    principal = Principal.of_caller(caller) if caller is not None else None
    answer = call(route, takt_api.ControlRequest(task=task, principal=principal, step=step))
    return models.Task.objects.get(pk=answer.task)


def cancel(input: inputs.CancelInputModel, caller: models.Caller | None = None) -> models.Task:
    """Two-phase: CANCELLING now, CANCELLED when the agent confirms."""
    return _control(takt_api.CANCEL, input.task, caller)


def interrupt(input: inputs.InterruptInputModel, caller: models.Caller | None = None) -> models.Task:
    """Forceful: reaches every still-running descendant."""
    return _control(takt_api.INTERRUPT, input.task, caller)


def pause(input: inputs.PauseInputModel, caller: models.Caller | None = None) -> models.Task:
    """Ask the agent to pause the task."""
    return _control(takt_api.PAUSE, input.task, caller)


def resume(input: inputs.ResumeInputModel, caller: models.Caller | None = None) -> models.Task:
    """Resume a paused task (``step``: to the next breakpoint only)."""
    return _control(takt_api.RESUME, input.task, caller, step=input.step)


def _agent_op(route: Route[takt_api.AgentRequest, takt_api.AgentAnswer], context: CallerContext, agent: str, reason: str | None = None) -> models.Agent:
    answer = call(route, takt_api.AgentRequest(principal=Principal.of(context), agent=agent, reason=reason))
    return models.Agent.objects.get(pk=answer.agent)


def bounce(context: CallerContext, input: inputs.BounceInputModel) -> models.Agent:
    """Restart the agent's process."""
    return _agent_op(takt_api.BOUNCE, context, input.agent)


def kick(context: CallerContext, input: inputs.KickInputModel) -> models.Agent:
    """Close the agent's connection; it is told why."""
    return _agent_op(takt_api.KICK, context, input.agent, reason=input.reason or None)


def block(context: CallerContext, input: inputs.BlockInputModel) -> models.Agent:
    """Refuse the agent until it is unblocked."""
    return _agent_op(takt_api.BLOCK, context, input.agent, reason=input.reason or None)


def unblock(context: CallerContext, input: inputs.UnblockInputModel) -> models.Agent:
    """Let a blocked agent back in."""
    return _agent_op(takt_api.UNBLOCK, context, input.agent)


def collect(context: CallerContext, input: inputs.CollectInputModel) -> list[str]:
    """Drop the given drawers from their agents' shelves."""
    return call(takt_api.COLLECT, takt_api.CollectRequest(principal=Principal.of(context), drawers=input.drawers)).drawers


def probe(context: CallerContext, input: inputs.ProbeInputModel) -> takt_api.ProbeState:
    """Create and dispatch a probe; its state."""
    return call(takt_api.PROBE, takt_api.ProbeRequest(principal=Principal.of(context), input=input))


def _probe_control(route: Route[takt_api.ProbeControlRequest, takt_api.ProbeState], context: CallerContext, probe: str) -> takt_api.ProbeState:
    return call(route, takt_api.ProbeControlRequest(principal=Principal.of(context), probe=probe))


def cancel_probe(context: CallerContext, probe: str) -> takt_api.ProbeState:
    """Cancel a probe; its state."""
    return _probe_control(takt_api.PROBE_CANCEL, context, probe)


def pause_probe(context: CallerContext, probe: str) -> takt_api.ProbeState:
    """Pause a probe; its state."""
    return _probe_control(takt_api.PROBE_PAUSE, context, probe)


def resume_probe(context: CallerContext, probe: str) -> takt_api.ProbeState:
    """Resume a probe; its state."""
    return _probe_control(takt_api.PROBE_RESUME, context, probe)
