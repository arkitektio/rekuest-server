"""Schedules: one open run at a time, materialized as a delayed task by the refill sweep.

Real postgres + redis. The background reaper is off under test settings, so the tests drive
``refill_schedules`` / ``dispatch_due_tasks`` themselves, from a fresh backend each time.
"""

import datetime
import threading
from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from django.utils import timezone

from facade import enums, inputs, models, schedules
from facade.backend import controll_backend, get_caller_for_context
from facade.caller_context import CallerContext
from facade.schema import schema

from tests.factories import TEST_TOKEN, build_implementation_for_agent, build_webhook_agent
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context

pytestmark = pytest.mark.usefixtures("fake_agentd")

UTC = datetime.timezone.utc


class TestSlots:
    def test_interval_slots_align_to_creation_not_to_the_last_run(self):
        anchor = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        schedule = models.Schedule(interval_seconds=600, created_at=anchor)

        assert schedules.next_slot(schedule, anchor) == anchor + timedelta(minutes=10)
        # A run that finished 3 minutes late does not shift the grid.
        assert schedules.next_slot(schedule, anchor + timedelta(minutes=13)) == anchor + timedelta(minutes=20)
        assert schedules.next_slot(schedule, anchor - timedelta(days=1)) == anchor

    def test_cron_is_read_in_the_schedules_zone_across_dst(self):
        schedule = models.Schedule(cron="0 2 * * *", timezone="Europe/Berlin")

        winter = schedules.next_slot(schedule, datetime.datetime(2026, 1, 10, 12, 0, tzinfo=UTC))
        summer = schedules.next_slot(schedule, datetime.datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
        assert winter == datetime.datetime(2026, 1, 11, 1, 0, tzinfo=UTC)  # 02:00 CET
        assert summer == datetime.datetime(2026, 7, 11, 0, 0, tzinfo=UTC)  # 02:00 CEST

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"interval_seconds": None, "cron": None, "tz": "UTC"}, "exactly one"),
            ({"interval_seconds": 60, "cron": "* * * * *", "tz": "UTC"}, "exactly one"),
            ({"interval_seconds": 0, "cron": None, "tz": "UTC"}, "at least 1"),
            ({"interval_seconds": None, "cron": "every day", "tz": "UTC"}, "cron line"),
            ({"interval_seconds": 60, "cron": None, "tz": "Mars/Olympus"}, "timezone"),
        ],
    )
    def test_invalid_timing_is_refused(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            schedules.validate_timing(**kwargs)


@sync_to_async
def _schedule_for(agent_pk: int, impl_pk: int, **overrides) -> models.Schedule:
    agent = models.Agent.objects.select_related("user", "client", "organization").get(pk=agent_pk)
    impl = models.Implementation.objects.get(pk=impl_pk)
    fields = {"name": "every minute", "interval_seconds": 60, "agent": agent, "interface": impl.interface, **overrides}
    return models.Schedule.objects.create(caller=get_caller_for_context(CallerContext.from_agent(agent)), action=impl.action, **fields)


async def _hook_schedule(prefix: str, **overrides) -> models.Schedule:
    """A schedule pinned to a HookAgent — always a valid assign target, connected or not."""
    agent = await build_webhook_agent(prefix)
    impl = await build_implementation_for_agent(agent.pk, prefix, needs_token=False)
    return await _schedule_for(agent.pk, impl.pk, **overrides)


async def _refill() -> int:
    return await sync_to_async(schedules.refill_schedules_sync)()


async def _open_runs(schedule: models.Schedule) -> list[models.Task]:
    return [t async for t in models.Task.objects.filter(schedule=schedule, is_done=False)]


async def _finish(task: models.Task, kind=enums.TaskEventKind.COMPLETED, message: str = "") -> None:
    await models.Task.objects.filter(pk=task.pk).aupdate(is_done=True, latest_event_kind=kind, finished_at=timezone.now())
    await models.TaskEvent.objects.acreate(task_id=task.pk, kind=kind, message=message)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestRefill:
    async def test_plans_exactly_one_waiting_run(self):
        schedule = await _hook_schedule("sch-one")

        assert await _refill() == 1
        assert await _refill() == 0  # it has its open run: nothing more to plan

        (run,) = await _open_runs(schedule)
        assert run.not_before > timezone.now()
        assert run.dispatch_attempts == 0
        assert run.reference == f"schedule:{schedule.pk}:{run.not_before.isoformat()}"

    async def test_the_next_run_follows_a_finished_one(self):
        schedule = await _hook_schedule("sch-next")
        await _refill()
        (first,) = await _open_runs(schedule)

        await _finish(first)
        assert await _refill() == 1
        (second,) = await _open_runs(schedule)
        assert second.pk != first.pk and second.not_before >= first.not_before

    async def test_failures_are_counted_once_per_run_and_reset_by_success(self):
        schedule = await _hook_schedule("sch-fail")
        await _refill()
        (run,) = await _open_runs(schedule)

        await _finish(run, enums.TaskEventKind.CRITICAL, "bank said no")
        await _refill()
        refreshed = await models.Schedule.objects.aget(pk=schedule.pk)
        assert refreshed.consecutive_failures == 1
        assert "bank said no" in refreshed.last_error

        (run,) = await _open_runs(schedule)
        await _finish(run)
        await _refill()
        refreshed = await models.Schedule.objects.aget(pk=schedule.pk)
        assert refreshed.consecutive_failures == 0 and refreshed.last_error is None

    async def test_cancelling_the_waiting_run_skips_that_slot(self):
        schedule = await _hook_schedule("sch-skip")
        await _refill()
        (skipped,) = await _open_runs(schedule)

        await sync_to_async(controll_backend.cancel)(inputs.CancelInputModel(task=str(skipped.pk)))
        assert (await models.Task.objects.aget(pk=skipped.pk)).latest_event_kind == enums.TaskEventKind.CANCELLED

        assert await _refill() == 1
        (following,) = await _open_runs(schedule)
        assert following.not_before > skipped.not_before  # the reference of the skipped slot is taken

    async def test_a_disabled_schedule_plans_nothing(self):
        await _hook_schedule("sch-off", enabled=False)
        assert await _refill() == 0

    async def test_a_broken_target_is_recorded_and_backed_off(self):
        schedule = await _hook_schedule("sch-broken")
        await models.Implementation.objects.filter(agent_id=schedule.agent_id).adelete()

        assert await _refill() == 0
        refreshed = await models.Schedule.objects.aget(pk=schedule.pk)
        assert "Could not create the next run" in refreshed.last_error
        assert refreshed.refill_after > timezone.now()
        assert await _refill() == 0  # backed off: not retried every tick

    async def test_two_backends_refilling_plan_one_run(self):
        """Real threads on their own connections: the schedule row lock lets one through."""
        schedule = await _hook_schedule("sch-race")
        results: list[int] = []
        barrier = threading.Barrier(2)

        def run() -> None:
            try:
                barrier.wait()
                results.append(schedules.refill_schedules_sync())
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(results) == 1
        assert len(await _open_runs(schedule)) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestTrigger:
    async def test_run_now_moves_the_waiting_run_and_refuses_while_it_executes(self):
        schedule = await _hook_schedule("sch-trigger", interval_seconds=3600)
        await _refill()
        (waiting,) = await _open_runs(schedule)

        moved = await sync_to_async(schedules.trigger)(schedule)
        assert moved.pk == waiting.pk and moved.not_before <= timezone.now()

        # agentd dispatched it (the due-task sweep is agentd's): now it is executing.
        await models.Task.objects.filter(pk=waiting.pk).aupdate(dispatch_attempts=1)
        with pytest.raises(ValueError, match="already executing"):
            await sync_to_async(schedules.trigger)(schedule)

    async def test_run_now_without_an_open_run_creates_a_one_off(self):
        schedule = await _hook_schedule("sch-oneoff", enabled=False)
        task = await sync_to_async(schedules.trigger)(schedule)
        assert task.schedule_id == schedule.pk
        assert task.reference.startswith(f"schedule:{schedule.pk}:manual:")


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
