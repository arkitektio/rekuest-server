"""Signals and triggers: a service announces an object, a user's trigger runs an action on it.

Real postgres + redis. The reaper is off under test settings, so tests drive ``fire_triggers``
themselves, from a fresh backend each time. Provenance tokens are signed with rekuest's OWN key
(``mint._sign``), exactly what a service would forward back.
"""

import datetime
import json
import threading
import time
import uuid

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from facade import enums, hooks, models, registration, triggers
from facade.caller_context import CallerContext
from facade.persist_backend import ModelPersistBackend
from facade.provenance import keys
from facade.provenance.mint import _sign, mint_token_for_task
from facade.service_agents import _identity

IDENTIFIER = "@mikro/arraydataset"
CHANNELS = "@mikro/n_channels"


@pytest.fixture
def mikro_service(settings):
    settings.SERVICE_AGENTS = [{"service": "mikro", "hook_url": "http://127.0.0.1:9/_rekuest/hook"}]
    return settings


def _org(slug: str):
    from authentikate.models import Organization

    return Organization.objects.get_or_create(slug=slug)[0]


def _target(prefix: str, org, *, needs_token: bool = False) -> models.Implementation:
    """An agent in ``org`` implementing an action whose ``image`` port takes a multi-channel dataset."""
    from facade.mutations.agent import ImplementAgentInputModel
    from rekuest_core.enums import ActionKind
    from rekuest_core.inputs.models import DefinitionInputModel, ImplementationInputModel

    user, client = _identity(prefix, org)
    definition = DefinitionInputModel(
        key=f"{prefix}-thumbnail",
        name="Thumbnail",
        kind=ActionKind.FUNCTION,
        args=[
            {"key": "image", "kind": "STRUCTURE", "identifier": IDENTIFIER, "requires": [{"key": CHANNELS, "operator": "GTE", "value": 2}]},
            {"key": "size", "kind": "INT", "nullable": True},
        ],
    )
    agent, _ = registration.implement_agent(client, user, org, ImplementAgentInputModel(name=prefix, implementations=[ImplementationInputModel(interface="thumbnail", definition=definition, needs_token=needs_token)]))
    models.Agent.objects.filter(pk=agent.pk).update(kind=enums.AgentKind.WEBHOOK.value, hook_url="http://127.0.0.1:9/hook", hook_url_secret="x")
    return models.Implementation.objects.select_related("action", "agent").get(agent=agent, interface="thumbnail")


def _trigger(impl: models.Implementation, *, conditions=None, owner_prefix: str = "owner") -> models.Trigger:
    user, client = _identity(f"{owner_prefix}-{impl.pk}", impl.agent.organization)
    caller, _ = models.Caller.objects.get_or_create(client=client, user=user, organization=impl.agent.organization)
    stored, compiled = triggers.compile_conditions(conditions or [])
    return models.Trigger.objects.create(
        name="thumbnail new images", caller=caller, kind="CREATED", identifier=IDENTIFIER, conditions=stored, compiled_jsonpath=compiled,
        action=impl.action, agent=impl.agent, interface=impl.interface, port="image", args={"size": 128},
    )


def _causing_task(impl: models.Implementation, *, depth: int = 0) -> models.Task:
    """A task in the target's organization, as the one a service was called in."""
    user, client = _identity(f"cause-{uuid.uuid4().hex[:6]}", impl.agent.organization)
    caller, _ = models.Caller.objects.get_or_create(client=client, user=user, organization=impl.agent.organization)
    return models.Task.objects.create(
        action=impl.action, implementation=impl, agent=impl.agent, caller=caller, args={}, trigger_depth=depth,
        latest_event_kind=enums.TaskEventKind.STARTED, latest_instruct_kind=enums.TaskInstructKind.ASSIGN,
    )


def _token(task: models.Task, *, issuer: str | None = None, expired: bool = False) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    exp = now - datetime.timedelta(minutes=5) if expired else now + datetime.timedelta(hours=1)
    claims = {"iss": issuer or keys.issuer(), "aud": ["mikro"], "sub": "x", "act": {"sub": "a", "cid": "c"}, "iat": int(now.timestamp()), "exp": int(exp.timestamp()), "jti": uuid.uuid4().hex, "tsk": str(task.pk), "rtk": str(task.root_id or task.pk), "rcb": task.caller.user.sub, "ahs": "h", "aha": "v1"}
    return _sign(claims)


