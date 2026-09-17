"""An agent's reported state: patches, snapshots, sessions and lock reports.

Plain persistence, deliberately separate from the task machinery — none of it touches a task, a
claim or a lease. Note the lock rows are a *report* of what the agent's own in-process lock is
doing, not a grant: the server never hands out a lock.
"""

import logging


from facade import models, messages

logger = logging.getLogger(__name__)


class AgentStateMixin:
    async def on_agent_state_patch(self, agent_id: int, message: messages.StatePatch) -> None:
        logger.info(f"Log Patch for Task {message.state_name}")

        state = await models.State.objects.aget(agent_id=agent_id, interface=message.state_name)
        session, _ = await models.Session.objects.aget_or_create(agent_id=agent_id, session_id=message.session_id)

        await models.Patch.objects.acreate(
            state=state,
            agent_id=agent_id,
            session=session,
            interface=message.state_name,
            op=message.op,
            path=message.path,
            value=message.value,
            task_id=message.task_id,
            global_rev=message.global_rev,
        )

    async def on_agent_state_snapshot(self, agent_id: int, message: messages.StateSnapshot) -> None:
        logger.info(f"Log Snapshot for Task {agent_id}")

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
        logger.info(f"Session init {message.session_id} with data {message}")
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
