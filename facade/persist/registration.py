"""Registration and shelving requested over the agent's own socket.

The bodies live in :mod:`facade.registration`, shared with the GraphQL mutations; this mixin
is the async seam the consumer (``Register``) and the router (``Shelve``/``Unshelve``) call,
running each request off the event loop because ``implement_agent`` is one atomic
transaction that walks FK chains.

``facade.registration`` is imported lazily: ``implement_agent`` reaches into
``facade.mutations``, which imports the consumers, which import the router, which is what
calls this mixin.
"""

import logging

from channels.db import database_sync_to_async

from facade import messages, models
from rekuest_core.objects.models import DiagnosticModel

logger = logging.getLogger(__name__)


class AgentRegistrationMixin:
    @staticmethod
    def _agent_with_identity_sync(agent_id: int) -> models.Agent:
        return models.Agent.objects.select_related("client", "client__release", "client__release__app", "user", "organization").get(id=agent_id)

    @classmethod
    def _implement_sync(cls, agent_id: int, register: messages.Register) -> tuple[models.Agent, list[DiagnosticModel]]:
        from facade import registration
        from facade.mutations.agent import ImplementAgentInputModel

        agent = cls._agent_with_identity_sync(agent_id)
        payload = ImplementAgentInputModel(
            name=register.name,
            implementations=register.implementations,
            states=register.states,
            locks=register.locks,
            bloks=register.bloks,
            hash=register.hash,
        )
        return registration.implement_agent(agent.client, agent.user, agent.organization, payload)

    @classmethod
    def _shelve_sync(cls, agent_id: int, message: messages.Shelve) -> models.MemoryDrawer:
        from facade import registration

        agent = cls._agent_with_identity_sync(agent_id)
        return registration.shelve(
            agent,
            identifier=message.identifier,
            resource_id=message.resource_id,
            label=message.label,
            description=message.description,
        )

    @classmethod
    def _unshelve_sync(cls, agent_id: int, message: messages.Unshelve) -> None:
        from facade import registration

        registration.unshelve(cls._agent_with_identity_sync(agent_id), message.drawer)

    async def on_agent_implement(self, agent_id: int, register: messages.Register) -> tuple[models.Agent, list[DiagnosticModel]]:
        """Reconcile the declaration a ``Register`` carries, atomically; the agent and its diagnostics."""
        return await database_sync_to_async(self._implement_sync)(agent_id, register)

    async def on_agent_shelve(self, agent_id: int, message: messages.Shelve) -> models.MemoryDrawer:
        """Upsert a drawer on the agent's shelve."""
        return await database_sync_to_async(self._shelve_sync)(agent_id, message)

    async def on_agent_unshelve(self, agent_id: int, message: messages.Unshelve) -> None:
        """Drop a drawer from the agent's shelve; raises ``ValueError`` for an unknown or foreign one."""
        await database_sync_to_async(self._unshelve_sync)(agent_id, message)