def _post(org, *, channels: int = 3, provenance: str | None = None, signal_id: str | None = None, signer: str = "mikro", service: str = "mikro", signed_at: float | None = None):
    """POST a signal as ``signer`` (its test instance key) would, to ``service``'s endpoint."""
    from django.conf import settings as django_settings
    from rekuest_service import trust

    body = json.dumps({"id": signal_id or uuid.uuid4().hex, "kind": "CREATED", "identifier": IDENTIFIER, "object": "42", "organization": org.slug, "descriptors": {CHANNELS: channels}, "provenance": provenance}).encode()
    url = reverse("signal_intake", kwargs={"service": service})
    real_time = trust.time.time
    if signed_at is not None:
        trust.time.time = lambda: signed_at
    try:
        authorization = trust.sign("POST", url, body, issuer=f"live.arkitekt.{signer}", audience=django_settings.REKUEST_IDENTIFIER, key=django_settings.TEST_SERVICE_KEYS[signer])
    finally:
        trust.time.time = real_time
    return HttpClient().post(url, data=body, content_type="application/json", headers={"Authorization": authorization})


def _fire() -> int:
    return async_to_sync(ModelPersistBackend().fire_triggers)()


@pytest.mark.django_db(transaction=True)
class TestIntake:
    def test_a_signed_signal_is_stored_with_its_verified_cause(self, mikro_service):
        org = _org("sig-intake")
        impl = _target("sig-intake", org)
        cause = _causing_task(impl)

        response = _post(org, provenance=_token(cause))
        assert response.status_code == 202, response.content
        signal = models.Signal.objects.get()
        assert signal.causing_task_id == cause.pk and signal.descriptors == {CHANNELS: 3}
        assert signal.processed_at is None

    def test_a_resend_of_the_same_signal_is_a_no_op(self, mikro_service):
        org = _org("sig-dedupe")
        _post(org, signal_id="s-1")
        _post(org, signal_id="s-1")
        assert models.Signal.objects.count() == 1

    @pytest.mark.parametrize("case", ["impostor", "unknown_service", "stale"])
    def test_unauthenticated_signals_are_refused(self, mikro_service, case):
        org = _org(f"sig-{case}")
        if case == "impostor":
            # bank's genuine key, posting as mikro: the bundle knows whose key it is.
            assert _post(org, signer="bank").status_code == 401
        elif case == "unknown_service":
            assert _post(org, service="nobody").status_code == 404
        else:
            assert _post(org, signed_at=time.time() - 3600).status_code == 401
        assert models.Signal.objects.count() == 0

    def test_an_unknown_organization_is_dropped(self, mikro_service):
        from types import SimpleNamespace

        assert _post(SimpleNamespace(slug="no-such-org")).status_code == 202
        assert models.Signal.objects.count() == 0

    @pytest.mark.parametrize("case", ["expired", "other_issuer", "other_org", "garbage"])
    def test_a_token_that_does_not_hold_leaves_no_cause(self, mikro_service, case):
        org = _org(f"sig-tok-{case}")
        impl = _target(f"sig-tok-{case}", org)
        cause = _causing_task(impl)
        if case == "expired":
            token = _token(cause, expired=True)
        elif case == "other_issuer":
            token = _token(cause, issuer="somebody-else")
        elif case == "other_org":
            foreign = _causing_task(_target(f"sig-tok-foreign-{case}", _org("sig-tok-foreign")))
            token = _token(foreign)
        else:
            token = "not-a-token"

        assert _post(org, provenance=token).status_code == 202
        assert models.Signal.objects.get().causing_task_id is None


