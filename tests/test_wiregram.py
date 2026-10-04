"""Wiregrams: one document of automation, imported by an organization's user.

Real postgres and a real takt (``takt``: it plans and cancels the schedules' runs and
records every request).
"""

import pytest
from asgiref.sync import sync_to_async

from facade import models
from facade.schema import schema
from tests.conftest import settled_run, waiting_run
from tests.factories import TEST_TOKEN
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context
from tests.test_schedules import _schedulable
from tests.test_triggers import CHANNELS, IDENTIFIER, _target

pytestmark = [pytest.mark.usefixtures("takt"), pytest.mark.django_db(transaction=True), pytest.mark.asyncio]

IMPORT = """
    mutation($input: WiregramInput!) {
        importWiregram(input: $input) {
            id key name
            schedules { id name wireKey enabled intervalSeconds cron overlap wiregram { key } }
            triggers { id name wireKey kind port conditions debounceSeconds }
        }
    }
"""


async def _contexts():
    return await sync_to_async(lambda: (tenant_context(TEST_TOKEN)[0], tenant_context(OTHER_TOKEN)[0]))()


async def _named(implementation: models.Implementation, name: str) -> models.Implementation:
    await models.Agent.objects.filter(pk=implementation.agent_id).aupdate(name=name)
    return implementation


async def _hub(context, prefix: str):
    """An organization with a 'kuvert' agent offering a sweep and a 'thumbs' agent whose action takes a dataset."""
    sweep = await _named(await _schedulable(f"{prefix}-sweep", context), "kuvert")
    thumbs = await _named(await sync_to_async(_target)(f"{prefix}-thumbs", context.request.organization), "thumbs")
    if not await models.SignalDeclaration.objects.filter(identifier=IDENTIFIER, kind="CREATED").aexists():
        service = await models.Service.objects.acreate(name=f"{prefix}-service")
        await models.SignalDeclaration.objects.acreate(service=service, identifier=IDENTIFIER, kind="CREATED", descriptor_keys=[CHANNELS])
    return sweep, thumbs


def _document(sweep, thumbs, **overrides) -> dict:
    return {
        "key": "housekeeping",
        "name": "Housekeeping",
        "schedules": [{"key": "sync", "name": "Sync mail", "agent": "kuvert", "interface": sweep.interface, "intervalSeconds": 300}],
        "triggers": [
            {
                "key": "thumbnail",
                "name": "Thumbnail new images",
                "agent": "thumbs",
                "interface": thumbs.interface,
                "kind": "CREATED",
                "identifier": IDENTIFIER,
                "port": "image",
                "args": {"size": 64},
                "conditions": [{"key": CHANNELS, "operator": "GTE", "value": 2}],
                "debounceSeconds": 60,
            }
        ],
        **overrides,
    }


async def _import(context, document):
    return await schema.execute(IMPORT, variable_values={"input": document}, context_value=context)


async def _waiting(schedule: str) -> int:
    """The run takt planned for the schedule. An import's answer does not wait for it."""
    return await waiting_run(schedule)


async def test_importing_creates_the_rules_and_plans_the_schedules(authenticated_context):
    context, other = await _contexts()
    sweep, thumbs = await _hub(context, "wg-create")

    result = await _import(context, _document(sweep, thumbs))
    assert result.errors is None, result.errors
    wiregram = result.data["importWiregram"]
    (schedule,) = wiregram["schedules"]
    (trigger,) = wiregram["triggers"]
    assert (schedule["wireKey"], schedule["intervalSeconds"], schedule["overlap"], schedule["wiregram"]) == ("sync", 300, "SKIP", {"key": "housekeeping"})
    assert await _waiting(schedule["id"]) is not None  # takt planned it on hearing of the import
    assert (trigger["wireKey"], trigger["port"], trigger["debounceSeconds"]) == ("thumbnail", "image", 60)
    assert trigger["conditions"] == [{"key": CHANNELS, "operator": "GTE", "value": 2}]

    # The rules are ordinary rules of the organization, and only of it.
    listed = await schema.execute("query { wiregrams { key } schedules { id } triggers { id } }", context_value=context)
    assert listed.data == {"wiregrams": [{"key": "housekeeping"}], "schedules": [{"id": schedule["id"]}], "triggers": [{"id": trigger["id"]}]}
    elsewhere = await schema.execute("query { wiregrams { key } schedules { id } triggers { id } }", context_value=other)
    assert elsewhere.data == {"wiregrams": [], "schedules": [], "triggers": []}


