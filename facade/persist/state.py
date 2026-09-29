"""An agent's reported state: patches, snapshots, sessions and lock reports.

Plain persistence, deliberately separate from the task machinery — none of it touches a task, a
claim or a lease. Note the lock rows are a *report* of what the agent's own in-process lock is
doing, not a grant: the server never hands out a lock.
"""

import logging

from django.db import IntegrityError

from facade import models, messages
from facade.persist.positions import position_stamp
from facade.probes.ids import is_probe_id

logger = logging.getLogger(__name__)


class AgentStateMixin:
    async def on_agent_state_patch(self, agent_id: int, message: messages.StatePatch) -> None:
        logger.debug("Patch for state %s", message.state_name)

        state = await models.State.objects.aget(agent_id=agent_id, interface=message.state_name)
        session, _ = await models.Session.objects.aget_or_create(agent_id=agent_id, session_id=message.session_id)
        # The changing task, when it is one of this agent's tasks. A probe (``p-`` id) has no row,
        # and a stranger's id must not be linked: the patch is still the state's history, so it
        # is kept without the link rather than refused.
        task_id = message.task_id
        if task_id is not None and (is_probe_id(task_id) or not str(task_id).isdigit() or not await models.Task.objects.filter(pk=task_id, agent_id=agent_id).aexists()):
            task_id = None

        try:
            await models.Patch.objects.acreate(
                state=state,
                agent_id=agent_id,
                session=session,
                interface=message.state_name,
                op=message.op,
                path=message.path,
                value=message.value,
                old_value=message.old_value,
                task_id=task_id,
                global_rev=message.global_rev,
                **position_stamp(message),
            )
        except IntegrityError as e:
            if "patch_unique_rev_per_session_state" not in str(e):
                raise
            # One patch per (session, global_rev, state): this revision is already recorded, so
            # the frame is a resend. Applying it twice is what corrupted reconstruction before.
            logger.info("Dropping a duplicate patch for state %s at revision %s (session %s)", message.state_name, message.global_rev, message.session_id)

    async def on_agent_state_snapshot(self, agent_id: int, message: messages.StateSnapshot) -> None:
        logger.debug("Snapshot from agent %s", agent_id)

        session, _ = await models.Session.objects.aget_or_create(agent_id=agent_id, session_id=message.session_id)
        agent = await models.Agent.objects.aget(id=agent_id)

        for state_name, snapshot in message.snapshots.items():
            state = await models.State.objects.aget(agent_id=agent_id, interface=state_name)

            await models.Snapshot.objects.acreate(
                session=session,
                state=state,
                agent=agent,
                value=snapshot,
                global_rev=message.global_rev,
            )

    async def on_agent_session_init(self, agent_id: int, message: messages.SessionInit) -> None:
        logger.debug("Session init %s", message.session_id)
        # For now we don't do anything with this, but it could be used to initialize session-specific data

        session, _ = await models.Session.objects.aget_or_create(agent_id=agent_id, session_id=message.session_id)
        agent = await models.Agent.objects.aget(id=agent_id)

        for state_name, snapshot in message.states.items():
            state = await models.State.objects.aget(agent_id=agent_id, interface=state_name)

            await models.Snapshot.objects.acreate(
                session=session,
                state=state,
                agent=agent,
                value=snapshot,
                global_rev=0,
            )

    async def on_agent_lock(self, agent_id: int, message: messages.Lock) -> None:
        # Acquire: record that ``task`` holds lock ``key`` on this agent. Lock rows are
        # normally pre-created at registration; aupdate_or_create tolerates a missing one.
        # An unknown task is ignored (a stray lock must not tear down the transport, and
        # setting a dangling FK would raise IntegrityError → socket close).
        if not await models.Task.objects.filter(pk=message.task).aexists():
            logger.warning(f"Lock {message.key} requested by unknown task {message.task} — ignored")
            return
        await models.Lock.objects.aupdate_or_create(
            agent_id=agent_id,
            key=message.key,
            defaults={"hold_by_id": message.task},
        )

    async def on_agent_unlock(self, agent_id: int, message: messages.Unlock) -> None:
        # Release: clear the holder (no-op if the lock is absent or already free).
        await models.Lock.objects.filter(agent_id=agent_id, key=message.key).aupdate(hold_by=None)
