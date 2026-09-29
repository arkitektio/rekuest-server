"""State history reads: reconstruction is scoped to one session and applies patches in revision
order, and ``checkout`` reports the revision it reconstructed.

``global_rev`` restarts with every agent process, so mixing sessions (or ordering by receive time)
applied a previous process's patches — or re-sent ones — on top of the wrong base.
"""

import pytest
from asgiref.sync import sync_to_async

from facade import logic, models
from facade.schema import schema


@sync_to_async
def _seed_two_sessions(prefix: str, organization):
    """One state, an old session and a newer one whose patches arrived out of revision order."""
    from tests.factories import create_agent_for_registry, create_registry_bundle

    user, client, _, caller = create_registry_bundle(prefix)
    agent = create_agent_for_registry(caller, user, organization, prefix)
    definition = models.StateDefinition.objects.create(name=f"{prefix} def", hash=f"{prefix}-hash", ports=[], description="d", organization=organization)
    state = models.State.objects.create(definition=definition, interface="counter", agent=agent, value={})

    old = models.Session.objects.create(agent=agent, session_id=f"{prefix}-old")
    models.Snapshot.objects.create(state=state, agent=agent, session=old, value={"count": 100, "items": []}, global_rev=0)
    models.Patch.objects.create(state=state, agent=agent, session=old, interface="counter", op="replace", path="/count", value=999, global_rev=1)

    new = models.Session.objects.create(agent=agent, session_id=f"{prefix}-new")
    models.Snapshot.objects.create(state=state, agent=agent, session=new, value={"count": 0, "items": []}, global_rev=0)
    # Received in reverse order (rev 3 first): applying by receive time would add to a list
    # that does not exist yet and replace the count with the older value.
    models.Patch.objects.create(state=state, agent=agent, session=new, interface="counter", op="add", path="/items/1", value="b", global_rev=3)
    models.Patch.objects.create(state=state, agent=agent, session=new, interface="counter", op="add", path="/items/0", value="a", global_rev=2)
    models.Patch.objects.create(state=state, agent=agent, session=new, interface="counter", op="replace", path="/count", value=1, global_rev=1)
    return agent, state


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestLatestState:
    async def test_latest_state_is_scoped_to_the_session_and_ordered_by_revision(self, authenticated_context):
        agent, state = await _seed_two_sessions("latest-state", authenticated_context.request.organization)

        result = await sync_to_async(logic.get_latest_state)(agent)
        assert result["session_id"] == "latest-state-new"
        assert result["states"]["counter"] == {"count": 1, "items": ["a", "b"]}
        assert result["global_revision"] == 3

        at_two = await sync_to_async(logic.get_latest_state)(agent, session_id="latest-state-new", global_revision=2)
        assert at_two["states"]["counter"] == {"count": 1, "items": ["a"]}

        old = await sync_to_async(logic.get_latest_state)(agent, session_id="latest-state-old", forward_patch_count=5, backward_patch_count=5)
        assert old["states"]["counter"] == {"count": 999, "items": []}
        assert [p.global_rev for p in old["backward_patches"]] == [1]  # nothing of the other session

    async def test_checkout_reports_the_revision(self, authenticated_context):
        agent, state = await _seed_two_sessions("checkout-rev", authenticated_context.request.organization)

        result = await schema.execute(
            'query Q($s: ID!) { checkout(state: $s, sessionId: "checkout-rev-new", globalRevision: 2) { value globalRevision } }',
            context_value=authenticated_context,
            variable_values={"s": str(state.pk)},
        )
        assert not result.errors, result.errors
        assert result.data["checkout"] == {"value": {"count": 1, "items": ["a"]}, "globalRevision": 2}