async def test_importing_again_updates_in_place_and_removes_what_is_no_longer_listed(authenticated_context, schedule_notices):
    context, _ = await _contexts()
    sweep, thumbs = await _hub(context, "wg-again")
    first = (await _import(context, _document(sweep, thumbs))).data["importWiregram"]
    schedule_id = first["schedules"][0]["id"]
    waiting = await _waiting(schedule_id)
    # The organization switched the schedule off, and takt counted a failure on it.
    await models.Schedule.objects.filter(pk=schedule_id).aupdate(enabled=False, consecutive_failures=2)
    schedule_notices.sent()

    document = _document(sweep, thumbs, name="Housekeeping v2", triggers=[])
    document["schedules"][0].update({"name": "Sync mail hourly", "intervalSeconds": 3600})
    second = await _import(context, document)
    assert second.errors is None, second.errors
    wiregram = second.data["importWiregram"]

    assert wiregram["id"] == first["id"] and wiregram["name"] == "Housekeeping v2"
    (schedule,) = wiregram["schedules"]
    assert (schedule["id"], schedule["name"], schedule["intervalSeconds"]) == (schedule_id, "Sync mail hourly", 3600)
    kept = await models.Schedule.objects.aget(pk=schedule_id)
    assert (kept.enabled, kept.consecutive_failures) == (False, 2)  # the organization's switch and takt's bookkeeping stay
    assert wiregram["triggers"] == [] and not await models.Trigger.objects.filter(pk=first["triggers"][0]["id"]).aexists()
    # One notice, sent with the change: the run planned on the old interval goes, and a new one is planned.
    assert [notice["replan"] for notice in schedule_notices.sent()] == [True]
    assert (await settled_run(waiting)).latest_event_kind == "CANCELLED"


async def test_a_document_that_cannot_be_is_refused_whole(authenticated_context):
    context, _ = await _contexts()
    sweep, thumbs = await _hub(context, "wg-refuse")

    async def refused(document) -> str:
        result = await _import(context, document)
        assert result.errors is not None
        assert not await models.Wiregram.objects.aexists() and not await models.Schedule.objects.aexists() and not await models.Trigger.objects.aexists()
        return str(result.errors[0])

    unknown_agent = _document(sweep, thumbs)
    unknown_agent["schedules"][0]["agent"] = "nobody"
    assert "no agent named 'nobody'" in await refused(unknown_agent)

    unknown_interface = _document(sweep, thumbs)
    unknown_interface["schedules"][0]["interface"] = "does_not_exist"
    message = await refused(unknown_interface)
    assert "no interface 'does_not_exist'" in message and sweep.interface in message  # says what it does offer

    # Every broken rule is named, not only the first.
    both = _document(sweep, thumbs)
    both["schedules"][0]["agent"] = "nobody"
    both["triggers"][0]["port"] = "size"
    message = await refused(both)
    assert "Schedule 'sync'" in message and "Trigger 'thumbnail'" in message

    twice = _document(sweep, thumbs)
    twice["schedules"].append(dict(twice["schedules"][0]))
    assert "more than once" in await refused(twice)

    no_timing = _document(sweep, thumbs)
    del no_timing["schedules"][0]["intervalSeconds"]
    assert "exactly one of" in await refused(no_timing)


