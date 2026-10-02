"""Schedules, from this server's side: rows it owns, runs it asks takt for.

Planning, moving and cancelling runs, cron lines and the refill sweep are takt's and tested
there (``takt/crates/facade/tests/scheduling.rs``, ``src/timing.rs``). Here: the GraphQL
mutations scope and check what they are given, write the row, and ask takt for the right
thing (``fake_takt`` stands in for it, and records every request).
"""

import pytest
from asgiref.sync import sync_to_async

from facade import models
from facade.schema import schema
from tests.factories import TEST_TOKEN, build_implementation_for_agent, build_webhook_agent
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context

pytestmark = pytest.mark.usefixtures("fake_takt")


async def _schedulable(prefix: str, context, *, needs_token: bool = False) -> models.Implementation:
    """An implementation on a HookAgent, both in the tenant of ``context``."""
    agent = await build_webhook_agent(prefix)
    impl = await build_implementation_for_agent(agent.pk, prefix, needs_token=needs_token)
    organization = context.request.organization
    await models.Action.objects.filter(pk=impl.action_id).aupdate(organization=organization)
    await models.Agent.objects.filter(pk=agent.pk).aupdate(organization=organization)
    return impl


CREATE_SCHEDULE = """
    mutation CreateSchedule($input: CreateScheduleInput!) {
        createSchedule(input: $input) { id name enabled nextRun { id notBefore } }
    }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestScheduleGraphQL:
    async def test_create_plans_the_next_run_and_is_scoped_to_the_organization(self, authenticated_context):
        @sync_to_async
        def seed():
            context_a, *_ = tenant_context(TEST_TOKEN)
            context_b, *_ = tenant_context(OTHER_TOKEN)
            return context_a, context_b

        context_a, context_b = await seed()
        session_agent = await build_webhook_agent("sch-gql")
        impl = await build_implementation_for_agent(session_agent.pk, "sch-gql", needs_token=False)
        # The action has to live in tenant A to be schedulable from it.
        org_a = context_a.request.organization
        await models.Action.objects.filter(pk=impl.action_id).aupdate(organization=org_a)
        await models.Agent.objects.filter(pk=session_agent.pk).aupdate(organization=org_a)

        result = await schema.execute(
            CREATE_SCHEDULE,
            variable_values={"input": {"name": "nightly", "action": str(impl.action_id), "cron": "0 2 * * *", "timezone": "Europe/Berlin", "agent": str(session_agent.pk), "interface": impl.interface}},
            context_value=context_a,
        )
        assert result.errors is None, result.errors
        created = result.data["createSchedule"]
        assert created["nextRun"] is not None and created["nextRun"]["notBefore"] is not None

        seen_by_a = await schema.execute("query { schedules { id } }", context_value=context_a)
        seen_by_b = await schema.execute("query { schedules { id } }", context_value=context_b)
        assert [s["id"] for s in seen_by_a.data["schedules"]] == [created["id"]]
        assert seen_by_b.data["schedules"] == []

        direct = await schema.execute("query($id: ID!) { schedule(id: $id) { id } }", variable_values={"id": created["id"]}, context_value=context_b)
        assert direct.errors is not None  # another tenant's id reads as missing

    async def test_strict_provenance_refuses_a_target_that_needs_a_token(self, authenticated_context, settings):
        settings.PROVENANCE = {**settings.PROVENANCE, "STRICT": True}
        context_a = (await sync_to_async(tenant_context)(TEST_TOKEN))[0]
        agent = await build_webhook_agent("sch-gql-strict")
        impl = await build_implementation_for_agent(agent.pk, "sch-gql-strict", needs_token=True)
        await models.Action.objects.filter(pk=impl.action_id).aupdate(organization=context_a.request.organization)

        result = await schema.execute(
            CREATE_SCHEDULE,
            variable_values={"input": {"name": "strict", "action": str(impl.action_id), "intervalSeconds": 60}},
            context_value=context_a,
        )
        assert result.errors is not None and "provenance" in str(result.errors[0])

    async def test_invalid_timing_is_refused(self, authenticated_context):
        context_a = (await sync_to_async(tenant_context)(TEST_TOKEN))[0]
        agent = await build_webhook_agent("sch-gql-bad")
        impl = await build_implementation_for_agent(agent.pk, "sch-gql-bad", needs_token=False)
        await models.Action.objects.filter(pk=impl.action_id).aupdate(organization=context_a.request.organization)

        result = await schema.execute(
            CREATE_SCHEDULE,
            variable_values={"input": {"name": "broken", "action": str(impl.action_id), "cron": "whenever"}},
            context_value=context_a,
        )
        assert result.errors is not None and "cron line" in str(result.errors[0])


UPDATE_SCHEDULE = """
    mutation UpdateSchedule($input: UpdateScheduleInput!) {
        updateSchedule(input: $input) { id name enabled cron intervalSeconds nextRun { id } }
    }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestScheduleChanges:
    async def _created(self, prefix: str, context, **input) -> tuple[str, models.Implementation]:
        impl = await _schedulable(prefix, context)
        fields = {"name": "hourly", "action": str(impl.action_id), "intervalSeconds": 3600, "agent": str(impl.agent_id), "interface": impl.interface, **input}
        result = await schema.execute(CREATE_SCHEDULE, variable_values={"input": fields}, context_value=context)
        assert result.errors is None, result.errors
        return result.data["createSchedule"]["id"], impl

    async def test_a_retimed_schedule_is_replanned_and_a_renamed_one_is_not(self, authenticated_context, fake_takt):
        context = (await sync_to_async(tenant_context)(TEST_TOKEN))[0]
        schedule_id, _ = await self._created("sch-retime", context)
        first_run = (await models.Task.objects.aget(schedule_id=schedule_id, is_done=False)).pk
        # takt's bookkeeping on the row: a rename must not write over it.
        await models.Schedule.objects.filter(pk=schedule_id).aupdate(consecutive_failures=2, last_error="earlier")
        fake_takt.calls.clear()

        renamed = await schema.execute(UPDATE_SCHEDULE, variable_values={"input": {"id": schedule_id, "name": "every hour"}}, context_value=context)
        assert renamed.errors is None, renamed.errors
        assert renamed.data["updateSchedule"]["nextRun"]["id"] == str(first_run)
        assert [(op, payload.get("replan")) for op, payload in fake_takt.calls if op == "schedule/plan"] == [("schedule/plan", False)]
        kept = await models.Schedule.objects.aget(pk=schedule_id)
        assert (kept.name, kept.consecutive_failures, kept.last_error) == ("every hour", 2, "earlier")

        retimed = await schema.execute(UPDATE_SCHEDULE, variable_values={"input": {"id": schedule_id, "cron": "0 2 * * *"}}, context_value=context)
        assert retimed.errors is None, retimed.errors
        assert retimed.data["updateSchedule"]["cron"] == "0 2 * * *" and retimed.data["updateSchedule"]["intervalSeconds"] is None
        (replanned,) = [payload for op, payload in fake_takt.calls if op == "schedule/plan" and payload.get("replan")]
        assert replanned["principal"]["organization"] == context.request.organization.pk
        assert retimed.data["updateSchedule"]["nextRun"]["id"] != str(first_run)  # the waiting run of the old timing went

        bad = await schema.execute(UPDATE_SCHEDULE, variable_values={"input": {"id": schedule_id, "cron": "whenever"}}, context_value=context)
        assert bad.errors is not None and "cron line" in str(bad.errors[0])
        assert (await models.Schedule.objects.aget(pk=schedule_id)).cron == "0 2 * * *"

    async def test_run_now_and_delete_go_through_takt(self, authenticated_context, fake_takt):
        context = (await sync_to_async(tenant_context)(TEST_TOKEN))[0]
        other = (await sync_to_async(tenant_context)(OTHER_TOKEN))[0]
        schedule_id, _ = await self._created("sch-now", context)
        waiting = await models.Task.objects.aget(schedule_id=schedule_id, is_done=False)
        run_now = "mutation($input: ScheduleIdInput!) { triggerSchedule(input: $input) { id } }"
        delete = "mutation($input: ScheduleIdInput!) { deleteSchedule(input: $input) }"

        foreign = await schema.execute(run_now, variable_values={"input": {"id": schedule_id}}, context_value=other)
        assert foreign.errors is not None  # another tenant's schedule reads as missing

        triggered = await schema.execute(run_now, variable_values={"input": {"id": schedule_id}}, context_value=context)
        assert triggered.errors is None, triggered.errors
        assert triggered.data["triggerSchedule"]["id"] == str(waiting.pk)

        await models.Task.objects.filter(pk=waiting.pk).aupdate(dispatch_attempts=1)
        executing = await schema.execute(run_now, variable_values={"input": {"id": schedule_id}}, context_value=context)
        assert executing.errors is not None and "already executing" in str(executing.errors[0])

        fake_takt.calls.clear()
        deleted = await schema.execute(delete, variable_values={"input": {"id": schedule_id}}, context_value=context)
        assert deleted.errors is None, deleted.errors
        assert [op for op, _ in fake_takt.calls] == ["schedule/cancel-waiting"]
        assert not await models.Schedule.objects.filter(pk=schedule_id).aexists()
        assert (await models.Task.objects.aget(pk=waiting.pk)).schedule_id is None  # the history is kept
