"""Reading and changing the automation rules: filters, reverse links, the dry run, re-pointing.

Real postgres; ``fake_takt`` stands in for takt where a schedule is planned. What a signal does
when it arrives is takt's and tested there.
"""

import pytest
from asgiref.sync import sync_to_async

from facade import models
from facade.schema import schema
from tests.factories import TEST_TOKEN
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context
from tests.test_schedules import CREATE_SCHEDULE, _schedulable
from tests.test_triggers import CHANNELS, CREATE_TRIGGER, IDENTIFIER, _target

pytestmark = [pytest.mark.usefixtures("fake_takt"), pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


async def _contexts():
    return await sync_to_async(lambda: (tenant_context(TEST_TOKEN)[0], tenant_context(OTHER_TOKEN)[0]))()


async def _declared(name: str, *kinds: str) -> None:
    service = await models.Service.objects.acreate(name=name)
    for kind in kinds:
        await models.SignalDeclaration.objects.acreate(service=service, identifier=IDENTIFIER, kind=kind, descriptor_keys=[CHANNELS])


async def _signal(organization, object: str, channels: int, kind: str = "CREATED") -> models.Signal:
    return await models.Signal.objects.acreate(service="mikro", signal_id=f"{kind}-{object}", kind=kind, identifier=IDENTIFIER, object=object, organization=organization, descriptors={CHANNELS: channels})


async def _run(query: str, context, **variables):
    result = await schema.execute(query, variable_values=variables or None, context_value=context)
    assert result.errors is None, result.errors
    return result.data


async def _trigger(context, impl, **fields) -> str:
    values = {"name": "thumbnail", "kind": "CREATED", "identifier": IDENTIFIER, "action": str(impl.action_id), "port": "image", "args": {"size": 64}, **fields}
    return (await _run(CREATE_TRIGGER, context, input=values))["createTrigger"]["id"]


class TestFilters:
    async def test_schedules_are_filtered_and_ordered(self, authenticated_context):
        context, _ = await _contexts()
        impl = await _schedulable("flt-sched", context)
        base = {"action": str(impl.action_id), "intervalSeconds": 3600}
        nightly = (await _run(CREATE_SCHEDULE, context, input={**base, "name": "nightly sync"}))["createSchedule"]["id"]
        hourly = (await _run(CREATE_SCHEDULE, context, input={**base, "name": "hourly purge", "enabled": False}))["createSchedule"]["id"]
        await models.Schedule.objects.filter(pk=hourly).aupdate(consecutive_failures=2)

        listed = "query($filters: ScheduleFilter, $ordering: [ScheduleOrder!]! = []) { schedules(filters: $filters, ordering: $ordering) { id } }"

        async def ids(**variables):
            return [row["id"] for row in (await _run(listed, context, **variables))["schedules"]]

        assert await ids(filters={"search": "NIGHT"}) == [nightly]
        assert await ids(filters={"enabled": False}) == [hourly]
        assert await ids(filters={"failing": True}) == [hourly]
        assert await ids(filters={"failing": False}) == [nightly]
        assert await ids(filters={"action": str(impl.action_id)}, ordering=[{"name": "ASC"}]) == [hourly, nightly]
        # A filters variable that is given as null scopes like one that is absent.
        assert sorted(await ids(filters=None)) == sorted([nightly, hourly])

    async def test_triggers_and_signals_are_filtered(self, authenticated_context):
        context, other = await _contexts()
        await _declared("flt-service", "CREATED", "UPDATED")
        impl = await sync_to_async(_target)("flt-trig", context.request.organization)
        created = await _trigger(context, impl, name="on create")
        updated = await _trigger(context, impl, name="on update", kind="UPDATED")

        triggers = "query($filters: TriggerFilter) { triggers(filters: $filters) { id } }"
        assert [t["id"] for t in (await _run(triggers, context, filters={"kind": "UPDATED"}))["triggers"]] == [updated]
        assert [t["id"] for t in (await _run(triggers, context, filters={"identifier": IDENTIFIER, "search": "create"}))["triggers"]] == [created]

        organization = context.request.organization
        first = await _signal(organization, "1", 3)
        second = await _signal(organization, "2", 1, kind="UPDATED")
        await models.Signal.objects.filter(pk=first.pk).aupdate(processed_at="2026-10-03T00:00:00Z")
        await _signal(other.request.organization, "9", 3)  # another tenant's

        signals = "query($filters: SignalFilter, $ordering: [SignalOrder!]! = []) { signals(filters: $filters, ordering: $ordering) { id } }"

        async def ids(**variables):
            return [row["id"] for row in (await _run(signals, context, **variables))["signals"]]

        assert await ids(ordering=[{"receivedAt": "ASC"}]) == [str(first.pk), str(second.pk)]
        assert await ids(filters={"kind": ["UPDATED"]}) == [str(second.pk)]
        assert await ids(filters={"object": "1"}) == [str(first.pk)]
        assert await ids(filters={"processed": False}) == [str(second.pk)]
        assert await ids(filters={"matched": True}) == []  # nothing ran for either

    async def test_a_signal_is_fetched_by_id_within_its_organization(self, authenticated_context):
        context, other = await _contexts()
        signal = await _signal(context.request.organization, "7", 2)
        query = "query($id: ID!) { signal(id: $id) { id object descriptors } }"

        assert (await _run(query, context, id=str(signal.pk)))["signal"]["object"] == "7"
        foreign = await schema.execute(query, variable_values={"id": str(signal.pk)}, context_value=other)
        assert foreign.errors is not None  # another tenant's id reads as missing


class TestReverseLinks:
    async def test_a_run_names_its_schedule_and_actions_and_agents_their_rules(self, authenticated_context):
        context, _ = await _contexts()
        await _declared("link-service", "CREATED")
        scheduled = await _schedulable("link-sched", context)
        schedule = (await _run(CREATE_SCHEDULE, context, input={"name": "hourly", "action": str(scheduled.action_id), "intervalSeconds": 3600, "agent": str(scheduled.agent_id), "interface": scheduled.interface}))[
            "createSchedule"
        ]
        triggered = await sync_to_async(_target)("link-trig", context.request.organization)
        trigger = await _trigger(context, triggered, agent=str(triggered.agent_id), interface=triggered.interface)

        run = await _run("query($id: ID!) { task(id: $id) { schedule { id name } } }", context, id=schedule["nextRun"]["id"])
        assert run["task"]["schedule"] == {"id": schedule["id"], "name": "hourly"}
        runs = await _run("query($filters: TaskFilter) { tasks(filters: $filters) { id } }", context, filters={"schedule": schedule["id"]})
        assert [t["id"] for t in runs["tasks"]] == [schedule["nextRun"]["id"]]

        rules = "query($action: ID!, $agent: ID!) { action(id: $action) { schedules { id } triggers { id } } agent(id: $agent) { schedules { id } triggers { id } } }"
        of_schedule = await _run(rules, context, action=str(scheduled.action_id), agent=str(scheduled.agent_id))
        assert of_schedule["action"] == {"schedules": [{"id": schedule["id"]}], "triggers": []}
        assert of_schedule["agent"] == {"schedules": [{"id": schedule["id"]}], "triggers": []}
        of_trigger = await _run(rules, context, action=str(triggered.action_id), agent=str(triggered.agent_id))
        assert of_trigger["action"] == {"schedules": [], "triggers": [{"id": trigger}]}
        assert of_trigger["agent"] == {"schedules": [], "triggers": [{"id": trigger}]}

    async def test_a_declaration_lists_the_organizations_own_triggers(self, authenticated_context):
        context, other = await _contexts()
        await _declared("decl-service", "CREATED")
        impl = await sync_to_async(_target)("decl-trig", context.request.organization)
        trigger = await _trigger(context, impl)
        query = "query { signalDeclarations { kind triggers { id } } }"

        assert (await _run(query, context))["signalDeclarations"] == [{"kind": "CREATED", "triggers": [{"id": trigger}]}]
        assert (await _run(query, other))["signalDeclarations"] == [{"kind": "CREATED", "triggers": []}]


class TestDryRun:
    async def test_conditions_are_tried_against_the_stored_signals(self, authenticated_context):
        context, other = await _contexts()
        organization = context.request.organization
        one, three = await _signal(organization, "one", 1), await _signal(organization, "three", 3)
        await _signal(organization, "gone", 3, kind="DELETED")
        await _signal(other.request.organization, "theirs", 3)
        query = "query($conditions: AnyDefault) { matchingSignals(kind: CREATED, identifier: \"%s\", conditions: $conditions) { id } }" % IDENTIFIER

        async def matching(conditions=None):
            return [row["id"] for row in (await _run(query, context, conditions=conditions))["matchingSignals"]]

        assert await matching() == [str(three.pk), str(one.pk)]  # newest first, this kind, this tenant
        assert await matching([{"key": CHANNELS, "operator": "GTE", "value": 2}]) == [str(three.pk)]
        assert await matching([{"key": "@mikro/unsent", "operator": "EQUALS", "value": 1}]) == []
        bad = await schema.execute(query, variable_values={"conditions": [{"key": CHANNELS, "operator": "IN", "value": 3}]}, context_value=context)
        assert bad.errors is not None

    async def test_a_trigger_also_applies_what_its_port_requires(self, authenticated_context):
        context, _ = await _contexts()
        await _declared("dry-service", "CREATED")
        organization = context.request.organization
        impl = await sync_to_async(_target)("dry-trig", organization)  # its port requires two channels or more
        await _signal(organization, "one", 1)
        three, five = await _signal(organization, "three", 3), await _signal(organization, "five", 5)
        query = "query($id: ID!) { trigger(id: $id) { matchingSignals { id } lastRunAt } }"

        unconditional = await _trigger(context, impl)
        found = (await _run(query, context, id=unconditional))["trigger"]
        assert [s["id"] for s in found["matchingSignals"]] == [str(five.pk), str(three.pk)]
        assert found["lastRunAt"] is None

        picky = await _trigger(context, impl, conditions=[{"key": CHANNELS, "operator": "GTE", "value": 4}])
        assert [s["id"] for s in (await _run(query, context, id=picky))["trigger"]["matchingSignals"]] == [str(five.pk)]


UPDATE_TRIGGER = "mutation($input: UpdateTriggerInput!) { updateTrigger(input: $input) { id kind port conditions agent { id } interface action { id } } }"
UPDATE_SCHEDULE = "mutation($input: UpdateScheduleInput!) { updateSchedule(input: $input) { id ephemeralRuns agent { id } interface action { id } nextRun { id } } }"


class TestRepointing:
    async def test_a_trigger_is_repointed_and_checked_as_a_whole(self, authenticated_context):
        context, _ = await _contexts()
        await _declared("repoint-service", "CREATED", "UPDATED")
        organization = context.request.organization
        first = await sync_to_async(_target)("repoint-a", organization)
        second = await sync_to_async(_target)("repoint-b", organization)
        trigger = await _trigger(context, first, agent=str(first.agent_id), interface=first.interface)
        # takt's bookkeeping on the row: a change must not write over it.
        await models.Trigger.objects.filter(pk=trigger).aupdate(consecutive_failures=3, last_error="earlier")

        async def update(**fields):
            return await schema.execute(UPDATE_TRIGGER, variable_values={"input": {"id": trigger, **fields}}, context_value=context)

        # The old pin does not implement the new action: refused, nothing written.
        refused = await update(action=str(second.action_id))
        assert refused.errors is not None and "no implementation" in str(refused.errors[0])
        assert (await models.Trigger.objects.aget(pk=trigger)).action_id == first.action_id

        moved = await update(action=str(second.action_id), agent=str(second.agent_id), interface=second.interface, kind="UPDATED")
        assert moved.errors is None, moved.errors
        assert moved.data["updateTrigger"]["action"]["id"] == str(second.action_id) and moved.data["updateTrigger"]["kind"] == "UPDATED"

        unpinned = await update(agent=None, interface=None)
        assert unpinned.errors is None, unpinned.errors
        assert unpinned.data["updateTrigger"]["agent"] is None and unpinned.data["updateTrigger"]["interface"] is None

        wrong_port = await update(port="size")
        assert wrong_port.errors is not None and "not a @mikro/arraydataset structure" in str(wrong_port.errors[0])

        kept = await models.Trigger.objects.aget(pk=trigger)
        assert (kept.port, kept.consecutive_failures, kept.last_error) == ("image", 3, "earlier")

    async def test_a_retargeted_schedule_is_replanned(self, authenticated_context, fake_takt):
        context, _ = await _contexts()
        first = await _schedulable("retarget-a", context)
        second = await _schedulable("retarget-b", context)
        schedule = (await _run(CREATE_SCHEDULE, context, input={"name": "hourly", "action": str(first.action_id), "intervalSeconds": 3600, "agent": str(first.agent_id), "interface": first.interface}))[
            "createSchedule"
        ]
        fake_takt.calls.clear()

        moved = await _run(UPDATE_SCHEDULE, context, input={"id": schedule["id"], "action": str(second.action_id), "agent": str(second.agent_id), "interface": second.interface, "ephemeralRuns": True})
        assert moved["updateSchedule"]["action"]["id"] == str(second.action_id) and moved["updateSchedule"]["ephemeralRuns"] is True
        # The waiting run was planned for the old target: it is cancelled, not dispatched once more.
        assert [payload.get("replan") for op, payload in fake_takt.calls if op == "schedule/plan"] == [True]
        assert moved["updateSchedule"]["nextRun"]["id"] != schedule["nextRun"]["id"]

        mismatched = await schema.execute(UPDATE_SCHEDULE, variable_values={"input": {"id": schedule["id"], "action": str(first.action_id)}}, context_value=context)
        assert mismatched.errors is not None and "no implementation" in str(mismatched.errors[0])