@pytest.mark.django_db(transaction=True)
class TestFiring:
    def test_a_matching_signal_runs_the_action_as_a_child_of_its_cause(self, mikro_service):
        org = _org("fire-child")
        impl = _target("fire-child", org)
        trigger = _trigger(impl)
        cause = _causing_task(impl, depth=1)
        _post(org, provenance=_token(cause))

        assert _fire() == 1
        run = models.Task.objects.get(trigger=trigger)
        signal = models.Signal.objects.get()
        assert run.parent_id == cause.pk and run.root_id == cause.pk
        assert run.signal_id == signal.pk and run.trigger_depth == 2
        assert run.args == {"size": 128, "image": {"__identifier": IDENTIFIER, "object": "42"}}
        assert run.reference == f"trigger:{trigger.pk}:{signal.pk}"
        assert signal.processed_at is not None
        assert _fire() == 0  # processed: never fired twice

    def test_without_a_cause_the_run_is_a_root_of_the_trigger_owner(self, mikro_service):
        org = _org("fire-root")
        trigger = _trigger(_target("fire-root", org))
        _post(org)

        assert _fire() == 1
        run = models.Task.objects.get(trigger=trigger)
        assert run.parent_id is None and run.caller_id == trigger.caller_id and run.trigger_depth == 1

    def test_the_ports_own_requires_filters(self, mikro_service):
        org = _org("fire-requires")
        trigger = _trigger(_target("fire-requires", org))
        _post(org, channels=1)  # the port requires n_channels >= 2

        assert _fire() == 0
        assert models.Signal.objects.get().processed_at is not None
        assert models.Trigger.objects.get(pk=trigger.pk).last_error is None  # filtered out, not failed
        assert models.ArgPort.objects.get(action=trigger.action, key="image").compiled_jsonpath

    def test_the_triggers_conditions_filter(self, mikro_service):
        org = _org("fire-conditions")
        _trigger(_target("fire-conditions", org), conditions=[{"key": CHANNELS, "operator": "LTE", "value": 3}])
        _post(org, channels=5)
        _post(org, channels=3)

        assert _fire() == 1
        assert models.Task.objects.get(trigger__isnull=False).signal.descriptors == {CHANNELS: 3}

    def test_triggers_only_see_their_own_organization(self, mikro_service):
        _trigger(_target("fire-tenant-a", _org("fire-tenant-a")))
        _post(_org("fire-tenant-b"))
        assert _fire() == 0

    def test_the_loop_guard_stops_deep_chains(self, mikro_service, settings):
        settings.TRIGGER_MAX_DEPTH = 3
        org = _org("fire-loop")
        impl = _target("fire-loop", org)
        trigger = _trigger(impl)
        _post(org, provenance=_token(_causing_task(impl, depth=3)))

        assert _fire() == 0
        assert "triggers deep" in models.Trigger.objects.get(pk=trigger.pk).last_error

    def test_a_broken_target_is_recorded_not_retried(self, mikro_service):
        org = _org("fire-broken")
        impl = _target("fire-broken", org)
        trigger = _trigger(impl)
        models.Trigger.objects.filter(pk=trigger.pk).update(args={"size": "not-an-int"})
        _post(org)

        assert _fire() == 0
        refreshed = models.Trigger.objects.get(pk=trigger.pk)
        assert refreshed.consecutive_failures == 1 and "Could not run" in refreshed.last_error
        assert models.Signal.objects.get().processed_at is not None

    def test_two_reapers_fire_each_trigger_once(self, mikro_service):
        org = _org("fire-race")
        trigger = _trigger(_target("fire-race", org))
        _post(org)
        results: list[int] = []
        barrier = threading.Barrier(2)

        def run() -> None:
            try:
                barrier.wait()
                results.append(_fire())
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(results) == 1
        assert models.Task.objects.filter(trigger=trigger).count() == 1

    def test_a_child_run_carries_the_causing_trees_human(self, mikro_service):
        org = _org("fire-prov")
        impl = _target("fire-prov", org, needs_token=True)
        trigger = _trigger(impl)
        cause = _causing_task(impl)
        _post(org, provenance=_token(cause))
        _fire()

        run = models.Task.objects.select_related("implementation", "agent__user", "agent__client").get(trigger=trigger)
        caller = trigger.caller
        token = mint_token_for_task(run, CallerContext(user=caller.user, client=caller.client, organization=caller.organization, roles=[]))
        from joserfc import jwt
        from joserfc.jwk import KeySet

        claims = jwt.decode(token, KeySet([keys.get_public_key()]), algorithms=keys.ALGORITHMS).claims
        assert claims["rcb"] == cause.caller.user.sub and claims["rtk"] == str(cause.pk)


