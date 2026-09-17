"""Work an agent originates over its own socket, as a *caller* rather than an executor.

An agent may assign dependent work and control what it assigned. Roots are not available here:
they must trace to an accountable human, so they come only from the GraphQL ``assign`` mutation
(see the human-root invariant in :mod:`facade.provenance`).

These delegate to the postman backend, which is why this is the module that carries the lazy
``facade.backend`` import: ``backend`` → ``async_consumer`` → ``agent_protocol`` → here.
"""

import logging
from datetime import timedelta
from typing import Tuple

from channels.db import database_sync_to_async
from django.utils import timezone

from facade import inputs, models, messages

logger = logging.getLogger(__name__)


class CallerOpsMixin:
    @staticmethod
    def _agent_and_caller_sync(agent_id: int) -> Tuple[models.Agent, models.Caller]:
        """An agent and the durable ``Caller`` row for its own identity.

        The ``(client, user, organization)`` triple is a correctness-bearing key — it decides which
        realtime topics the work is published to — and it was spelled out at three call sites.
        Returns the agent too: two of those sites need it for the ``CallerContext``.
        """
        agent = models.Agent.objects.select_related("user", "client", "organization").get(id=agent_id)
        caller, _ = models.Caller.objects.get_or_create(client=agent.client, user=agent.user, organization=agent.organization)
        return agent, caller

    async def get_or_create_caller_id(self, agent_id: int) -> str:
        """The durable ``Caller`` id for an agent's identity (user/client/organization).

        A connection joins ``task_caller_{caller_id}`` to receive the events of work it
        originated. Mirrors ``get_caller_for_context`` (``facade/backend.py``) but resolves
        the identity from the agent instead of a GraphQL request.
        """
        _, caller = await database_sync_to_async(self._agent_and_caller_sync)(agent_id)
        return str(caller.pk)

    async def on_caller_assign(
        self,
        agent_id: int,
        message: messages.AssignRequest,
        connection_id: str | None = None,
        session_id: str | None = None,
    ) -> Tuple[models.Task, bool]:
        """Assign *dependent* work requested by an agent over the socket.

        Idempotent on ``(caller, reference)`` and durable-before-return: a resend of the same
        ``reference`` returns the existing task with ``created=False`` rather than creating a
        duplicate. Raises ``PermissionError`` for a parentless (root) assign — roots must trace
        to an accountable human, so they originate solely from the GraphQL ``assign`` mutation
        (see the human-root invariant in ``facade.provenance``). Runs the sync postman backend
        off the event loop.
        """
        return await database_sync_to_async(self._caller_assign_sync)(agent_id, message, connection_id, session_id)

    def _caller_assign_sync(
        self,
        agent_id: int,
        message: messages.AssignRequest,
        connection_id: str | None = None,
        session_id: str | None = None,
    ) -> Tuple[models.Task, bool]:
        # Imported lazily: facade.backend → async_consumer → agent_protocol → persist_backend
        # would otherwise be a circular import at module load.
        from facade.backend import controll_backend
        from facade.caller_context import CallerContext
        from facade.provenance import principal

        agent, caller = self._agent_and_caller_sync(agent_id)

        # Idempotency: a resend of the same reference returns the existing task.
        existing = models.Task.objects.filter(caller=caller, reference=message.reference).first()
        if existing is not None:
            return existing, False

        if message.parent is None:
            raise PermissionError("An agent may only assign dependent work: 'parent' is required. Root tasks originate from the GraphQL assign mutation, where the initiator is an accountable human.")

        ctx = CallerContext.from_agent(agent, roles=principal.roles_for_caller(caller))
        hooks = [inputs.HookInputModel(**h) for h in message.hooks] if message.hooks else None
        assign_input = inputs.AssignInputModel(
            reference=message.reference,
            args=message.args,
            action=message.action,
            action_hash=message.action_hash,
            implementation=message.implementation,
            agent=message.agent,
            interface=message.interface,
            parent=message.parent,
            dependency=message.dependency,
            method=message.method,
            resolution=message.resolution,
            hooks=hooks,
            capture=message.capture,
            step=message.step,
        )
        # A dependent task's fate follows its parent, so nothing about this connection needs
        # recording on the row: if this agent dies, the executor-death cascade covers its work,
        # and if the parent's tree is cancelled the child goes with it.
        # ``created`` is the backend's verdict, not an assumption: a resend racing the original on
        # another backend loses the unique constraint and must report ``created=False``.
        return controll_backend.assign_with_status(ctx, assign_input)

    def _caller_control_sync(self, agent_id: int, task_id: str, op: str, *, step: bool = False) -> models.Task:
        """Ownership-check then dispatch a control op on the sync postman backend.

        A caller may only control tasks whose ``caller`` is its own identity. Raises
        ``Task.DoesNotExist`` (unknown), ``PermissionError`` (not the caller), or
        ``ValueError`` (already terminal — from the postman backend).
        """
        from facade import inputs
        from facade.backend import controll_backend

        _, caller = self._agent_and_caller_sync(agent_id)
        task = models.Task.objects.get(id=task_id)
        if task.caller_id != caller.pk:
            raise PermissionError("Not authorized to control this task (not its caller).")

        ref = str(task_id)
        ops = {
            "cancel": lambda: controll_backend.cancel(inputs.CancelInputModel(task=ref), caller=caller),
            "interrupt": lambda: controll_backend.interrupt(inputs.InterruptInputModel(task=ref), caller=caller),
            "pause": lambda: controll_backend.pause(inputs.PauseInputModel(task=ref), caller=caller),
            "resume": lambda: controll_backend.resume(inputs.ResumeInputModel(task=ref, step=step), caller=caller),
        }
        return ops[op]()

    async def on_caller_cancel(self, agent_id: int, message: messages.CancelRequest, *, connection_id: str | None = None, session_id: str | None = None) -> models.Task:
        task = await database_sync_to_async(self._caller_control_sync)(agent_id, message.task, "cancel")
        if message.auto_interrupt is not None:
            # The escalation deadline is a column, not a timer: ``escalate_due_controls`` (the
            # reaper sweep, on any backend) fires it. Wins over the global control deadline.
            await models.Task.objects.filter(pk=task.pk, is_done=False).aupdate(interrupt_at=timezone.now() + timedelta(seconds=float(message.auto_interrupt)))
        return task

    async def on_caller_interrupt(self, agent_id: int, message: messages.InterruptRequest, *, connection_id: str | None = None, session_id: str | None = None) -> models.Task:
        return await database_sync_to_async(self._caller_control_sync)(agent_id, message.task, "interrupt")

    async def on_caller_pause(self, agent_id: int, message: messages.PauseRequest, *, connection_id: str | None = None, session_id: str | None = None) -> models.Task:
        return await database_sync_to_async(self._caller_control_sync)(agent_id, message.task, "pause")

    async def on_caller_resume(self, agent_id: int, message: messages.ResumeRequest, *, connection_id: str | None = None, session_id: str | None = None) -> models.Task:
        return await database_sync_to_async(self._caller_control_sync)(agent_id, message.task, "resume", step=message.step)

    async def _escalate_to_interrupt(self, task_id: str | int) -> None:
        """A cancel's deadline passed unconfirmed: escalate it to an interrupt. Idempotent."""
        from facade import inputs
        from facade.backend import controll_backend

        def _do() -> None:
            task = models.Task.objects.get(id=task_id)
            if task.is_done:
                return  # the cancel confirmed (or otherwise terminal) before the window — no-op
            controll_backend.interrupt(inputs.InterruptInputModel(task=str(task_id)))

        try:
            await database_sync_to_async(_do)()
        except models.Task.DoesNotExist:
            return
