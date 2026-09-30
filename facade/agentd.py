"""The control backend, served by agentd (the agent protocol in Rust, ``rekuest-agentd``).

Every assign, control, agent operation, probe, registration and drawer the GraphQL mutations,
schedules, triggers and the hub-service provisioning make goes to agentd's internal API: agentd
owns the agent sockets and is the one writer of task state, agents and their declarations. The request is signed with this instance's key as a service token from
rekuest to itself (agentd reads the same ``config.yaml``, so it holds the same key); the JSON
contract is documented at the top of ``rekuest-agentd/crates/rekuest-server/src/internal.rs``.

agentd answers the in-process backends' refusals with their messages: ``400`` is their
``ValueError``, ``403`` their ``PermissionError``; either is raised as such here, so GraphQL
reports it exactly as before.

``rekuest.agentd_url`` is required: without agentd there is nothing to serve these.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict
from urllib.parse import urlparse

import httpx
from django.conf import settings

from facade import models
from facade.caller_context import CallerContext

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_client = httpx.Client(timeout=_TIMEOUT)


class AgentdUnavailable(RuntimeError):
    """agentd did not answer, or refused the request itself (not the operation)."""


def _principal(value: "CallerContext | models.Caller | Any") -> Dict[str, Any]:
    """The requesting identity as agentd's ``principal``: primary keys and roles."""
    if isinstance(value, models.Caller):
        return {"user": value.user_id, "client": value.client_id, "organization": value.organization_id, "roles": []}
    ctx = CallerContext.coerce(value)
    return {
        "user": ctx.user.pk,
        "client": getattr(ctx.client, "pk", None),
        "organization": getattr(ctx.organization, "pk", None),
        "roles": list(ctx.roles or []),
    }


def call(op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST ``payload`` to agentd's ``internal/<op>``; its JSON answer, or the refusal raised."""
    from facade.service_trust import rekuest_identifier
    from rekuest_service import trust

    base = getattr(settings, "AGENTD_URL", None)
    if not base:
        raise AgentdUnavailable("rekuest.agentd_url is not configured: agentd serves this")
    url = f"{base.rstrip('/')}/internal/{op}"
    body = json.dumps(payload).encode("utf-8")
    identity = rekuest_identifier()
    authorization = trust.sign("POST", urlparse(url).path, body, issuer=identity, audience=identity)
    try:
        response = _client.post(url, content=body, headers={"Content-Type": "application/json", "Authorization": authorization})
    except httpx.HTTPError as error:
        raise AgentdUnavailable(f"agentd is unreachable at {url}: {error}") from error
    try:
        answer = response.json()
    except ValueError:
        answer = {"error": response.text}
    if response.status_code == 200:
        return answer
    message = answer.get("error") if isinstance(answer, dict) else str(answer)
    if response.status_code == 400:
        raise ValueError(message)
    if response.status_code == 403:
        raise PermissionError(message)
    raise AgentdUnavailable(f"agentd refused internal/{op} ({response.status_code}): {message}")


def _dump(model: Any) -> Dict[str, Any]:
    """A pydantic input (or a strawberry one converted to it) as the JSON agentd reads."""
    if hasattr(model, "to_pydantic"):
        model = model.to_pydantic()
    return model.model_dump(mode="json", exclude_none=True)


class AgentdControllBackend:
    """Assigns, controls and agent operations, in agentd."""

    def assign(self, principal: Any, input: Any) -> models.Task:
        """The task for ``input``."""
        return self.assign_with_status(principal, input)[0]

    def assign_with_status(
        self,
        principal: Any,
        input: Any,
        *,
        schedule: "models.Schedule | None" = None,
        signal: "models.Signal | None" = None,
        trigger: "models.Trigger | None" = None,
        trigger_depth: int = 0,
    ) -> tuple[models.Task, bool]:
        """``(task, created)``; ``created`` is False for a resend of a known reference."""
        payload: Dict[str, Any] = {"principal": _principal(principal), "input": _dump(input)}
        if schedule is not None:
            payload["schedule"] = schedule.pk
        if signal is not None:
            payload["signal"] = signal.pk
        if trigger is not None:
            payload["trigger"] = trigger.pk
        if trigger_depth:
            payload["trigger_depth"] = trigger_depth
        answer = call("assign", payload)
        return models.Task.objects.get(pk=answer["task"]), bool(answer["created"])

    def _control(self, op: str, input: Any, caller: "models.Caller | None", **extra: Any) -> models.Task:
        payload: Dict[str, Any] = {"task": str(input.task), **extra}
        if caller is not None:
            payload["principal"] = _principal(caller)
        answer = call(op, payload)
        return models.Task.objects.get(pk=answer["task"])

    def cancel(self, input: Any, caller: "models.Caller | None" = None) -> models.Task:
        """Two-phase: CANCELLING now, CANCELLED when the agent confirms."""
        return self._control("cancel", input, caller)

    def interrupt(self, input: Any, caller: "models.Caller | None" = None) -> models.Task:
        """Forceful: reaches every still-running descendant."""
        return self._control("interrupt", input, caller)

    def pause(self, input: Any, caller: "models.Caller | None" = None) -> models.Task:
        """Ask the agent to pause the task."""
        return self._control("pause", input, caller)

    def resume(self, input: Any, caller: "models.Caller | None" = None) -> models.Task:
        """Resume a paused task (``step``: to the next breakpoint only)."""
        return self._control("resume", input, caller, step=bool(getattr(input, "step", False)))

    def _agent_op(self, op: str, info: Any, input: Any, **extra: Any) -> models.Agent:
        answer = call(op, {"principal": _principal(info), "agent": str(input.agent), **extra})
        return models.Agent.objects.get(pk=answer["agent"])

    def bounce(self, info: Any, input: Any) -> models.Agent:
        """Restart the agent's process."""
        return self._agent_op("bounce", info, input)

    def kick(self, info: Any, input: Any) -> models.Agent:
        """Close the agent's connection."""
        return self._agent_op("kick", info, input)

    def block(self, info: Any, input: Any) -> models.Agent:
        """Refuse the agent until it is unblocked."""
        return self._agent_op("block", info, input, **({"reason": input.reason} if getattr(input, "reason", None) else {}))

    def unblock(self, info: Any, input: Any) -> models.Agent:
        """Let a blocked agent back in."""
        return self._agent_op("unblock", info, input)

    def collect(self, info: Any, input: Any) -> list[str]:
        """Drop the given drawers from their agents' shelves."""
        answer = call("collect", {"principal": _principal(info), "drawers": [str(d) for d in input.drawers]})
        return [str(d) for d in answer["drawers"]]


class AgentdProbeBackend:
    """Probes and their controls, in agentd."""

    def probe(self, principal: Any, input: Any) -> Dict[str, str]:
        """Create and dispatch a probe; its state, with ``id``."""
        return call("probe", {"principal": _principal(principal), "input": _dump(input)})

    def _control(self, op: str, principal: Any, probe_id: str) -> Dict[str, str]:
        return call(f"probe/{op}", {"principal": _principal(principal), "probe": str(probe_id)})

    def cancel(self, principal: Any, probe_id: str) -> Dict[str, str]:
        """Cancel a probe; its state."""
        return self._control("cancel", principal, probe_id)

    def pause(self, principal: Any, probe_id: str) -> Dict[str, str]:
        """Pause a probe; its state."""
        return self._control("pause", principal, probe_id)

    def resume(self, principal: Any, probe_id: str) -> Dict[str, str]:
        """Resume a probe; its state."""
        return self._control("resume", principal, probe_id)
