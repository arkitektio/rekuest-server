"""The state-history API executes and reconstructs state correctly.

All six of these root queries used to raise ``FieldError`` on every call: the resolvers and the
``Patch``/``Snapshot`` types referenced ``revision`` / ``current_revision`` / ``future_revision`` /
``global_current_revision`` / ``global_future_revision``, none of which exist. The models store a
single revision column, ``global_rev``, which is the revision *after* a patch applies
(``facade.logic.get_latest_state`` sets ``current_global_revision = patch.global_rev`` after
applying). Everything is now derived from it:

    future  == global_rev
    current == global_rev - 1

so a patch spans ``(global_rev - 1) -> global_rev``. There is no separate per-state counter, so
the duplicate non-global fields (``revision`` / ``currentRevision`` / ``futureRevision``) and the
duplicate ``stateAtLocalRev`` query were removed rather than kept as aliases.
"""

import pytest
from asgiref.sync import sync_to_async

from facade import models
from facade.schema import schema

from tests.factories import create_agent_for_registry, create_registry_bundle


@sync_to_async
def _seed_history(prefix, context):
    """One state with a snapshot at rev 1 and patches carrying it to rev 3."""
    org = context.request.organization
    user, client, _, caller = create_registry_bundle(prefix)
    agent = create_agent_for_registry(caller, user, org, prefix)

    definition = models.StateDefinition.objects.create(
        name=f"{prefix} def", hash=f"{prefix}-def-hash", ports=[], description="d", organization=org
    )
    state = models.State.objects.create(definition=definition, interface=f"{prefix}-iface", agent=agent)
    session = models.Session.objects.create(agent=agent, session_id=f"{prefix}-session")

    models.Snapshot.objects.create(state=state, agent=agent, session=session, value={"count": 0}, global_rev=1)
    for rev, count in ((2, 1), (3, 2)):
        models.Patch.objects.create(
            state=state,
            agent=agent,
            session=session,
            interface=state.interface,
            op="replace",
            path="/count",
            value=count,
            global_rev=rev,
        )
    return {"state": state, "session": session, "agent": agent}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestStateHistory:
    async def test_state_at_global_rev_reconstructs_the_value(self, authenticated_context):
        seeded = await _seed_history("hist-at", authenticated_context)

        result = await schema.execute(
            "query Q($rev: Int!, $state: ID!) { stateAtGlobalRev(globalRevision: $rev, stateId: $state) { value globalRevision } }",
            context_value=authenticated_context,
            variable_values={"rev": 3, "state": str(seeded["state"].pk)},
        )

        assert not result.errors, result.errors
        rows = result.data["stateAtGlobalRev"]
        assert len(rows) == 1
        # snapshot {"count": 0} at rev 1, then patches to rev 2 and rev 3 -> count == 2
        assert rows[0]["value"] == {"count": 2}
        assert rows[0]["globalRevision"] == 3

    async def test_state_at_earlier_revision_stops_there(self, authenticated_context):
        seeded = await _seed_history("hist-mid", authenticated_context)

        result = await schema.execute(
            "query Q($rev: Int!, $state: ID!) { stateAtGlobalRev(globalRevision: $rev, stateId: $state) { value } }",
            context_value=authenticated_context,
            variable_values={"rev": 2, "state": str(seeded["state"].pk)},
        )

        assert not result.errors, result.errors
        assert result.data["stateAtGlobalRev"][0]["value"] == {"count": 1}

    async def test_boundaries_span_the_patch_range(self, authenticated_context):
        seeded = await _seed_history("hist-bound", authenticated_context)

        result = await schema.execute(
            "query Q($s: ID!) { sessionBoundaries(sessionId: $s) { startGlobalRevision endGlobalRevision } }",
            context_value=authenticated_context,
            variable_values={"s": seeded["session"].session_id},
        )

        assert not result.errors, result.errors
        b = result.data["sessionBoundaries"]
        # patches at global_rev 2 and 3 => the range they cover is 1 -> 3
        assert b["startGlobalRevision"] == 1
        assert b["endGlobalRevision"] == 3

    async def test_patch_revision_fields_are_derived(self, authenticated_context):
        seeded = await _seed_history("hist-fields", authenticated_context)

        result = await schema.execute(
            """query Q($f: Int!, $t: Int!) {
                 patchEventsBetweenGlobalRevs(fromGlobalRevision: $f, toGlobalRevision: $t) {
                   globalCurrentRevision globalFutureRevision
                 }
               }""",
            context_value=authenticated_context,
            variable_values={"f": 1, "t": 3},
        )

        assert not result.errors, result.errors
        rows = result.data["patchEventsBetweenGlobalRevs"]
        assert len(rows) == 2
        for row in rows:
            assert row["globalFutureRevision"] == row["globalCurrentRevision"] + 1

    async def test_every_state_history_query_executes(self, authenticated_context):
        """All six remaining root fields — each of these previously raised FieldError."""
        seeded = await _seed_history("hist-all", authenticated_context)
        sid, session = str(seeded["state"].pk), seeded["session"].session_id

        queries = [
            ("query Q($s: ID!) { sessionBoundaries(sessionId: $s) { startGlobalRevision } }", {"s": session}),
            ("query Q($c: String!) { taskBoundaries(correlationId: $c) { startGlobalRevision } }", {"c": "no-such-task"}),
            ("query Q($r: Int!) { stateAtGlobalRev(globalRevision: $r) { value } }", {"r": 3}),
            ("query Q($r: Int!) { forwardEventsAfterRev(globalRevision: $r) { globalFutureRevision } }", {"r": 1}),
            ("query Q($f: Int!, $t: Int!) { patchEventsBetweenGlobalRevs(fromGlobalRevision: $f, toGlobalRevision: $t) { globalFutureRevision } }", {"f": 1, "t": 3}),
            ("query Q($r: Int!, $s: ID!) { snapshotsAroundRev(revision: $r, stateId: $s) { globalRevision } }", {"r": 2, "s": sid}),
        ]
        for query, variables in queries:
            result = await schema.execute(query, context_value=authenticated_context, variable_values=variables)
            assert not result.errors, f"{query.strip()[:60]} -> {result.errors}"
