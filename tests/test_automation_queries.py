"""Reading and changing the automation rules: filters, reverse links, the dry run, re-pointing.

Real postgres and a real takt (``takt``) where a schedule is planned. What a signal does
when it arrives is takt's and tested there.
"""

import pytest
from asgiref.sync import sync_to_async

from facade import models
from facade.schema import schema
from tests.conftest import settled_run, waiting_run
from tests.factories import TEST_TOKEN
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context
from tests.test_schedules import CREATE_SCHEDULE, _schedulable
from tests.test_triggers import CHANNELS, CREATE_TRIGGER, IDENTIFIER, _target

pytestmark = [pytest.mark.usefixtures("takt"), pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


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


async def _waiting(schedule: str) -> int:
    """The run takt planned for the schedule. A mutation's answer does not wait for it."""
    return await waiting_run(schedule)


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
        schedule = (await _run(CREATE_SCHEDULE, context, input={"name": "hourly", "action": str(scheduled.action_id), "intervalSeconds": 3600, "agent": str(scheduled.agent_id), "interface": scheduled.interface}))["createSchedule"]
        triggered = await sync_to_async(_target)("link-trig", context.request.organization)
        trigger = await _trigger(context, triggered, agent=str(triggered.agent_id), interface=triggered.interface)

        planned = str(await _waiting(schedule["id"]))
        run = await _run("query($id: ID!) { task(id: $id) { schedule { id name } } }", context, id=planned)
        assert run["task"]["schedule"] == {"id": schedule["id"], "name": "hourly"}
        runs = await _run("query($filters: TaskFilter) { tasks(filters: $filters) { id } }", context, filters={"schedule": schedule["id"]})
        assert [t["id"] for t in runs["tasks"]] == [planned]

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
        query = 'query($conditions: AnyDefault) { matchingSignals(kind: CREATED, identifier: "%s", conditions: $conditions) { id } }' % IDENTIFIER

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
UPDATE_SCHEDULE = "mutation($input: UpdateScheduleInput!) { updateSchedule(input: $input) { id ephemeralRuns agent { id } interface action { id } } }"


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

    async def test_a_retargeted_schedule_is_replanned(self, authenticated_context, schedule_notices):
        context, _ = await _contexts()
        first = await _schedulable("retarget-a", context)
        second = await _schedulable("retarget-b", context)
        schedule = (await _run(CREATE_SCHEDULE, context, input={"name": "hourly", "action": str(first.action_id), "intervalSeconds": 3600, "agent": str(first.agent_id), "interface": first.interface}))["createSchedule"]
        first_run = await _waiting(schedule["id"])
        schedule_notices.sent()

        moved = await _run(UPDATE_SCHEDULE, context, input={"id": schedule["id"], "action": str(second.action_id), "agent": str(second.agent_id), "interface": second.interface, "ephemeralRuns": True})
        assert moved["updateSchedule"]["action"]["id"] == str(second.action_id) and moved["updateSchedule"]["ephemeralRuns"] is True
        # The waiting run was planned for the old target: it is cancelled, not dispatched once more.
        assert [notice["replan"] for notice in schedule_notices.sent()] == [True]
        assert await waiting_run(schedule["id"], other_than=first_run) != first_run
        assert (await settled_run(first_run)).latest_event_kind == "CANCELLED"

        mismatched = await schema.execute(UPDATE_SCHEDULE, variable_values={"input": {"id": schedule["id"], "action": str(first.action_id)}}, context_value=context)
        assert mismatched.errors is not None and "no implementation" in str(mismatched.errors[0])


class TestBrokenRules:
    """A rule that broke since it was written can still be renamed and switched off."""

    async def test_a_trigger_whose_signal_is_no_longer_declared(self, authenticated_context):
        context, _ = await _contexts()
        await _declared("gone-service", "CREATED")
        impl = await sync_to_async(_target)("gone-trig", context.request.organization)
        trigger = await _trigger(context, impl)
        await models.SignalDeclaration.objects.all().adelete()
        update = "mutation($input: UpdateTriggerInput!) { updateTrigger(input: $input) { name enabled maxRuns } }"

        switched = await _run(update, context, input={"id": trigger, "enabled": False, "name": "retired", "maxRuns": 5})
        assert switched["updateTrigger"] == {"name": "retired", "enabled": False, "maxRuns": 5}
        # What it listens for is still checked as soon as that is what changes.
        retargeted = await schema.execute(update, variable_values={"input": {"id": trigger, "args": {"size": 32}}}, context_value=context)
        assert retargeted.errors is not None and "No service declares" in str(retargeted.errors[0])

    async def test_a_schedule_whose_implementation_is_gone(self, authenticated_context):
        context, _ = await _contexts()
        impl = await _schedulable("gone-sched", context)
        schedule = (await _run(CREATE_SCHEDULE, context, input={"name": "hourly", "action": str(impl.action_id), "intervalSeconds": 3600, "agent": str(impl.agent_id), "interface": impl.interface}))["createSchedule"]["id"]
        await models.Implementation.objects.filter(pk=impl.pk).adelete()
        update = "mutation($input: UpdateScheduleInput!) { updateSchedule(input: $input) { name enabled } }"

        switched = await _run(update, context, input={"id": schedule, "enabled": False, "name": "retired"})
        assert switched["updateSchedule"] == {"name": "retired", "enabled": False}
        retimed = await schema.execute(update, variable_values={"input": {"id": schedule, "intervalSeconds": 60}}, context_value=context)
        assert retimed.errors is not None and "no implementation" in str(retimed.errors[0])

    async def test_an_edit_does_not_use_up_a_schedules_run_limit(self, authenticated_context):
        context, _ = await _contexts()
        impl = await _schedulable("limit-sched", context)
        # The mutations answer with the row as they wrote it; what takt made of it is read afterwards.
        read = "query($id: ID!) { schedule(id: $id) { id runCount exhausted nextRun { id } } }"
        schedule = (await _run("mutation($input: CreateScheduleInput!) { createSchedule(input: $input) { id } }", context, input={"name": "once", "action": str(impl.action_id), "intervalSeconds": 3600, "maxRuns": 1}))["createSchedule"]["id"]
        first_run = await _waiting(schedule)
        created = (await _run(read, context, id=schedule))["schedule"]
        # Its one allowed run is planned and waiting: not over yet.
        assert (created["runCount"], created["exhausted"]) == (1, False) and created["nextRun"] is not None

        await _run("mutation($input: UpdateScheduleInput!) { updateSchedule(input: $input) { id } }", context, input={"id": schedule, "intervalSeconds": 60})
        await waiting_run(schedule, other_than=first_run)
        retimed = (await _run(read, context, id=schedule))["schedule"]
        assert retimed["runCount"] == 1 and retimed["exhausted"] is False
        assert retimed["nextRun"] is not None and retimed["nextRun"]["id"] != created["nextRun"]["id"]


class TestPoliciesAndTheFiringLog:
    async def test_policies_are_checked_and_lifted(self, authenticated_context, settings):
        context, _ = await _contexts()
        await _declared("policy-service", "CREATED")
        impl = await sync_to_async(_target)("policy-trig", context.request.organization)
        settings.SIGNAL_RETENTION_SECONDS = 3600

        async def create(**fields):
            values = {"name": "t", "kind": "CREATED", "identifier": IDENTIFIER, "action": str(impl.action_id), "port": "image", "args": {"size": 64}, **fields}
            return await schema.execute("mutation($input: CreateTriggerInput!) { createTrigger(input: $input) { id debounceSeconds maxRuns exhausted } }", variable_values={"input": values}, context_value=context)

        too_long = await create(debounceSeconds=7200)
        assert too_long.errors is not None and "signal retention" in str(too_long.errors[0])
        assert (await create(maxRuns=0)).errors is not None

        created = await create(debounceSeconds=60, maxRuns=1, description="one thumbnail per burst")
        assert created.errors is None, created.errors
        trigger = created.data["createTrigger"]
        assert (trigger["debounceSeconds"], trigger["maxRuns"], trigger["exhausted"]) == (60, 1, False)

        # takt counted its one allowed run: it is exhausted, until the limit is lifted.
        await models.Trigger.objects.filter(pk=trigger["id"]).aupdate(run_count=1)
        query = "query($id: ID!) { trigger(id: $id) { exhausted runCount } }"
        assert (await _run(query, context, id=trigger["id"]))["trigger"] == {"exhausted": True, "runCount": 1}
        lifted = await _run("mutation($input: UpdateTriggerInput!) { updateTrigger(input: $input) { exhausted maxRuns debounceSeconds } }", context, input={"id": trigger["id"], "maxRuns": None})
        assert lifted["updateTrigger"] == {"exhausted": False, "maxRuns": None, "debounceSeconds": 60}

    async def test_a_schedule_carries_its_policies_and_says_what_comes_next(self, authenticated_context):
        context, _ = await _contexts()
        impl = await _schedulable("policy-sched", context)
        created = await _run(
            "mutation($input: CreateScheduleInput!) { createSchedule(input: $input) { id overlap catchUp endsAt exhausted upcoming(count: 3) } }",
            context,
            input={"name": "hourly", "action": str(impl.action_id), "intervalSeconds": 3600, "overlap": "ALLOW", "catchUp": True, "endsAt": "2099-01-01T00:00:00+00:00"},
        )
        schedule = created["createSchedule"]
        assert (schedule["overlap"], schedule["catchUp"], schedule["exhausted"]) == ("ALLOW", True, False)
        assert len(schedule["upcoming"]) == 3 and schedule["upcoming"] == sorted(schedule["upcoming"])

        waiting = await _waiting(schedule["id"])
        ended = await _run("mutation($input: UpdateScheduleInput!) { updateSchedule(input: $input) { overlap } }", context, input={"id": schedule["id"], "endsAt": "2000-01-01T00:00:00+00:00", "overlap": "SKIP"})
        assert ended["updateSchedule"] == {"overlap": "SKIP"}
        # Its end passed: takt drops the run that was waiting, and with that it is over.
        await settled_run(waiting)
        assert (await _run("query($id: ID!) { schedule(id: $id) { exhausted } }", context, id=schedule["id"]))["schedule"] == {"exhausted": True}

    async def test_the_firing_log_is_read_and_a_trigger_is_replayed(self, authenticated_context):
        context, other = await _contexts()
        await _declared("firing-service", "CREATED")
        organization = context.request.organization
        impl = await sync_to_async(_target)("firing-trig", organization)
        trigger = await _trigger(context, impl, agent=str(impl.agent_id), interface=impl.interface)
        silent, loud = await _signal(organization, "one", 1), await _signal(organization, "three", 3)
        # What takt would have logged for the first signal.
        await models.Firing.objects.acreate(signal=silent, trigger_id=trigger, outcome="REJECTED", reason="the port's requires not met")

        log = "query($filters: FiringFilter) { firings(filters: $filters) { outcome reason replay signal { object } trigger { id } task { id } } }"
        assert (await _run(log, context, filters={"outcome": ["REJECTED"]}))["firings"] == [{"outcome": "REJECTED", "reason": "the port's requires not met", "replay": False, "signal": {"object": "one"}, "trigger": {"id": trigger}, "task": None}]
        assert (await _run(log, other))["firings"] == []  # another tenant sees none of it

        fire = "mutation($input: FireTriggerInput!) { fireTrigger(input: $input) { outcome replay task { id trigger { id } signal { id } } } }"
        replayed = (await _run(fire, context, input={"trigger": trigger, "signal": str(loud.pk)}))["fireTrigger"]
        assert (replayed["outcome"], replayed["replay"]) == ("FIRED", True)
        assert replayed["task"]["trigger"] == {"id": trigger} and replayed["task"]["signal"] == {"id": str(loud.pk)}
        foreign = await schema.execute(fire, variable_values={"input": {"trigger": trigger, "signal": str(loud.pk)}}, context_value=other)
        assert foreign.errors is not None

        seen = await _run('query($id: ID!) { signal(id: $id) { firings { outcome } } trigger(id: "%s") { firings { outcome } } }' % trigger, context, id=str(silent.pk))
        assert seen["signal"]["firings"] == [{"outcome": "REJECTED"}]
        assert [f["outcome"] for f in seen["trigger"]["firings"]] == ["FIRED", "REJECTED"]  # newest first


class TestRuleFeed:
    async def test_a_users_change_to_a_rule_is_published_to_its_organization(self, authenticated_context):
        """What the ``schedules`` / ``triggers`` subscriptions listen to, read off the channel layer itself."""
        from channels.layers import get_channel_layer

        context, other = await _contexts()
        layer = get_channel_layer()
        mine, theirs = await layer.new_channel(), await layer.new_channel()
        await layer.group_add(f"rules_org_{context.request.organization.pk}", mine)
        await layer.group_add(f"rules_org_{other.request.organization.pk}", theirs)

        impl = await _schedulable("feed-sched", context)
        schedule = (await _run(CREATE_SCHEDULE, context, input={"name": "hourly", "action": str(impl.action_id), "intervalSeconds": 3600}))["createSchedule"]["id"]
        await _run("mutation($input: ScheduleIdInput!) { deleteSchedule(input: $input) }", context, input={"id": schedule})

        async def received() -> list[tuple]:
            import asyncio

            seen = []
            while True:
                try:
                    message = (await asyncio.wait_for(layer.receive(mine), timeout=0.5))["message"]
                except asyncio.TimeoutError:
                    return seen
                seen.append((message["schedule"], message["trigger"], message["change"]))

        assert await received() == [(int(schedule), None, "create"), (int(schedule), None, "delete")]
        assert layer.channels.get(theirs) is None or layer.channels[theirs].empty()  # nothing for the other organization


class TestSearchAndLookups:
    async def test_rules_are_searched_by_what_they_run_and_where_they_came_from(self, authenticated_context):
        context, _ = await _contexts()
        await _declared("search-service", "CREATED")
        impl = await sync_to_async(_target)("search-trig", context.request.organization)
        await models.Agent.objects.filter(pk=impl.agent_id).aupdate(name="thumbnailer")
        await models.Action.objects.filter(pk=impl.action_id).aupdate(name="Make Thumbnail")
        plain = await _trigger(context, impl, name="plain", description="keeps previews fresh")
        pinned = await _trigger(context, impl, name="pinned", agent=str(impl.agent_id), interface=impl.interface)
        query = "query($filters: TriggerFilter) { triggers(filters: $filters, ordering: [{name: ASC}]) { id } }"

        async def found(text):
            return [row["id"] for row in (await _run(query, context, filters={"search": text}))["triggers"]]

        assert await found("PREVIEWS") == [plain]  # its description
        assert await found("thumbnailer") == [pinned]  # the agent it is pinned to
        assert await found("make thumb") == [pinned, plain]  # the action both run
        assert await found("arraydataset") == [pinned, plain]  # the structure they listen for
        assert await found("nothing like this") == []

        scheduled = await _schedulable("search-sched", context)
        await models.Action.objects.filter(pk=scheduled.action_id).aupdate(name="Sweep Mailboxes")
        schedule = (await _run(CREATE_SCHEDULE, context, input={"name": "nightly", "action": str(scheduled.action_id), "cron": "0 2 * * *"}))["createSchedule"]["id"]
        schedules = "query($filters: ScheduleFilter) { schedules(filters: $filters) { id } }"
        for text in ("mailboxes", "0 2 *", "night"):
            assert [row["id"] for row in (await _run(schedules, context, filters={"search": text}))["schedules"]] == [schedule], text

    async def test_signals_are_searched_by_text_and_name_their_service(self, authenticated_context):
        context, other = await _contexts()
        organization = context.request.organization
        catalogued = await models.Service.objects.acreate(name="mikro", description="Microscopy data")
        dataset = await _signal(organization, "dataset-77", 3)
        stranger = await models.Signal.objects.acreate(service="retired", signal_id="x", kind="CREATED", identifier="@old/thing", object="1", organization=organization, descriptors={"@old/colour": "magenta"})
        # Another tenant's signal about an object of the same id.
        await models.Signal.objects.acreate(service="mikro", signal_id="theirs", kind="CREATED", identifier=IDENTIFIER, object="dataset-77", organization=other.request.organization, descriptors={CHANNELS: 3})
        query = "query($filters: SignalFilter) { signals(filters: $filters) { id serviceName service { id name description } } }"

        async def found(text):
            return [row["id"] for row in (await _run(query, context, filters={"search": text}))["signals"]]

        assert await found("dataset-77") == [str(dataset.pk)]  # the object's id
        assert await found("ARRAYDATASET") == [str(dataset.pk)]  # the structure
        assert await found("magenta") == [str(stranger.pk)]  # a descriptor's value
        assert await found("n_channels") == [str(dataset.pk)]  # a descriptor's key
        assert await found("retired") == [str(stranger.pk)]  # the sender

        rows = {row["id"]: row for row in (await _run(query, context))["signals"]}
        assert rows[str(dataset.pk)]["service"] == {"id": str(catalogued.pk), "name": "mikro", "description": "Microscopy data"}
        # A sender the hub no longer catalogues is still named.
        assert (rows[str(stranger.pk)]["serviceName"], rows[str(stranger.pk)]["service"]) == ("retired", None)

    async def test_a_service_and_a_firing_are_fetched_by_id(self, authenticated_context):
        context, other = await _contexts()
        await _declared("lookup-service", "CREATED")
        service = await models.Service.objects.aget(name="lookup-service")
        organization = context.request.organization
        impl = await sync_to_async(_target)("lookup-trig", organization)
        trigger = await _trigger(context, impl)
        signal = await _signal(organization, "one", 1)
        firing = await models.Firing.objects.acreate(signal=signal, trigger_id=trigger, outcome="REJECTED", reason="too few channels")

        found = await _run("query($id: ID!) { service(id: $id) { name signals { kind } } }", context, id=str(service.pk))
        assert found["service"] == {"name": "lookup-service", "signals": [{"kind": "CREATED"}]}
        # A service is the hub's: every organization reads it.
        assert (await _run("query($id: ID!) { service(id: $id) { name } }", other, id=str(service.pk)))["service"] == {"name": "lookup-service"}

        query = "query($id: ID!) { firing(id: $id) { outcome reason trigger { id } signal { object } } }"
        assert (await _run(query, context, id=str(firing.pk)))["firing"] == {"outcome": "REJECTED", "reason": "too few channels", "trigger": {"id": trigger}, "signal": {"object": "one"}}
        foreign = await schema.execute(query, variable_values={"id": str(firing.pk)}, context_value=other)
        assert foreign.errors is not None  # another tenant's firing reads as missing
