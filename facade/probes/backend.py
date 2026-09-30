"""The mutation-side backend for probes: create and cancel.

Mirrors the postman ``RedisControllBackend`` shape but persists nothing: state goes to
the :mod:`facade.probes.store` redis hash, the agent receives a perfectly normal
``Assign`` message whose ``task`` is a probe id, and every event the agent reports back
is routed to :mod:`facade.probes.persist` by the id prefix.

Probes deliberately refuse everything that needs a Task row to be sound:
higher-order implementations (server-side orchestration writes wrapper rows),
implementations with declared dependencies (sub-assignment needs a parent task),
parents/hooks/capture. They are always provenance roots.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from django.utils import timezone

from facade import enums, messages, models
from facade.caller_context import CallerContext
from facade.probes.ids import new_probe_id
from facade.probes.store import ProbeStore, get_probe_store
from facade.channel_events import ProbeEventBroadcast
from facade.channels import probe_event_channel

logger = logging.getLogger(__name__)


def probe_events_topic(probe_id: str) -> str:
    return f"probe_events_{probe_id}"


class _ProbeMintShim:
    """Duck-types the Task surface :func:`mint_token_for_task` reads.

    A probe is always a provenance root (``parent_id=None``), so the mint path takes its
    no-DB-walk ``is_top`` branch and stamps ``tsk == rtk == probe_id``.
    """

    def __init__(self, probe_id: str, implementation: models.Implementation, agent: models.Agent, args: dict) -> None:
        self.pk = probe_id
        self.implementation = implementation
        self.agent = agent
        self.args = args
        self.parent_id = None


class ProbeBackend:
    def __init__(self, store: Optional[ProbeStore] = None) -> None:
        self._store = store

    @property
    def store(self) -> ProbeStore:
        return self._store or get_probe_store()

    def probe(self, principal: "CallerContext | Any", input: "Any", *, origin: str = "graphql") -> Dict[str, str]:
        """Create and dispatch a probe; returns the stored probe state (with id).

        ``input`` is a :class:`facade.inputs.ProbeInputModel`. ``origin`` marks who fired
        it ("graphql" | "agent"); agent-origin probes mirror their events onto the
        requester's caller topic (see :func:`facade.probes.persist.probe_topics`).
        """
        # Imported lazily: facade.backend pulls in the consumer stack.
        from facade.backend import get_caller_for_context, resolve_direct_target
        from facade.consumers.async_consumer import AgentConsumer
        from facade.provenance import mint_token_for_task

        ctx = CallerContext.coerce(principal)
        if ctx.organization is None:
            raise ValueError("Cannot probe without an organization")

        caller = get_caller_for_context(ctx)

        action, implementation, agent = resolve_direct_target(
            action_id=input.action,
            implementation_id=input.implementation,
            action_hash=input.action_hash,
            organization=ctx.organization,
        )

        if not action.allow_probe:
            raise ValueError(f"Action {action.name} does not allow probes — its author must declare allow_probe. Use assign.")
        if implementation.higher_order_for_id is not None:
            raise ValueError("Probes cannot target higher-order implementations — their orchestration needs persisted tasks. Use assign.")
        if models.Dependency.objects.filter(implementation=implementation).exists():
            raise ValueError("Probes cannot target implementations with dependencies — sub-assignment needs a parent task. Use assign.")

        if not self.store.try_acquire_slot(caller.pk):
            raise ValueError("Too many in-flight probes for this caller — cancel or await some, or raise PROBE_MAX_INFLIGHT_PER_CALLER.")

        probe_id = new_probe_id()
        try:
            token = mint_token_for_task(_ProbeMintShim(probe_id, implementation, agent, input.args or {}), ctx)
            state = self.store.create(
                probe_id,
                agent_pk=agent.pk,
                caller_pk=caller.pk,
                user_sub=str(ctx.user.sub),
                org_slug=str(ctx.organization.slug),
                action_pk=action.pk,
                implementation_pk=implementation.pk,
                interface=implementation.interface,
                reference=input.reference,
                origin=origin,
            )
            AgentConsumer.broadcast(
                agent,
                priority=True,
                message=messages.Assign(
                    task=probe_id,
                    probe=True,
                    args=input.args or {},
                    user=str(ctx.user.sub),
                    org=str(ctx.organization.slug),
                    reference=input.reference,
                    capture=False,
                    resolution=None,
                    interface=implementation.interface,
                    action=str(action.hash),
                    implementation=str(implementation.pk),
                    token=token,
                ),
            )
        except Exception:
            self.store.release_slot_sync(caller.pk)
            raise

        state["id"] = probe_id
        return state

    def _control(self, principal: "CallerContext | Any", probe_id: str, inging_kind: enums.TaskEventKind, to_agent_message: messages.Message, verb: str) -> Dict[str, str]:
        """The shared request phase of a two-phase probe control op (cancel/pause/resume).

        Idempotent hover semantics: controls race completion constantly, so a probe that
        is already terminal is NOT an error — the terminal state comes back and the
        caller moves on. The ``-ING`` state is recorded + published now; the agent's
        confirmation report (routed by id prefix to the probe handlers) settles it. The
        ToAgent frame rides the priority lane — hover-away must never queue behind a
        task backlog.
        """
        from facade.backend import get_caller_for_context
        from facade.consumers.async_consumer import AgentConsumer
        from facade.probes.persist import probe_topics

        ctx = CallerContext.coerce(principal)
        caller = get_caller_for_context(ctx)

        state = self.store.get(probe_id)
        if state is None:
            raise ValueError(f"Unknown or expired probe {probe_id}")
        if state.get("caller") != str(caller.pk):
            raise PermissionError(f"Not authorized to {verb} this probe (not its caller).")
        if state.get("done"):
            state["id"] = probe_id
            return state  # already terminal — the control is a no-op, not an error

        recorded = self.store.record_nonterminal_sync(probe_id, inging_kind.value)
        if recorded is not None:
            seq, event_caller, origin = recorded
            probe_event_channel.broadcast(
                ProbeEventBroadcast(
                    probe=probe_id,
                    kind=inging_kind.value,
                    seq=seq,
                    created_at=timezone.now(),
                ),
                probe_topics(probe_id, event_caller, origin),
            )
        AgentConsumer.broadcast(int(state["agent"]), to_agent_message, priority=True)

        state = self.store.get(probe_id) or state
        state["id"] = probe_id
        return state

    def cancel(self, principal: "CallerContext | Any", probe_id: str) -> Dict[str, str]:
        return self._control(principal, probe_id, enums.TaskEventKind.CANCELLING, messages.Cancel(task=probe_id), "cancel")

    def pause(self, principal: "CallerContext | Any", probe_id: str) -> Dict[str, str]:
        return self._control(principal, probe_id, enums.TaskEventKind.PAUSING, messages.Pause(task=probe_id), "pause")

    def resume(self, principal: "CallerContext | Any", probe_id: str) -> Dict[str, str]:
        return self._control(principal, probe_id, enums.TaskEventKind.RESUMING, messages.Resume(task=probe_id, step=False), "resume")

    def probe_for_agent(self, agent_id: int, message: "messages.ProbeRequest") -> Dict[str, str]:
        """A probe fired by an agent over the socket (``ProbeRequest``).

        Resolves the agent's identity to the same caller shape the GraphQL path uses
        (mirroring ``persist_backend._caller_assign_sync``), then delegates to
        :meth:`probe` with ``origin="agent"`` so every event mirrors back onto the
        requester's ``task_caller_{caller}`` topic. All guards (allow_probe,
        higher-order, dependencies, the inflight cap — keyed by the agent's caller)
        apply unchanged.
        """
        from facade import inputs
        from facade.provenance import principal as provenance_principal

        agent = models.Agent.objects.select_related("user", "client", "organization").get(id=agent_id)
        caller, _ = models.Caller.objects.get_or_create(client=agent.client, user=agent.user, organization=agent.organization)
        ctx = CallerContext.from_agent(agent, roles=provenance_principal.roles_for_caller(caller))

        return self.probe(
            ctx,
            inputs.ProbeInputModel(
                action=message.action,
                action_hash=message.action_hash,
                implementation=message.implementation,
                args=message.args,
                reference=message.reference,
            ),
            origin="agent",
        )


probe_backend = ProbeBackend()


def _select_graphql_backend() -> "ProbeBackend":
    """What the GraphQL probe mutations use: agentd's internal API when ``rekuest.agentd_url`` is
    set. The socket path keeps :data:`probe_backend` (``probe_for_agent``) until it is deleted."""
    from facade import agentd

    return agentd.AgentdProbeBackend() if agentd.enabled() else probe_backend  # type: ignore[return-value]


graphql_probe_backend = _select_graphql_backend()