async def test_two_agents_of_one_name_are_not_guessed_between(authenticated_context):
    context, _ = await _contexts()
    sweep, thumbs = await _hub(context, "wg-twins")
    twin = await _named(await _schedulable("wg-twins-second", context), "kuvert")
    await models.Implementation.objects.filter(pk=twin.pk).aupdate(interface=sweep.interface)

    result = await _import(context, _document(sweep, thumbs))
    assert result.errors is not None and "several agents named 'kuvert'" in str(result.errors[0])
    assert not await models.Schedule.objects.aexists()


async def test_deleting_a_wiregram_removes_its_rules_and_cancels_their_waiting_runs(authenticated_context):
    context, other = await _contexts()
    sweep, thumbs = await _hub(context, "wg-delete")
    wiregram = (await _import(context, _document(sweep, thumbs))).data["importWiregram"]
    waiting = await _waiting(wiregram["schedules"][0]["id"])
    delete = "mutation($input: WiregramIdInput!) { deleteWiregram(input: $input) }"

    foreign = await schema.execute(delete, variable_values={"input": {"id": wiregram["id"]}}, context_value=other)
    assert foreign.errors is not None  # another tenant's wiregram reads as missing

    deleted = await schema.execute(delete, variable_values={"input": {"id": wiregram["id"]}}, context_value=context)
    assert deleted.errors is None, deleted.errors
    assert not await models.Schedule.objects.aexists() and not await models.Trigger.objects.aexists()
    run = await settled_run(waiting)
    assert run.latest_event_kind == "CANCELLED" and run.schedule_id is None  # cancelled, and kept as history


async def test_rules_are_exported_as_a_document_another_organization_can_import(authenticated_context):
    context, other = await _contexts()
    sweep, thumbs = await _hub(context, "wg-export")
    wiregram = (await _import(context, _document(sweep, thumbs))).data["importWiregram"]
    export = "mutation($input: ExportWiregramInput!) { exportWiregram(input: $input) }"

    exported = await schema.execute(
        export,
        variable_values={"input": {"key": "copied", "name": "Copied", "schedules": [wiregram["schedules"][0]["id"]], "triggers": [wiregram["triggers"][0]["id"]]}},
        context_value=context,
    )
    assert exported.errors is None, exported.errors
    document = exported.data["exportWiregram"]
    assert document["schedules"][0]["agent"] == "kuvert" and document["triggers"][0]["conditions"] == [{"key": CHANNELS, "operator": "GTE", "value": 2}]
    stored = (await schema.execute("query($id: ID!) { wiregram(id: $id) { document } }", variable_values={"id": wiregram["id"]}, context_value=context)).data["wiregram"]["document"]
    assert stored["schedules"][0]["interval_seconds"] == 300  # what was imported is kept, importable as it is

    # The other organization has agents of the same names: the document fits it as it is.
    await _hub(other, "wg-export-other")
    await models.Implementation.objects.filter(agent__organization=other.request.organization, agent__name="kuvert").aupdate(interface=sweep.interface)
    await models.Implementation.objects.filter(agent__organization=other.request.organization, agent__name="thumbs").aupdate(interface=thumbs.interface)
    imported = await _import(other, {key: value for key, value in document.items()} | {"schedules": [_camel(s) for s in document["schedules"]], "triggers": [_camel(t) for t in document["triggers"]]})
    assert imported.errors is None, imported.errors
    assert await _waiting(imported.data["importWiregram"]["schedules"][0]["id"]) is not None

    # A rule that names an action but no agent cannot be written down as a wiregram.
    unpinned = await models.Schedule.objects.acreate(name="loose", caller_id=(await models.Schedule.objects.afirst()).caller_id, action_id=sweep.action_id, interval_seconds=60)
    refused = await schema.execute(export, variable_values={"input": {"key": "x", "name": "x", "schedules": [str(unpinned.pk)]}}, context_value=context)
    assert refused.errors is not None and "not pinned to an agent" in str(refused.errors[0])


def _camel(rule: dict) -> dict:
    """A document rule (snake_case, as stored) as GraphQL input variables (camelCase)."""

    def camel(name: str) -> str:
        head, *rest = name.split("_")
        return head + "".join(part.title() for part in rest)

    return {camel(key): value for key, value in rule.items() if value is not None}