CREATE_TRIGGER = """
    mutation($input: CreateTriggerInput!) { createTrigger(input: $input) { id port kind identifier conditions } }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestTriggerGraphQL:
    async def test_create_validates_and_is_scoped(self, authenticated_context):
        from asgiref.sync import sync_to_async

        from facade.schema import schema
        from tests.factories import TEST_TOKEN
        from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context

        context_a, context_b = await sync_to_async(lambda: (tenant_context(TEST_TOKEN)[0], tenant_context(OTHER_TOKEN)[0]))()
        impl = await sync_to_async(_target)("gql-trigger", context_a.request.organization)
        await models.SignalDeclaration.objects.acreate(agent=impl.agent, identifier=IDENTIFIER, kind="CREATED", descriptor_keys=[CHANNELS])

        def create(port, conditions=None, kind="CREATED", identifier=IDENTIFIER):
            inp = {"name": "t", "kind": kind, "identifier": identifier, "action": str(impl.action_id), "port": port, "args": {"size": 64}}
            if conditions is not None:
                inp["conditions"] = conditions
            return schema.execute(CREATE_TRIGGER, variable_values={"input": inp}, context_value=context_a)

        undeclared_kind = await create("image", kind="DELETED")
        assert undeclared_kind.errors and "No service emits DELETED" in str(undeclared_kind.errors[0])
        undeclared_identifier = await create("image", identifier="@mikro/nothing")
        assert undeclared_identifier.errors
        unknown_key = await create("image", [{"key": "@mikro/n_chanels", "operator": "GTE", "value": 1}])
        assert unknown_key.errors and "@mikro/n_chanels" in str(unknown_key.errors[0])

        wrong_port = await create("size")
        assert wrong_port.errors and "not a @mikro/arraydataset structure" in str(wrong_port.errors[0])
        bad_condition = await create("image", [{"key": CHANNELS, "operator": "IN", "value": 3}])
        assert bad_condition.errors

        created = await create("image", [{"key": CHANNELS, "operator": "GTE", "value": 1}])
        assert created.errors is None, created.errors
        seen_by_a = await schema.execute("query { triggers { id } }", context_value=context_a)
        seen_by_b = await schema.execute("query { triggers { id } }", context_value=context_b)
        assert [t["id"] for t in seen_by_a.data["triggers"]] == [created.data["createTrigger"]["id"]]
        assert seen_by_b.data["triggers"] == []

        # Declarations are hub-wide: every tenant sees what the services emit.
        listed = await schema.execute("query { signalDeclarations { identifier kind descriptorKeys service } }", context_value=context_b)
        assert listed.errors is None, listed.errors
        assert listed.data["signalDeclarations"] == [{"identifier": IDENTIFIER, "kind": "CREATED", "descriptorKeys": [CHANNELS], "service": impl.agent.name}]


@pytest.mark.django_db(transaction=True)
def test_a_service_emit_travels_to_a_triggered_run(live_server, settings):
    """rekuest's own rekuest_service copy plays the service: emit → signed POST → intake → fire."""
    from django.db import transaction as db_transaction
    from django.conf import settings as django_settings

    from rekuest_service import Service

    prefix = f"/{django_settings.MY_SCRIPT_NAME.strip('/')}" if django_settings.MY_SCRIPT_NAME else ""
    settings.SERVICE_AGENTS = [{"service": "mikro", "hook_url": "http://127.0.0.1:9/_rekuest/hook"}]
    settings.REKUEST_HOOK = {"REKUEST_URL": f"{live_server.url}{prefix}", "SERVICE": "mikro"}
    org = _org("emit-roundtrip")
    trigger = _trigger(_target("emit-roundtrip", org))

    dataset_created = Service("mikro", key=settings.TEST_SERVICE_KEYS["mikro"]).signal(IDENTIFIER, kinds=["CREATED"], descriptors=[CHANNELS])
    with db_transaction.atomic():
        dataset_created.emit(42, organization=org.slug, descriptors={CHANNELS: 4})
        assert models.Signal.objects.count() == 0  # nothing leaves before the commit

    deadline = time.monotonic() + 10
    while not models.Signal.objects.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _fire() == 1
    assert models.Task.objects.get(trigger=trigger).args["image"] == {"__identifier": IDENTIFIER, "object": "42"}
